"""JSON 文件存储层。

三个业务数据文件，各自独立加锁：
    data/users.json     账号、密码哈希、角色
    data/orders.json    订单
    data/refunds.json   退款申请与审批记录

另有两个记录型文件（同样加锁，只追加为主）：
    data/audit.json     审计事件流：用户指令、Agent 步骤、人工操作
    data/sessions.json  Agent 会话与消息历史

JSON 文件不像数据库那样自带事务和并发控制，所以这里自己补两件事：

1. **跨进程互斥锁**：每个文件配一个 `.lock` 伴生文件。Windows 用 msvcrt，
   Unix 用 fcntl。Agent 的 MCP 子进程和商家后台是两个独立进程，会同时写
   同一个文件——没有文件锁就会出现丢更新。
2. **原子替换**：先写同目录下的临时文件，再 os.replace 覆盖。
   os.replace 在同一文件系统内是原子操作，读者要么看到旧内容，要么看到
   新内容，不会读到写了一半的 JSON。

另外每个文件带 `schema_version`，后续结构变更时可以据此做迁移。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

BASE_DIR = Path(__file__).resolve().parent.parent
SCHEMA_VERSION = 1

USERS_FILE = "users.json"
ORDERS_FILE = "orders.json"
REFUNDS_FILE = "refunds.json"
AUDIT_FILE = "audit.json"
SESSIONS_FILE = "sessions.json"


def data_dir() -> Path:
    """数据目录，可用 ORDER_DATA_DIR 覆盖。相对路径按项目根解析。"""
    raw = os.getenv("ORDER_DATA_DIR", "").strip()
    if not raw:
        return BASE_DIR / "data"
    path = Path(raw)
    return path if path.is_absolute() else BASE_DIR / path


def path_of(name: str) -> Path:
    return data_dir() / name


# --------------------------------------------------------------------------
# 文件锁
# --------------------------------------------------------------------------
@contextmanager
def _file_lock(target: Path, timeout: float = 10.0) -> Iterator[None]:
    """跨进程排它锁。锁文件与数据文件同目录。"""
    lock_path = target.with_name(target.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"等待文件锁超时: {lock_path}") from None
                    time.sleep(0.02)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"等待文件锁超时: {lock_path}") from None
                    time.sleep(0.02)
        yield
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def _read(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return json.loads(json.dumps(default))
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return json.loads(json.dumps(default))
    return json.loads(raw)


def _write_atomic(path: Path, data: dict[str, Any]) -> None:
    """临时文件 + os.replace，保证读者不会读到半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


@contextmanager
def transaction(name: str, default: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """读-改-写事务：全程持锁，正常退出才落盘，异常则丢弃改动。"""
    path = path_of(name)
    with _file_lock(path):
        data = _read(path, default)
        yield data
        _write_atomic(path, data)


def read_only(name: str, default: dict[str, Any]) -> dict[str, Any]:
    """只读取一份快照，不加排它锁（原子替换保证内容完整）。"""
    return _read(path_of(name), default)


# --------------------------------------------------------------------------
# 密码
# --------------------------------------------------------------------------
_ITERATIONS = 260_000


def hash_password(password: str) -> str:
    """PBKDF2-HMAC-SHA256。格式: pbkdf2_sha256$迭代数$salt_b64$hash_b64"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _ITERATIONS)
    return "pbkdf2_sha256${}${}${}".format(
        _ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """常数时间比较，避免通过响应时间猜测密码。"""
    try:
        algo, iterations, salt_b64, hash_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
        return hmac.compare_digest(digest, expected)
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------
# 默认结构
# --------------------------------------------------------------------------
def empty_users() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "users": {}}


def empty_orders() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "orders": {}}


def empty_refunds() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "refunds": {}, "locks": {}, "seq": 0}


def empty_audit() -> dict[str, Any]:
    """审计事件流。events 以 seq 为序，只追加，不修改历史事件。"""
    return {"schema_version": SCHEMA_VERSION, "events": {}, "seq": 0}


def empty_sessions() -> dict[str, Any]:
    """Agent 会话。CLI 与 Web 共用同一份，便于审计页回溯。"""
    return {"schema_version": SCHEMA_VERSION, "sessions": {}, "seq": 0}


def ensure_files() -> None:
    """确保数据文件存在，避免首次运行时到处判断 None。"""
    for name, factory in (
        (USERS_FILE, empty_users),
        (ORDERS_FILE, empty_orders),
        (REFUNDS_FILE, empty_refunds),
        (AUDIT_FILE, empty_audit),
        (SESSIONS_FILE, empty_sessions),
    ):
        path = path_of(name)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            _write_atomic(path, factory())


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

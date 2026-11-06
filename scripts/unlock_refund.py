"""运营人工解锁——解除某订单的退款驳回锁。

退款被人工驳回后，该订单进入锁定态，用户无法自行再次申请。
运营复核通过后，用本脚本解锁，用户才能重走退款流程。

用法:
    python scripts/unlock_refund.py --list                    # 查看所有锁
    python scripts/unlock_refund.py --active                  # 只看生效中的锁
    python scripts/unlock_refund.py ORD20261008009 --by 陈明  # 解锁
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv

load_dotenv(BASE_DIR / ".env")

from mcp_server import service  # noqa: E402


def print_locks(order_id: str | None, only_active: bool) -> None:
    rows = service.list_locks(order_id)
    if only_active:
        rows = [r for r in rows if r["active"]]

    if not rows:
        print("没有符合条件的退款锁记录")
        return

    print(f"共 {len(rows)} 条退款锁记录\n")
    for r in rows:
        state = "生效中" if r["active"] else "已解锁"
        print(f"{r['order_id']}  [{state}]")
        print(f"  驳回时间: {r['locked_at']}")
        print(f"  关联追踪: {r['trace_id']}")
        print(f"  驳回说明: {r['reason']}")
        if not r["active"]:
            print(f"  解锁时间: {r['unlocked_at']}  操作人: {r['unlocked_by']}")
            print(f"  解锁备注: {r['unlock_note']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="退款驳回锁的人工解锁入口")
    parser.add_argument("order_id", nargs="?", help="要解锁的订单号")
    parser.add_argument("--list", action="store_true", help="列出全部锁记录")
    parser.add_argument("--active", action="store_true", help="只看生效中的锁")
    parser.add_argument("--by", default="运营人工", help="操作人，写进审计留痕")
    parser.add_argument("--note", default="", help="解锁原因，写进审计留痕")
    parser.add_argument("--yes", action="store_true", help="跳过确认")
    args = parser.parse_args()

    if args.list or args.active or not args.order_id:
        print_locks(args.order_id, args.active)
        if not args.order_id:
            return

    if not args.order_id:
        return

    if not args.yes:
        try:
            answer = input(f"确认解锁 {args.order_id} 的退款锁？(y/N) > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in {"y", "yes", "是"}:
            print("已取消")
            return

    try:
        result = service.unlock_refund(args.order_id, operator=args.by, note=args.note)
    except service.BizError as exc:
        print(f"解锁失败: {exc.message}")
        return

    if result["changed"]:
        print(f"[已解锁] {result['message']}")
        print(f"  原驳回说明: {result['previous_reason']}")
        print(f"  操作人    : {result['unlocked_by']}")
        print(f"  订单状态  : {result['order']['status_label']}")
    else:
        print(f"[无需操作] {result['message']}")


if __name__ == "__main__":
    main()

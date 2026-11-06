"""生成测试数据：4 个账号、9 笔订单、若干退款记录，写入 JSON 文件。

用法:
    python -m data.seed          # 重建全部数据文件

演示账号：
    zhangwei  / 123456     张伟，顾客
    lina      / 123456     李娜，顾客
    wangqiang / 123456     王强，顾客
    merchant  / admin123   陈明，商家（后台审批用）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from mcp_server import store  # noqa: E402

# 演示账号密码。生产环境请通过环境变量覆盖。
CUSTOMER_PASSWORD = os.getenv("DEMO_CUSTOMER_PASSWORD", "123456")
MERCHANT_PASSWORD = os.getenv("DEMO_MERCHANT_PASSWORD", "admin123")


def _user(user_id, username, name, role, password):
    return {
        "user_id": user_id,
        "username": username,
        "name": name,
        "role": role,
        "password_hash": store.hash_password(password),
        "created_at": "2026-09-01 09:00:00",
    }


USERS = [
    _user("u_1001", "zhangwei", "张伟", "customer", CUSTOMER_PASSWORD),
    _user("u_1002", "lina", "李娜", "customer", CUSTOMER_PASSWORD),
    _user("u_1003", "wangqiang", "王强", "customer", CUSTOMER_PASSWORD),
    _user("m_2001", "merchant", "陈明", "merchant", MERCHANT_PASSWORD),
]

ORDERS = [
    # ---------- 张伟：覆盖各种组合 ----------
    dict(
        order_id="ORD20261028001", owner_id="u_1001", owner_name="张伟",
        item_name="索尼 WH-1000XM5 头戴式降噪耳机", item_desc="黑色 / 国行 / 质保一年",
        amount_cents=249900, status="PAID",
        created_at="2026-10-28 20:14:06", paid_at="2026-10-28 20:15:33",
        shipped_at=None, delivered_at=None,
        cancelled_at=None, cancel_reason=None,
        refunded_cents=0, refunded_at=None,
        tracking_no=None, updated_at="2026-10-28 20:15:33",
    ),
    dict(
        order_id="ORD20261015002", owner_id="u_1001", owner_name="张伟",
        item_name="罗技 MX Master 3S 无线鼠标", item_desc="石墨黑 / 8000DPI",
        amount_cents=69900, status="CANCELLED",
        created_at="2026-10-15 09:02:11", paid_at="2026-10-15 09:03:40",
        shipped_at=None, delivered_at=None,
        cancelled_at="2026-10-16 11:20:08", cancel_reason="NO_LONGER_NEEDED",
        refunded_cents=0, refunded_at=None,
        tracking_no=None, updated_at="2026-10-16 11:20:08",
    ),
    dict(
        order_id="ORD20261008009", owner_id="u_1001", owner_name="张伟",
        item_name="罗技 G Pro X 2 无线游戏耳机", item_desc="白色 / 7.1 声道",
        amount_cents=109900, status="CANCELLED",
        created_at="2026-10-08 15:40:22", paid_at="2026-10-08 15:41:05",
        shipped_at="2026-10-09 09:30:22", delivered_at=None,
        cancelled_at="2026-10-11 10:05:00", cancel_reason="WRONG_ITEM",
        refunded_cents=0, refunded_at=None,
        tracking_no="JD0098771234", updated_at="2026-10-11 10:05:00",
    ),
    dict(
        order_id="ORD20260920003", owner_id="u_1001", owner_name="张伟",
        item_name="Apple AirPods Pro 2 无线降噪耳机", item_desc="USB-C 版 / 国行",
        amount_cents=189900, status="REFUNDED",
        created_at="2026-09-20 21:03:14", paid_at="2026-09-20 21:04:02",
        shipped_at="2026-09-21 10:12:00", delivered_at="2026-09-23 14:22:10",
        cancelled_at="2026-09-24 09:00:00", cancel_reason="OTHER",
        refunded_cents=189900, refunded_at="2026-09-25 16:30:44",
        tracking_no="SF1234567890", updated_at="2026-09-25 16:30:44",
    ),
    dict(
        order_id="ORD20261101004", owner_id="u_1001", owner_name="张伟",
        item_name="小米移动电源 3 20000mAh", item_desc="白色 / 双向快充",
        amount_cents=12900, status="CREATED",
        created_at="2026-11-01 12:30:00", paid_at=None,
        shipped_at=None, delivered_at=None,
        cancelled_at=None, cancel_reason=None,
        refunded_cents=0, refunded_at=None,
        tracking_no=None, updated_at="2026-11-01 12:30:00",
    ),
    dict(
        order_id="ORD20261102005", owner_id="u_1001", owner_name="张伟",
        item_name="CHERRY MX Board 3.0S 机械键盘", item_desc="红轴 / 108 键 / 白色",
        amount_cents=45800, status="COMPLETED",
        created_at="2026-10-20 19:00:00", paid_at="2026-10-20 19:01:12",
        shipped_at="2026-10-21 09:00:00", delivered_at="2026-10-23 11:45:00",
        cancelled_at=None, cancel_reason=None,
        refunded_cents=0, refunded_at=None,
        tracking_no="YT7788990011", updated_at="2026-10-23 11:45:00",
    ),
    # ---------- 李娜：越权测试 + 部分退款演示 ----------
    dict(
        order_id="ORD20261103006", owner_id="u_1002", owner_name="李娜",
        item_name="戴森 Supersonic HD15 吹风机", item_desc="紫红色 / 礼盒装",
        amount_cents=299900, status="PAID",
        created_at="2026-11-03 10:00:00", paid_at="2026-11-03 10:02:00",
        shipped_at="2026-11-04 08:00:00", delivered_at=None,
        cancelled_at=None, cancel_reason=None,
        refunded_cents=0, refunded_at=None,
        tracking_no="SF9988776655", updated_at="2026-11-04 08:00:00",
    ),
    dict(
        order_id="ORD20261010007", owner_id="u_1002", owner_name="李娜",
        item_name="Apple iPad Air 11 英寸 (M3)", item_desc="128GB / 深空灰 / WiFi",
        amount_cents=479900, status="COMPLETED",
        created_at="2026-10-10 14:20:00", paid_at="2026-10-10 14:21:30",
        shipped_at="2026-10-11 09:00:00", delivered_at="2026-10-13 16:10:00",
        cancelled_at=None, cancel_reason=None,
        refunded_cents=100000, refunded_at="2026-10-28 11:00:00",
        tracking_no="JD5566778899", updated_at="2026-10-28 11:00:00",
    ),
    # ---------- 王强：越权测试 ----------
    dict(
        order_id="ORD20261104008", owner_id="u_1003", owner_name="王强",
        item_name="德龙 ECAM22.110 全自动咖啡机", item_desc="银色 / 家用",
        amount_cents=268000, status="PAID",
        created_at="2026-11-04 11:15:00", paid_at="2026-11-04 11:16:40",
        shipped_at=None, delivered_at=None,
        cancelled_at=None, cancel_reason=None,
        refunded_cents=0, refunded_at=None,
        tracking_no=None, updated_at="2026-11-04 11:16:40",
    ),
]

REFUNDS = [
    # 历史：AirPods 全额退款已通过
    dict(
        refund_id="RF20260925163044A1B2", order_id="ORD20260920003",
        user_id="u_1001", user_name="张伟", amount_cents=189900,
        reason="商品与描述不符，申请退货退款", status="APPROVED", mode="MANUAL",
        seq=1, created_at="2026-09-24 09:05:00", reviewed_at="2026-09-25 16:30:44",
        reviewer_id="m_2001", reviewer_name="陈明", review_note="已核验退货入库",
    ),
    # 历史：iPad 部分退款 ¥1000 已通过
    dict(
        refund_id="RF20261028110000C3D4", order_id="ORD20261010007",
        user_id="u_1002", user_name="李娜", amount_cents=100000,
        reason="屏幕有细微划痕，协商部分补偿", status="APPROVED", mode="MANUAL",
        seq=2, created_at="2026-10-27 20:00:00", reviewed_at="2026-10-28 11:00:00",
        reviewer_id="m_2001", reviewer_name="陈明", review_note="协商退 1000 元",
    ),
    # 待审批：iPad 再次申请 ¥500，后台一打开就能看到
    dict(
        refund_id="RF20261106093000E5F6", order_id="ORD20261010007",
        user_id="u_1002", user_name="李娜", amount_cents=50000,
        reason="触控偶尔失灵，希望再退 500 元", status="PENDING", mode="MANUAL",
        seq=3, created_at="2026-11-06 09:30:00", reviewed_at=None,
        reviewer_id=None, reviewer_name=None, review_note=None,
    ),
]


def seed(target_dir: Path | None = None) -> Path:
    """重建数据目录，返回目录路径。"""
    directory = Path(target_dir) if target_dir else store.data_dir()
    directory.mkdir(parents=True, exist_ok=True)

    for name in (store.USERS_FILE, store.ORDERS_FILE, store.REFUNDS_FILE):
        for suffix in ("", ".lock"):
            p = directory / (name + suffix)
            if p.exists():
                p.unlink()

    users = store.empty_users()
    for u in USERS:
        users["users"][u["user_id"]] = u

    orders = store.empty_orders()
    for o in ORDERS:
        orders["orders"][o["order_id"]] = o

    refunds = store.empty_refunds()
    for r in REFUNDS:
        refunds["refunds"][r["refund_id"]] = r
        refunds["seq"] = max(refunds["seq"], r["seq"])

    store._write_atomic(directory / store.USERS_FILE, users)
    store._write_atomic(directory / store.ORDERS_FILE, orders)
    store._write_atomic(directory / store.REFUNDS_FILE, refunds)
    return directory


def main() -> None:
    directory = seed()
    print(f"数据已重建: {directory}")
    print(f"  用户   {len(USERS)} 个（3 顾客 + 1 商家）")
    print(f"  订单   {len(ORDERS)} 笔")
    print(f"  退款   {len(REFUNDS)} 条（其中 1 条待审批）")
    print()
    print("  演示账号：")
    print(f"    zhangwei  / {CUSTOMER_PASSWORD}   张伟（顾客）")
    print(f"    lina      / {CUSTOMER_PASSWORD}   李娜（顾客）")
    print(f"    wangqiang / {CUSTOMER_PASSWORD}   王强（顾客）")
    print(f"    merchant  / {MERCHANT_PASSWORD}   陈明（商家，后台审批）")


if __name__ == "__main__":
    main()

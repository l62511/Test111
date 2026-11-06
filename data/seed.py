"""生成测试数据：3 个用户、9 笔订单，覆盖全部订单状态与发货状态。

用法:
    python -m data.seed          # 重建数据库
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / "data" / "orders.db"
SCHEMA_PATH = BASE_DIR / "data" / "schema.sql"

USERS = {
    "u_1001": "张伟",
    "u_1002": "李娜",
    "u_1003": "王强",
}

ORDERS = [
    # ---------- u_1001 张伟：主测试账号，覆盖各种组合 ----------
    dict(
        order_id="ORD20261028001", owner_id="u_1001", owner_name="张伟",
        item_name="索尼 WH-1000XM5 头戴式降噪耳机", item_desc="黑色 / 国行 / 质保一年",
        amount_cents=249900, status="PAID",
        created_at="2026-10-28 20:14:06", paid_at="2026-10-28 20:15:33",
        shipped_at=None,  # 未发货，取消后可直接退款
        cancelled_at=None, refunded_at=None, cancel_reason=None,
        refund_cents=None, tracking_no=None,
    ),
    dict(
        order_id="ORD20261015002", owner_id="u_1001", owner_name="张伟",
        item_name="罗技 MX Master 3S 无线鼠标", item_desc="石墨黑 / 8000DPI",
        amount_cents=69900, status="CANCELLED",
        created_at="2026-10-15 09:02:11", paid_at="2026-10-15 09:03:40",
        shipped_at=None,  # 未发货就取消了，退款无需审批
        cancelled_at="2026-10-16 11:20:08", refunded_at=None,
        cancel_reason="NO_LONGER_NEEDED", refund_cents=None, tracking_no=None,
    ),
    dict(
        order_id="ORD20261008009", owner_id="u_1001", owner_name="张伟",
        item_name="罗技 G Pro X 2 无线游戏耳机", item_desc="白色 / 7.1 声道",
        amount_cents=109900, status="CANCELLED",
        created_at="2026-10-08 19:40:02", paid_at="2026-10-08 19:41:15",
        shipped_at="2026-10-09 09:30:22",  # 已发货后取消，退款需人工审批
        cancelled_at="2026-10-11 14:02:36", refunded_at=None,
        cancel_reason="WRONG_ITEM", refund_cents=None, tracking_no="JD0098771234",
    ),
    dict(
        order_id="ORD20260920003", owner_id="u_1001", owner_name="张伟",
        item_name="Apple AirPods Pro 2 无线降噪耳机", item_desc="USB-C 版",
        amount_cents=189900, status="REFUNDED",
        created_at="2026-09-20 15:40:22", paid_at="2026-09-20 15:41:05",
        shipped_at="2026-09-21 10:12:40",
        cancelled_at="2026-09-22 10:05:17", refunded_at="2026-09-25 16:30:44",
        cancel_reason="WRONG_ITEM", refund_cents=189900, tracking_no="SF1567230988310",
    ),
    dict(
        order_id="ORD20261101004", owner_id="u_1001", owner_name="张伟",
        item_name="小米移动电源 3 20000mAh", item_desc="18W 双向快充",
        amount_cents=12900, status="CREATED",
        created_at="2026-11-01 22:31:50", paid_at=None,
        shipped_at=None, cancelled_at=None, refunded_at=None,
        cancel_reason=None, refund_cents=None, tracking_no=None,
    ),
    dict(
        order_id="ORD20261102005", owner_id="u_1001", owner_name="张伟",
        item_name="CHERRY MX Board 3.0S 机械键盘", item_desc="红轴 / 108 键",
        amount_cents=45800, status="COMPLETED",
        created_at="2026-11-02 13:11:29", paid_at="2026-11-02 13:12:03",
        shipped_at="2026-11-02 18:20:11",
        cancelled_at=None, refunded_at=None, cancel_reason=None,
        refund_cents=None, tracking_no="SF1567230988421",
    ),

    # ---------- u_1002 李娜：验证越权访问必须被拒绝 ----------
    dict(
        order_id="ORD20261103006", owner_id="u_1002", owner_name="李娜",
        item_name="戴森 Supersonic HD15 吹风机", item_desc="紫红色 / 官翻",
        amount_cents=299900, status="PAID",
        created_at="2026-11-03 10:22:14", paid_at="2026-11-03 10:23:31",
        shipped_at="2026-11-03 20:05:48",
        cancelled_at=None, refunded_at=None, cancel_reason=None,
        refund_cents=None, tracking_no="YT7788990011",
    ),
    dict(
        order_id="ORD20261010007", owner_id="u_1002", owner_name="李娜",
        item_name="Apple iPad Air 11 英寸 (M3)", item_desc="128GB / 深空灰 / WiFi",
        amount_cents=479900, status="CANCELLED",
        created_at="2026-10-10 18:05:39", paid_at="2026-10-10 18:06:22",
        shipped_at=None,
        cancelled_at="2026-10-11 09:14:02", refunded_at=None,
        cancel_reason="PRICE_ISSUE", refund_cents=None, tracking_no=None,
    ),

    # ---------- u_1003 王强 ----------
    dict(
        order_id="ORD20261104008", owner_id="u_1003", owner_name="王强",
        item_name="德龙 ECAM22.110 全自动咖啡机", item_desc="黑色 / 15Bar",
        amount_cents=129900, status="PAID",
        created_at="2026-11-04 16:48:57", paid_at="2026-11-04 16:50:10",
        shipped_at=None, cancelled_at=None, refunded_at=None,
        cancel_reason=None, refund_cents=None, tracking_no=None,
    ),
]

HISTORY_LOG = [
    ("ORD20261015002", "cancel", "u_1001", "用户主动取消", "2026-10-16 11:20:08"),
    ("ORD20261008009", "cancel", "u_1001", "用户主动取消", "2026-10-11 14:02:36"),
    ("ORD20260920003", "cancel", "u_1001", "用户主动取消", "2026-09-22 10:05:17"),
    ("ORD20260920003", "refund", "u_1001", "全额退款", "2026-09-25 16:30:44"),
    ("ORD20261010007", "cancel", "u_1002", "用户主动取消", "2026-10-11 09:14:02"),
]

# 历史退款也要留 trace，保持「有退款必有追踪记录」
HISTORY_TRACE = [
    dict(
        trace_id="tr_20260925_0001", order_id="ORD20260920003", caller_id="u_1001",
        amount_cents=189900, reason="商品与描述不符，申请退款",
        shipped=1, shipping_state="SHIPPED", approval_mode="MANUAL",
        approval_status="APPROVED", approver="客服主管-陈明", outcome="SUCCESS",
        note="已发货件，人工核验退货入库后放款",
        created_at="2026-09-22 10:06:11", updated_at="2026-09-25 16:30:44",
    ),
]


def seed(db_path: Path = DB_PATH) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

    columns = [
        "order_id", "owner_id", "owner_name", "item_name", "item_desc",
        "amount_cents", "status", "created_at", "paid_at", "shipped_at",
        "cancelled_at", "refunded_at", "cancel_reason", "refund_cents", "tracking_no",
    ]
    conn.executemany(
        f"INSERT INTO orders ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})",
        [tuple(o[c] for c in columns) for o in ORDERS],
    )

    conn.executemany(
        "INSERT INTO operation_log (order_id, action, actor_id, detail, created_at)"
        " VALUES (?,?,?,?,?)",
        HISTORY_LOG,
    )

    trace_cols = list(HISTORY_TRACE[0].keys())
    conn.executemany(
        f"INSERT INTO refund_trace ({', '.join(trace_cols)})"
        f" VALUES ({', '.join('?' * len(trace_cols))})",
        [tuple(t[c] for c in trace_cols) for t in HISTORY_TRACE],
    )

    conn.commit()
    conn.close()
    return db_path


if __name__ == "__main__":
    path = seed()
    conn = sqlite3.connect(path)
    rows = conn.execute(
        "SELECT status, COUNT(*), SUM(amount_cents), SUM(shipped_at IS NOT NULL)"
        " FROM orders GROUP BY status ORDER BY status"
    ).fetchall()
    total = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    conn.close()
    print(f"数据库已重建: {path}  共 {total} 笔订单")
    print(f"  {'状态':<12}{'笔数':<6}{'金额合计':<14}{'其中已发货'}")
    for status, count, amount, shipped in rows:
        print(f"  {status:<12}{count:<6}¥{amount / 100:>10,.2f}    {shipped}")

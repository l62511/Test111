-- 订单客服 Agent 数据层
-- 金额统一以「分」为单位存储，避免浮点误差

CREATE TABLE IF NOT EXISTS orders (
    order_id      TEXT PRIMARY KEY,
    owner_id      TEXT    NOT NULL,
    owner_name    TEXT    NOT NULL,
    item_name     TEXT    NOT NULL,
    item_desc     TEXT,
    amount_cents  INTEGER NOT NULL CHECK (amount_cents > 0),
    status        TEXT    NOT NULL
                  CHECK (status IN ('CREATED','PAID','CANCELLED','REFUNDED','COMPLETED')),
    created_at    TEXT    NOT NULL,
    paid_at       TEXT,
    -- 发货时间：非空即表示货物已出库。退款是否需人工审批由它决定。
    shipped_at    TEXT,
    cancelled_at  TEXT,
    refunded_at   TEXT,
    cancel_reason TEXT,
    refund_cents  INTEGER,
    tracking_no   TEXT
);

CREATE INDEX IF NOT EXISTS idx_orders_owner   ON orders(owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_orders_status  ON orders(owner_id, status);

-- 操作流水：UNIQUE(order_id, action) 是「只能取消一次 / 只能退款一次」的兜底约束，
-- 由数据库层面拒绝重复写，不依赖应用层判断。
CREATE TABLE IF NOT EXISTS operation_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id   TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('cancel','refund')),
    actor_id   TEXT NOT NULL,
    detail     TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (order_id, action)
);

-- 退款追踪链路。
-- 每笔退款一条记录，完整记下退款原因、发货状态、是否走人工审批、审批人、最终结果，
-- 供事后追溯「这笔钱为什么退了 / 谁批的」。
CREATE TABLE IF NOT EXISTS refund_trace (
    trace_id        TEXT PRIMARY KEY,
    order_id        TEXT    NOT NULL,
    caller_id       TEXT    NOT NULL,
    amount_cents    INTEGER NOT NULL,
    reason          TEXT,
    shipped         INTEGER NOT NULL DEFAULT 0,
    shipping_state  TEXT    NOT NULL CHECK (shipping_state IN ('SHIPPED','NOT_SHIPPED')),
    approval_mode   TEXT    NOT NULL CHECK (approval_mode IN ('AUTO','MANUAL')),
    approval_status TEXT    NOT NULL
                    CHECK (approval_status IN ('NOT_REQUIRED','PENDING','APPROVED','REJECTED')),
    approver        TEXT,
    outcome         TEXT    NOT NULL CHECK (outcome IN ('PENDING','SUCCESS','REJECTED')),
    note            TEXT,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trace_order ON refund_trace(order_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_trace_state ON refund_trace(approval_status, outcome);

-- 退款驳回锁。
-- 人工驳回一次后，该订单进入锁定态，用户不能自行再次申请退款，
-- 必须由运营人工解锁（scripts/unlock_refund.py）才能重走流程。
-- 用独立表而不是给 refund_trace 加字段：trace 只记录历史事实，不该被后续动作改写；
-- 一张锁表多条记录，可以让「驳回 -> 解锁 -> 再驳回 -> 再解锁」的全过程都留痕。
CREATE TABLE IF NOT EXISTS refund_lock (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id    TEXT NOT NULL,
    trace_id    TEXT,
    locked_at   TEXT NOT NULL,
    reason      TEXT,
    unlocked_at TEXT,
    unlocked_by TEXT,
    unlock_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_lock_order ON refund_lock(order_id, locked_at DESC);

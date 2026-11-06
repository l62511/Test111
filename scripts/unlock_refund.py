"""运营人工解锁——解除某订单的退款驳回锁。

退款申请被商家驳回后，该订单进入锁定态，用户无法自行再次申请。
运营复核通过后，用本脚本解锁，用户才能重走退款流程。

用法:
    python scripts/unlock_refund.py --list                          # 查看所有锁
    python scripts/unlock_refund.py ORD20261008009 --note 复核通过   # 解锁
    python scripts/unlock_refund.py ORD20261008009 --by m_2001 --yes
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

DEFAULT_OPERATOR = "m_2001"  # 陈明，商家


def print_locks(order_id: str | None = None) -> None:
    rows = service.list_locks()
    if order_id:
        rows = [r for r in rows if r["order_id"] == order_id.strip().upper()]

    if not rows:
        print("当前没有生效中的退款锁")
        return

    print(f"共 {len(rows)} 条生效中的退款锁\n")
    for r in rows:
        print(f"{r['order_id']}  [锁定中]")
        print(f"  锁定时间: {r['locked_at']}")
        print(f"  关联申请: {r['refund_id']}")
        print(f"  驳回说明: {r['reason']}")
        print(f"  锁定人  : {r['locked_by']}")
        print()


def main() -> None:
    parser = argparse.ArgumentParser(description="退款驳回锁的人工解锁入口")
    parser.add_argument("order_id", nargs="?", help="要解锁的订单号")
    parser.add_argument("--list", action="store_true", help="列出当前生效中的锁")
    parser.add_argument("--by", default=DEFAULT_OPERATOR,
                        help="操作人的 user_id，需为商家账号（默认 m_2001）")
    parser.add_argument("--note", default="", help="解锁原因，写进审计留痕")
    parser.add_argument("--yes", action="store_true", help="跳过确认")
    args = parser.parse_args()

    if args.list or not args.order_id:
        print_locks(args.order_id)
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
        result = service.unlock_order(args.order_id, args.by, note=args.note)
    except service.BizError as exc:
        print(f"解锁失败: {exc.message}")
        return

    if result["changed"]:
        print(f"[已解锁] {result['message']}")
    else:
        print(f"[无需操作] {result['message']}")


if __name__ == "__main__":
    main()

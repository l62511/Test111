"""订单客服 MCP Server。

三个工具通过 MCP 协议暴露，调用方身份在进程启动时由环境变量 CALLER_ID 注入，
不出现在任何工具的入参 schema 中——模型看不见 user_id，也就无法伪造。

启动:
     python -m mcp_server.server                      # stdio（由 Agent 拉起）
     CALLER_ID=u_1001 python -m mcp_server.server
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from pydantic import Field

try:  # 以包方式运行
    from . import service
except ImportError:  # 直接运行脚本
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import service  # type: ignore[no-redef]

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

mcp = FastMCP("order-service", log_level=os.getenv("ORDER_SERVICE_LOG_LEVEL", "WARNING"))

_caller: dict[str, str] | None = None


def current_caller() -> dict[str, str]:
    """进程级身份，只在首次调用时解析并缓存。"""
    global _caller
    if _caller is None:
        caller_id = os.getenv("CALLER_ID", "").strip()
        if not caller_id:
            raise RuntimeError(
                "CALLER_ID 未注入。MCP Server 必须由 Agent 以已认证身份启动，"
                "拒绝匿名调用。"
            )
        _caller = service.resolve_caller(caller_id)
    return _caller


def _guard(fn, *args, **kwargs) -> dict[str, Any]:
    """业务异常统一转成结构化结果，不污染 MCP 协议层。"""
    try:
        return fn(*args, **kwargs)
    except service.BizError as exc:
        return {
            "ok": False,
            "error_code": exc.code,
            "message": exc.message,
            "hint": exc.hint,
            **exc.extra,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error_code": "INTERNAL_ERROR",
            "message": f"服务内部错误: {exc}",
            "hint": "请稍后重试，不要向用户暴露技术细节",
        }


# ==========================================================================
# 工具 1：查询订单（只读）
# ==========================================================================
QUERY_DESC = """\
查询当前登录用户本人的订单，只读操作，不会修改任何数据。

两种用法：
- 传 order_id：精确查询单笔订单；
- 传 status 或不传参数：列出本人的订单列表（默认最近 10 笔）。

返回订单号、商品、金额、状态、各节点时间、物流单号，以及判断后续操作所需的派生字段：
- can_cancel / can_refund：能否取消、能否发起退款；
- shipping_state：NOT_SHIPPED 未发货 / SHIPPED 已发货 / DELIVERED 已签收；
- refund_requires_approval：退款是否需要商家审批（已发货或已签收才需要）；
- refunded_amount：该订单历史已退金额；refundable_balance：当前还能退的余额。
  这两个字段是累计退款的依据——再次退款时金额不能超过 refundable_balance；
- pending_refund：若不为 null，表示已有一笔待商家审批的申请，此时不要再提交；
- refund_locked：该订单退款申请被商家驳回后是否处于锁定状态。
查询范围被服务端强制限制为当前登录用户，无法查出他人订单。

【不要调用本工具的场景】
1. 用户在咨询通用政策（退款多久到账、能否开发票、配送范围等）——直接回答，无需查库。
2. 用户没有提供任何可用于定位的条件——先追问订单号或状态，不要发起空查询。
3. 用户要求查询他人订单（"帮我查下我朋友的订单""查一下张伟那一单"）——本工具只能
   查当前用户，直接说明无此权限，不要调用。
4. 本轮对话中刚刚查过同一订单、且期间未执行过取消/退款——复用已有结果，不要重复调用。"""


@mcp.tool(
    name="query_order",
    title="查询订单",
    description=QUERY_DESC,
    annotations=ToolAnnotations(
        title="查询订单",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def query_order(
    order_id: Annotated[
        str | None,
        Field(description="订单号，例如 ORD20261028001。精确查询时必填；为 None 时按条件列出订单列表。"),
    ] = None,
    status: Annotated[
        Literal["CREATED", "PAID", "CANCELLED", "REFUNDED", "COMPLETED"] | None,
        Field(description="按状态筛选：CREATED 待支付 / PAID 已支付 / CANCELLED 已取消 / REFUNDED 已退款 / COMPLETED 已完成。"),
    ] = None,
    limit: Annotated[
        int,
        Field(ge=1, le=20, description="列表模式返回的最大条数，默认 10，上限 20。"),
    ] = 10,
) -> dict[str, Any]:
    caller = current_caller()
    return _guard(
        service.query_orders,
        caller["caller_id"],
        order_id=order_id,
        status=status,
        limit=limit,
    )


# ==========================================================================
# 工具 2：取消订单（写）
# ==========================================================================
CANCEL_DESC = """\
取消当前登录用户本人的一笔订单。写操作，执行后不可撤销。

前置条件（不满足时返回错误码，不会抛异常）：
- 订单必须属于当前用户，否则返回 ORDER_NOT_FOUND；
- 状态必须是 CREATED(待支付) 或 PAID(已支付)，否则返回 INVALID_STATE；
- 每笔订单只能取消一次，重复取消返回 ALREADY_CANCELLED；
- COMPLETED(已完成) 订单不可取消。

成功后订单状态变为 CANCELLED，并写入操作流水。如用户还需要退款，之后可再调用 refund_order。

【不要调用本工具的场景】
1. 用户没有给出订单号——必须先追问，严禁猜测、补全或编造 order_id。
2. 用户只是在咨询（"能取消吗""取消要多久""现在取消还来得及吗"）——这是询问而非
   指令，先答复并确认意图，不要直接执行。
3. 用户表述含糊（"我不想要了""这单有点问题"）——先澄清是否确实要取消，不要擅自
   执行不可逆操作。
4. 已知该订单不属于当前用户或不存在——直接告知查不到，不要调用本工具去"确认一下"。
5. 订单已是 CANCELLED / REFUNDED / COMPLETED——直接说明现状，不要调用后靠报错回复用户。"""


@mcp.tool(
    name="cancel_order",
    title="取消订单",
    description=CANCEL_DESC,
    annotations=ToolAnnotations(
        title="取消订单",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def cancel_order(
    order_id: Annotated[
        str,
        Field(
            min_length=1,
            description="要取消的订单号，必填，例如 ORD20261028001。必须来自用户明确提供或查询结果，不得编造。",
        ),
    ],
    reason: Annotated[
        Literal["NO_LONGER_NEEDED", "WRONG_ITEM", "PRICE_ISSUE", "OTHER"] | None,
        Field(description="取消原因，可选。NO_LONGER_NEEDED 不想要了 / WRONG_ITEM 拍错了 / PRICE_ISSUE 价格问题 / OTHER 其他。"),
    ] = None,
) -> dict[str, Any]:
    caller = current_caller()
    return _guard(
        service.cancel_order,
        caller["caller_id"],
        order_id=order_id,
        reason=reason,
    )


# ==========================================================================
# 工具 3：退款（写）
# ==========================================================================
REFUND_DESC = """\
对当前登录用户本人的订单发起退款。写操作。

工具会根据订单状态自动选择路径，返回值里的 result 字段说明走了哪条：
- 未发货且已取消 → result=REFUNDED，立即退款，无需审批，钱马上退。
- 已发货或已签收   → result=PENDING，生成退款申请进入商家审批队列，返回 queue_position
  （排队位置）。此时钱还没有退，必须告诉用户「已提交，等待商家审核」。

金额规则（很关键，务必理解）：
- 支持分次退款。订单上记录着已退金额 refunded_cents，本次退款会与它累加，
  「已退 + 本次」绝不能超过订单实付金额。
- 省略 amount 表示退剩余全部余额，不是退全额。查询结果里 refundable_balance
  就是当前还能退的钱。
- 金额超出余额会返回 AMOUNT_EXCEEDED，hint 里给出本次最多可退多少。

前置条件（不满足时返回错误码，不会抛异常）：
- 订单必须属于当前用户，否则 ORDER_NOT_FOUND；
- 状态必须是 CANCELLED(已取消) 或 COMPLETED(已完成)，其它状态返回 ORDER_NOT_REFUNDABLE；
- 已全额退完返回 ALREADY_REFUNDED；
- 已有一笔待审批申请返回 PENDING_REFUND_EXISTS，此时不要重复提交；
- 该订单退款申请被商家驳回后会被锁定，返回 REFUND_LOCKED_AFTER_REJECT。

退款原因 reason 会进入商家审批列表，供商家判断是否放款。请用用户自己的说法归纳，
不要编造，也不要为了凑原因反复追问。

【不要调用本工具的场景】
1. 订单是 PAID 或 CREATED 状态——PAID 需先取消，CREATED 还没付款。不要跳过这些步骤。
2. 用户还没取消就说"我要退款"，且订单未发货——先引导取消，取消后未发货可直接退。
3. 该订单已全额退完——直接告知已退金额和到账时间，不要重复调用。
4. 用户在咨询退款政策（到账时间、能否退到其他账户）——直接回答，不调用工具。
5. 用户要求退到非原支付账户，或要求退款金额高于可退余额——说明规则限制，不要尝试。
6. 订单不属于当前用户——直接拒绝，不要调用。
7. 查询结果里 refund_locked 为 true——该退款被商家驳回后已锁定。不要调用，
   告知用户需联系人工客服复核，由商家解锁后才能重新申请。换理由、改金额都不行。
8. 查询结果里 pending_refund 不为空——已有一笔在审核中。告知用户正在审核、预计等待，
   不要重复提交。"""


@mcp.tool(
    name="refund_order",
    title="订单退款",
    description=REFUND_DESC,
    annotations=ToolAnnotations(
        title="订单退款",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def refund_order(
    order_id: Annotated[
        str,
        Field(
            min_length=1,
            description="要退款的订单号，必填。该订单必须已取消或已完成。不得编造订单号。",
        ),
    ],
    amount: Annotated[
        float | None,
        Field(
            gt=0,
            description="本次退款金额（元）。省略表示退剩余全部余额；传入时与历史已退金额累加后，不得超过订单实付金额。",
        ),
    ] = None,
    reason: Annotated[
        str | None,
        Field(
            description="退款原因，会展示给审批的商家。尽量用用户原话归纳，例如“商品与描述不符”“收到时已破损”。",
        ),
    ] = None,
) -> dict[str, Any]:
    caller = current_caller()
    return _guard(
        service.refund_order,
        caller["caller_id"],
        order_id=order_id,
        amount=amount,
        reason=reason,
    )


def main() -> None:
    # 启动即校验身份，避免带着错误配置跑起来
    try:
        caller = current_caller()
    except Exception as exc:  # noqa: BLE001
        print(f"[order-service] 启动失败: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    if os.getenv("ORDER_SERVICE_DEBUG"):
        print(
            f"[order-service] 已启动，调用方 {caller['caller_name']}({caller['caller_id']})",
            file=sys.stderr,
        )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

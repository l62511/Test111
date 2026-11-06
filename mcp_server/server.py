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

返回订单号、商品、金额、状态、各节点时间、物流单号，以及几个判断依据：
- can_cancel / can_refund：能否取消、能否退款；
- shipping_state：SHIPPED 已发货 / NOT_SHIPPED 未发货；
- refund_needs_approval：该订单退款是否需要人工审批（已发货才需要）；
- refund_locked：该订单的退款申请是否曾被人工驳回并锁定（锁定后不可再自行申请）。
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
对当前登录用户本人的【已取消】订单发起退款。写操作，执行后不可撤销。

退款是否放款取决于订单是否已发货，这也是本工具的核心规则：
- 未发货：直接按金额退款，无需审批，一次调用即可完成。
- 已发货：第一次调用只生成审批单、不退款，返回 MANUAL_APPROVAL_REQUIRED 和 approval_id；
  人工审批通过后，需要带 approval_id 第二次调用，才真正放款。

前置条件（不满足时返回错误码，不会抛异常）：
- 订单必须属于当前用户，否则返回 ORDER_NOT_FOUND；
- 状态必须是 CANCELLED(已取消)——未取消的订单必须先取消，否则返回 ORDER_NOT_CANCELLED；
- 每笔订单只能退款一次，重复退款返回 ALREADY_REFUNDED；
- 退款金额不得超实付金额，超出返回 AMOUNT_EXCEEDED；省略 amount 表示全额退款；
- 该订单若曾被人驳回退款申请，会进入锁定态，返回 REFUND_LOCKED_AFTER_REJECT，
  用户不能自行再申请，必须由运营人工解锁后才行。

退款原因 reason 会写入退款追踪记录(refund_trace)，用于事后追溯，请尽量填写用户给出的真实原因，
不要自己编造。不要为了凑原因反复追问，用户没说就用默认值。

【不要调用本工具的场景】
1. 订单不是已取消状态——尤其是 PAID 订单，必须先取消再退款，不要试图跳过取消步骤。
2. 用户还没取消就说"我要退款"——先引导用户确认取消，取消成功后再调用本工具。
3. 该订单已经退过款——直接告知退款时间和到账周期，不要重复调用。
4. 用户在咨询退款政策（到账时间、退款方式、能否退到其他账户）——直接回答，不调用工具。
5. 用户要求退到非原支付账户，或要求退款金额高于实付金额——说明规则限制，不要尝试调用。
6. 订单不属于当前用户——直接拒绝，不要调用。
7. 已经拿到 approval_id、但用户明确拒绝了审批——不要再次调用，如实告知操作已取消。
8. 查询结果里 refund_locked 为 true——说明这笔退款被人工驳回后已锁定。不要调用本工具，
   直接告知用户需联系人工客服复核，由运营解锁后才能重新申请。换理由、换金额再试都不行。"""


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
            description="要退款的订单号，必填。该订单必须已处于已取消状态。不得编造订单号。",
        ),
    ],
    amount: Annotated[
        float | None,
        Field(
            gt=0,
            description="退款金额（元）。省略或为 null 表示全额退款；传入时不得超过该订单实付金额。已发货订单以审批单记录的金额为准。",
        ),
    ] = None,
    reason: Annotated[
        str | None,
        Field(
            description="退款原因，会写入退款追踪记录用于追溯。尽量用用户原话归纳，例如“商品与描述不符”“不想要了”。",
        ),
    ] = None,
    approval_id: Annotated[
        str | None,
        Field(
            description="人工审批凭证。无需主动填写：仅当上一次调用返回 MANUAL_APPROVAL_REQUIRED 时，把返回的 approval_id 原样回传，用于完成已发货订单的放款。",
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
        approval_id=approval_id,
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

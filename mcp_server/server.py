"""订单客服 MCP Server。

五个工具通过 MCP 协议暴露，分为只读与写入两类：

    query_order           查询本人订单（只读）
    query_after_sales     查询本人售后记录与历史（只读）
    query_sales_analytics 售后数据分析（只读，商家侧口径）
    cancel_order          取消订单（写入，需终端确认）
    submit_refund         提交退款（写入，金额由规则引擎裁决）

调用方身份在进程启动时由环境变量 CALLER_ID 注入，不出现在任何工具的入参
schema 中——模型看不见 user_id，也就无法伪造。

退款金额不在工具入参里由模型决定：submit_refund 只接收"用户主张多少"，
最终金额与走自动还是人工，全部由 mcp_server.rules 计算。

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
- refunded_amount：该订单历史已退金额；refundable_balance：当前还能退的余额。
  这两个字段是累计退款的依据——再退款时金额不能超过 refundable_balance；
- pending_refund：若不为 null，表示已有一笔待商家审批的申请，此时不要再提交；
- refund_locked：该订单退款申请被商家驳回后是否处于锁定状态。
查询范围被服务端强制限制为当前登录用户，无法查出他人订单。

【不要调用本工具的场景】
1. 用户在咨询通用政策（退款多久到账、能否开发票、配送范围等）——直接回答，无需查库。
2. 用户没有提供任何可用于定位的条件——先追问订单号或状态，不要发起空查询。
3. 用户要求查询他人订单（"帮我查下我朋友的订单""查一下张伟那一单"）——本工具只能
   查当前用户，直接说明无此权限，不要调用。
4. 本轮对话中刚刚查过同一订单、且期间未执行过取消/退款——复用已有结果，不要重复调用。
5. 用户问的是"我这单退款到哪一步了"——那是售后进度，用 query_after_sales 更合适。"""


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
# 工具 2：查询售后记录（只读）
# ==========================================================================
AFTER_SALES_DESC = """\
查询当前登录用户本人的售后情况，只读。回答"我上次退款到哪一步了""我这单退过几次"这类问题。

不传 order_id 时返回该用户全部售后记录与画像；传 order_id 只看这一单。
返回：
- records：每条售后申请的编号、金额、状态（PENDING 待审批 / APPROVED 已通过 /
  REJECTED 已驳回）、申请时间、审批人、审批备注、商品、原因分类；
- stats：申请总数、已通过数、待审批数、已驳回数、累计退款金额；
- profile：售后次数、近 30 天申请次数、最常见原因、是否属于高频退款用户。
其中 locked 为 true 表示该订单被驳回后锁定，不能重新申请。

【不要调用本工具的场景】
1. 用户只是想知道订单当前状态（待支付/已支付/已发货）——用 query_order。
2. 用户要发起新的退款——直接 submit_refund，不必先查售后记录；
   submit_refund 内部会自行校验是否有在途申请。
3. 用户问的是全平台售后统计（退款率、商品排行）——那是 query_sales_analytics，
   本工具只覆盖当前用户自己的记录。
4. 用户要查他人售后——无权限，直接说明，不要调用。"""


@mcp.tool(
    name="query_after_sales",
    title="查询售后记录",
    description=AFTER_SALES_DESC,
    annotations=ToolAnnotations(
        title="查询售后记录",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def query_after_sales(
    order_id: Annotated[
        str | None,
        Field(description="订单号。传了只看这一单的售后记录；为 None 时返回该用户全部售后记录与画像。"),
    ] = None,
) -> dict[str, Any]:
    caller = current_caller()
    return _guard(
        service.query_after_sales,
        caller["caller_id"],
        order_id=order_id,
    )


# ==========================================================================
# 工具 3：售后数据分析（只读）
# ==========================================================================
ANALYTICS_DESC = """\
查询售后经营数据，只读。回答"本周退款金额最高的商品是什么""退款率多少"
"哪些订单异常"这类统计问题。

period 时间范围（按申请提交时间算）：
- WEEK 本周（周一起） / MONTH 本月 / ALL 全部时间
dimension 分析维度：
- SUMMARY 全貌（默认，含金额、退款率、商品排行、原因分布、异常）
- PRODUCT 只看商品退款排行
- REASON  只看退款原因分布
- ANOMALY 只看异常订单
- TREND   只看近 14 天每日退款趋势
返回里的 text 字段已经是排好版的中文摘要，可直接据此组织回答。

【不要调用本工具的场景】
1. 顾客在问自己订单的事——那是 query_order / query_after_sales，不是经营数据。
2. 用户问的是单笔订单金额、单个退款进度——不需要聚合，别绕远路。
3. 时间范围不明确时不要连续试多个 period 反复调用，先按 ALL 查一次，
   再根据用户追问缩小范围。"""


@mcp.tool(
    name="query_sales_analytics",
    title="售后数据分析",
    description=ANALYTICS_DESC,
    annotations=ToolAnnotations(
        title="售后数据分析",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
)
def query_sales_analytics(
    period: Annotated[
        Literal["WEEK", "MONTH", "ALL"],
        Field(description="时间范围：WEEK 本周 / MONTH 本月 / ALL 全部。默认 ALL。"),
    ] = "ALL",
    dimension: Annotated[
        Literal["SUMMARY", "PRODUCT", "REASON", "ANOMALY", "TREND"],
        Field(description="分析维度：SUMMARY 全貌 / PRODUCT 商品排行 / REASON 原因分布 / ANOMALY 异常订单 / TREND 趋势。默认 SUMMARY。"),
    ] = "SUMMARY",
) -> dict[str, Any]:
    return _guard(service.sales_analytics, period=period, dimension=dimension)


# ==========================================================================
# 工具 4：取消订单（写）
# ==========================================================================
CANCEL_DESC = """\
取消当前登录用户本人的一笔订单。写操作，执行后不可撤销。

前置条件（不满足时返回错误码，不会抛异常）：
- 订单必须属于当前用户，否则返回 ORDER_NOT_FOUND；
- 状态必须是 CREATED(待支付) 或 PAID(已支付)，否则返回 INVALID_STATE；
- 每笔订单只能取消一次，重复取消返回 ALREADY_CANCELLED；
- COMPLETED(已完成) 订单不可取消。

成功后订单状态变为 CANCELLED。如用户还需要退款，之后可再调用 submit_refund。

【不要调用本工具的场景】
1. 用户没有给出订单号——必须先追问，严禁猜测、补全或编造 order_id。
2. 用户只是在咨询（"能取消吗""取消要多久""现在取消还来得及吗"）——这是询问而非
   指令，先答复并确认意图，不要直接执行。
3. 用户表述含糊（"我不想要了""这单有点问题"）——先澄清是否确实要取消，不要擅自
   执行不可逆操作。
4. 已知该订单不属于当前用户或不存在——直接告知查不到，不要调用本工具去"确认一下"。
5. 订单已是 CANCELLED / REFUNDED / COMPLETED——直接说明现状，不要调用后靠报错回复用户。
6. 用户真实意图是退款而不是取消——已签收订单应该走 submit_refund，不要为了退款去取消订单。"""


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
# 工具 5：提交退款（写，规则引擎裁决）
# ==========================================================================
SUBMIT_REFUND_DESC = """\
为当前登录用户本人的订单提交退款。写操作。

**金额不由你决定。** 你只提交"用户想要退多少"这个诉求，最终退款金额由服务端的
退款规则引擎根据订单实付、历史已退、问题类型计算得出，并写进返回的 amount 字段。
你不得在回复里自行编造或改动金额，也不要承诺"退多少"，一切以返回值为准。

两条通道由系统自动选择，返回值里的 result 字段说明走了哪条：
- result=REFUNDED：规则判定为低风险，已自动放款。可以直接告诉用户退款金额与到账周期。
- result=PENDING ：需人工审核，已进入商家队列，返回 queue_position 排队位置。
  钱还没退，必须明确告诉用户"已提交、等待商家审核"，不能说"已退款""马上到账"。

返回里的 decision 字段是判定依据（风险等级、金额算法、命中规则），仅供你理解，
不要把这些内部字段名、规则编号念给用户听。

前置条件（不满足时返回 ok=false 与错误码）：
- 订单必须属于当前用户，否则 ORDER_NOT_FOUND；
- 状态必须是 CANCELLED(已取消) 或 COMPLETED(已完成)，其它状态 ORDER_NOT_REFUNDABLE；
- 已全额退完返回 ALREADY_REFUNDED；
- 已有一笔待审批申请返回 PENDING_REFUND_EXISTS，此时不要重复提交；
- 被商家驳回后订单锁定，返回 REFUND_LOCKED_AFTER_REJECT；
- 主张金额超过可退余额返回 AMOUNT_EXCEEDED，extra 里给出本次最多可退多少。

issue_type 必须从枚举里选，它直接决定风险评分与是否需要用户提供凭证：
NOT_RECEIVED 未收到货 / DAMAGED 破损 / NOT_AS_DESCRIBED 与描述不符 /
QUALITY 质量问题 / WRONG_ITEM 拍错发错 / NO_LONGER_NEEDED 不想要了 / OTHER 其他。
用户描述含糊时按最贴近的选，不要为了分类反复追问。

【不要调用本工具的场景】
1. 订单是 PAID 或 CREATED 状态——PAID 需先取消，CREATED 还没付款。不要跳过这些步骤。
2. 用户还没取消就说"我要退款"，且订单未发货——先引导取消，取消后未发货可直接退。
3. 该订单已全额退完——直接告知已退金额和到账时间，不要重复调用。
4. 用户在咨询退款政策（到账时间、能否退到其他账户）——直接回答，不调用工具。
5. 用户要求退到非原支付账户——说明规则限制，不要尝试。
6. 订单不属于当前用户——直接拒绝，不要调用。
7. 查询结果 refund_locked 为 true，或返回 REFUND_LOCKED_AFTER_REJECT——
   告知用户需联系人工客服复核，由商家解锁后才能重新申请。换理由、改金额都不行。
8. 查询结果 pending_refund 不为空，或返回 PENDING_REFUND_EXISTS——
   告知用户正在审核、预计等待，不要重复提交。
9. 用户只是抱怨商品不好但没有明确要求退款——先确认意图，不要擅自发起退款流程。"""


@mcp.tool(
    name="submit_refund",
    title="提交退款",
    description=SUBMIT_REFUND_DESC,
    annotations=ToolAnnotations(
        title="提交退款",
        readOnlyHint=False,
        destructiveHint=True,
        idempotentHint=False,
        openWorldHint=False,
    ),
)
def submit_refund(
    order_id: Annotated[
        str,
        Field(
            min_length=1,
            description="要退款的订单号，必填。该订单必须已取消或已完成，不得编造。",
        ),
    ],
    reason: Annotated[
        str,
        Field(
            min_length=1,
            max_length=200,
            description="退款原因，必填，会展示给审批的商家。用用户原话归纳，例如“收到时屏幕已碎裂”。",
        ),
    ],
    issue_type: Annotated[
        Literal[
            "NOT_RECEIVED", "DAMAGED", "NOT_AS_DESCRIBED", "QUALITY",
            "WRONG_ITEM", "NO_LONGER_NEEDED", "OTHER",
        ],
        Field(description="问题类型，必填，决定风险评分与是否需要凭证。"),
    ],
    requested_amount: Annotated[
        float | None,
        Field(
            gt=0,
            description=(
                "用户口头主张的退款金额（元）。仅作为诉求记录，不决定最终金额——"
                "服务端会校验上限并按规则计算实际退款额。用户没提金额时省略，表示退剩余全部余额。"
            ),
        ),
    ] = None,
) -> dict[str, Any]:
    caller = current_caller()
    return _guard(
        service.submit_refund,
        caller["caller_id"],
        order_id=order_id,
        reason=reason,
        issue_type=issue_type,
        requested_amount=requested_amount,
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

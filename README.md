# 订单客服 Agent

基于 **LangChain + MCP + ReAct** 的电商订单客服智能体。三个工具通过 MCP 协议封装，身份与业务规则全部在服务端裁决，模型只负责理解和编排。

支持查询订单、取消订单、退款三条主链路，其中**已发货订单的退款必须经人工审批**，未发货订单直接放款。

## 设计立场

这个项目把绝大部分精力放在「模型不该有能力做的事」上，而不是「让模型更聪明」。

- **身份不进工具 schema**。`user_id` 在三个工具的参数里根本不存在，模型无从伪造，也就谈不上被提示词注入绕过。
- **业务规则在服务层**，不靠提示词约束。取消只能一次、退款须已取消、越权不可见，全部由带条件的原子 `UPDATE` 和数据库约束保证。
- **审批闸门在协议层**。即使绕过 Agent 直接调 MCP 工具，没带审批凭证也拿不到钱。
- **模型只做两件事**：判断用户想要什么、把结果翻译成人话。

一句话概括：**让模型做它擅长的，让系统做它必须做的。**

## 功能

| 能力 | 说明 |
|---|---|
| 查询订单 | 只读。按订单号精确查，或按状态列列表。范围强制限定为调用者本人 |
| 取消订单 | 仅 `CREATED` / `PAID` 可取消，每单一次，不可恢复 |
| 退款 | 仅 `CANCELLED` 可退，每单一次。**已发货需人工审批，未发货直接放款** |
| 人工审批 | 终端交互式确认，驳回后订单进入锁定态 |
| 退款追踪 | 每笔退款记录原因、发货状态、审批人、结果，支持事后追溯 |
| 驳回锁定 | 被驳回的退款申请锁定，须运营人工解锁才能重提 |

## 架构

```
┌──────────────────────────────────────────────┐
│  CLI 终端  (agent/main.py)                    │  读 .env 拿 CALLER_ID
└───────────────────┬──────────────────────────┘
                    │
┌───────────────────▼──────────────────────────┐
│  Agent 层                                     │  create_agent + ReAct
│  ├─ 系统提示词 (prompt.py)                     │  InMemorySaver 会话记忆
│  ├─ HumanInTheLoopMiddleware ────────────────┼─▶ 审批拦截（交互层）
│  └─ MCP Client（长连接）                       │
└───────────────────┬──────────────────────────┘
                    │  stdio
┌───────────────────▼──────────────────────────┐
│  MCP Server  (mcp_server/server.py)           │  FastMCP
│  三个工具 + 身份注入 ─────────────────────────┼─▶ current_caller()
└───────────────────┬──────────────────────────┘
                    │
┌───────────────────▼──────────────────────────┐
│  服务层  (mcp_server/service.py)              │  业务规则
│  原子 UPDATE / 状态机 / 审批凭证校验 ──────────┼─▶ 审批校验（权限层）
└───────────────────┬──────────────────────────┘
                    │
┌───────────────────▼──────────────────────────┐
│  SQLite  (data/orders.db)                     │  4 张表
└──────────────────────────────────────────────┘
```

## 快速开始

### 1. 装依赖

```bash
python -m venv .venv
# Windows
.venv/Scripts/python.exe -m pip install -r requirements.txt
# macOS / Linux
.venv/bin/python -m pip install -r requirements.txt
```

> 需要 Python 3.11+（开发环境为 3.13）。

### 2. 配置

复制 `.env.example` 为 `.env` 并填写：

```ini
# 任何 OpenAI 兼容端点都可以
LLM_API_KEY=your-api-key
LLM_BASE_URL=https://api.example.com/v1
LLM_MODEL=your-model-name

# 业务配置
ORDER_DB_PATH=data/orders.db
CALLER_ID=u_1001
```

不确定该用哪个模型名时，先探一下：

```bash
python scripts/probe_model.py
```

它会列出端点上的可用模型，并逐个验证是否支持 function calling（ReAct 依赖这个能力）。

### 3. 初始化数据

```bash
python -m data.seed
```

### 4. 跑起来

```bash
python -m agent.main
```

```
你 > 帮我把 ORD20261028001 取消掉，我不想要了
   调用 query_order {"order_id": "ORD20261028001"}
   结果 query_order: 索尼 WH-1000XM5 ¥2,499.00 已支付
   调用 cancel_order {"order_id": "ORD20261028001", ...}

   ---------- 需要你确认 ----------
   即将执行: cancel_order
   参数    : {"order_id": "ORD20261028001", "reason": "NO_LONGER_NEEDED"}
   确认执行？输入 y 执行，其它任意键取消 > y
   [已确认]

小柚 > 已经帮您取消好了。需要我接着帮您发起全额退款吗？
```

加 `-v` 可以看工具返回的原始 JSON。

## 三个工具

工具的 description 里写了四要素：功能、前置条件、返回内容、**不可调用场景**。最后一项是刻意设计的——比起告诉模型「什么时候用」，告诉它「什么时候不要用」更能减少误调用。

| 工具 | 读/写 | 必填 | 可选 |
|---|---|---|---|
| `query_order` | 只读 | — | `order_id`、`status`、`limit` |
| `cancel_order` | 写 | `order_id` | `reason` |
| `refund_order` | 写 | `order_id` | `amount`、`reason`、`approval_id` |

三个工具都挂了 MCP 官方 annotations：`query_order` 标 `readOnlyHint`，两个写工具标 `destructiveHint`，语义自描述，换成别的 MCP 客户端也同样适用。

`approval_id` 不需要用户或模型主动填——只有已发货订单首次调用返回 `MANUAL_APPROVAL_REQUIRED` 时，才把返回的凭证原样回传。

## 业务规则

### 状态机

```
CREATED ──pay──▶ PAID ──cancel──▶ CANCELLED ──refund──▶ REFUNDED
                  │
                  └──ship──▶ COMPLETED（终态）
```

### 退款的两条路径

```
                    ┌── 未发货 ──▶ 直接放款，trace 记 AUTO / NOT_REQUIRED
refund_order ───────┤
                    └── 已发货 ──▶ 生成审批单（不落款）
                                    ├─ 通过 ──▶ 放款，trace 记 MANUAL / APPROVED
                                    └─ 驳回 ──▶ 订单锁定，trace 记 MANUAL / REJECTED
```

已发货的审批采用**两段式协议**：第一次调用只写一条 `PENDING` 的追踪记录并返回 `approval_id`，数据库里的订单一个字节都不动；第二次带 `approval_id` 调用才真正改状态。金额以审批单为准，**不接受第二次调用传入的新金额**，防止审批通过后偷偷改价。

### 约束怎么保证

| 规则 | 实现手段 |
|---|---|
| 只能操作自己的订单 | 每条 SQL 强制 `WHERE owner_id = :caller` |
| 取消只能一次 | `cancelled_at IS NULL` + `operation_log` 唯一索引 |
| 退款只能一次 | 同上，外加 `refunded_at IS NULL` |
| 退款须已取消 | 前置状态校验 `status == 'CANCELLED'` |
| 并发不重复 | 单条带全部条件的 `UPDATE`，靠 `rowcount == 1` 判成败 |
| 驳回后不可重提 | `refund_lock` 表记录锁，须运营解锁 |

其中并发这条值得单独说：**没有采用「先查再改」**。两次独立 SQL 在高并发下会双写成功，必须一条 `UPDATE ... WHERE id=? AND owner_id=? AND status=? AND xxx_at IS NULL` 搞定，由数据库事务保证原子性。

越权统一返回「订单不存在」而不是「无权限」——否则别人能拿订单号试探系统里存在哪些单。

## 人工审批

分两层，各管一件事。

**交互层**（`HumanInTheLoopMiddleware`）：只在需要审批时才中断，未发货直接放行。中断后终端打印商品、金额、发货时间、运单号、退款原因，按 `y` 通过，其它键驳回。

```python
INTERRUPT_ON = {
    "cancel_order": True,                    # 取消始终确认
    "refund_order": InterruptOnConfig(
        when=_refund_needs_approval,         # 只有已发货才中断
        allowed_decisions=["approve", "reject"],
    ),
}
```

**权限层**（服务层两段式协议）：这才是真正的边界。实测过绕过 Agent 直接调 MCP 工具，没凭证拿不到钱，伪造凭证返回 `INVALID_APPROVAL`。

驳回时中间件会先把状态落库（trace 改 `REJECTED` + 写锁记录）再恢复执行，否则追踪链里会留一条永远 `PENDING` 的单子。

## 运维命令

```bash
# 查看退款追踪（含锁状态、审批人、原因）
python scripts/show_trace.py --full
python scripts/show_trace.py --order ORD20261008009

# 驳回锁定后，运营复核完解锁
python scripts/unlock_refund.py --list
python scripts/unlock_refund.py --active
python scripts/unlock_refund.py ORD20261008009 --by 陈明 --note "已核实破损照片"

# 检查 MCP 工具契约与连通性
python scripts/check_tools.py

# 重置测试数据
python -m data.seed
```

## 测试

```bash
python -m pytest tests -q        # 40 项业务规则测试
python scripts/smoke_test.py     # 32 项 MCP 协议层冒烟
```

两个测试套件的分工不同：

- **规则测试**直接调 `service` 层，不经过 MCP 和模型，跑得快（约 2 秒），覆盖状态机、并发、越权、审批凭证、锁定等边界。
- **冒烟测试**走完整的 stdio MCP 协议，验证工具契约、schema 生成、身份注入、错误码转换。这部分不依赖大模型，可以单独作为回归手段。

### 测试数据

`data/seed.py` 生成 3 个用户、9 笔订单，价格按真实行情设定（不是「耳机一块钱」那种占位数据），覆盖全部状态与发货组合：

| 订单号 | 用户 | 商品 | 状态 | 发货 | 用途 |
|---|---|---|---|---|---|
| `ORD20261028001` | 张伟 | 索尼 WH-1000XM5 | PAID | 否 | 可取消，取消后可直接退款 |
| `ORD20261015002` | 张伟 | 罗技 MX Master 3S | CANCELLED | 否 | 未发货退款，无需审批 |
| `ORD20261008009` | 张伟 | 罗技 G Pro X 2 | CANCELLED | **是** | 已发货退款，走人工审批 |
| `ORD20260920003` | 张伟 | AirPods Pro 2 | REFUNDED | 是 | 终态，验证重复退款被拦 |
| `ORD20261101004` | 张伟 | 小米移动电源 3 | CREATED | 否 | 待支付 |
| `ORD20261102005` | 张伟 | CHERRY MX Board 3.0S | COMPLETED | 是 | 终态，不可取消 |
| `ORD20261103006` | 李娜 | 戴森 Supersonic HD15 | PAID | 是 | **越权测试**（不属于当前用户） |
| `ORD20261010007` | 李娜 | iPad Air 11 | PAID | 是 | 越权测试 |
| `ORD20261104008` | 王强 | 德龙咖啡机 | PAID | 否 | 越权测试 |

默认 `CALLER_ID=u_1001`（张伟），所以李娜和王强的订单都是越权用例。

## 项目结构

```
.
├── agent/
│   ├── llm.py             # ChatOpenAI，任何 OpenAI 兼容端点
│   ├── tools_bridge.py    # MCP 客户端，长连接 + 身份注入
│   ├── prompt.py          # ReAct 系统提示词
│   └── main.py            # CLI 交互 + 审批中断处理
├── mcp_server/
│   ├── server.py          # FastMCP 实例，三个工具的契约定义 + 身份注入
│   └── service.py         # 业务规则引擎（状态机 + 原子更新 + 审批凭证）
├── data/
│   ├── schema.sql         # 4 张表定义
│   └── seed.py            # 测试数据生成
├── scripts/
│   ├── probe_model.py     # 探测模型与 function calling 能力
│   ├── check_tools.py     # 工具契约检查
│   ├── smoke_test.py      # MCP 协议层冒烟
│   ├── show_trace.py      # 退款追踪查看
│   └── unlock_refund.py   # 驳回锁人工解锁
├── tests/
│   ├── conftest.py
│   └── test_rules.py      # 40 项规则测试
├── .env.example
└── requirements.txt
```

## 关键实现决策

**为什么用 stdio 而不是 HTTP 传输？** 一条命令就能跑，身份通过启动参数绑定，演示和调试成本最低。要扩成多用户服务时，把 transport 换成 `streamable-http`、身份改从 `Authorization` 头解析即可，服务层代码不用动。

**为什么身份要放在子进程环境变量里？** 因为模型能控制的只有工具参数。参数里没有 `user_id`，提示词注入就没有着力点。

**为什么退款要两段式？** 因为审批是一个跨越人工环节的异步过程。如果一次调用里既开审批又放款，中间没有可插入的环节，也没有凭证可以校验。拆成两次调用后，「审批」变成一个可以放在任何地方的状态机节点。

**为什么 MCP 用长连接？** `get_tools()` 是无状态的，每次工具调用都会重新拉起子进程并初始化协议。实测 3 次调用从 8.82 秒降到 0.02 秒（`client.session()` + `load_mcp_tools`）。

## 已知限制

当前实现是**单机演示级**，上生产前需要处理：

- **身份是注入，不是认证**。谁改 `.env` 谁就是张伟。生产环境应换成 JWT/OAuth，从 HTTP header 解析并验签。
- **SQLite 单写者模型**。`BEGIN IMMEDIATE` 会拿写锁，并发写入会串行化。上量需换 PostgreSQL。
- **记忆仅限会话内**。`InMemorySaver` 进程退出即丢，没有跨会话的用户画像或历史感知。要持久化可换 `SqliteSaver`。
- **无累计退款校验**。目前只按单笔订单判断，没有「同一用户月累计退款超阈值」这类风控。
- **审批人是终端本地操作**，没有接入真实的工单系统或权限角色体系。

## License

MIT

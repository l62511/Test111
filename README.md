# 订单客服 Agent

基于 **LangChain + MCP + ReAct** 的电商订单客服智能体，配一套面向商家的退款审批后台。

顾客侧是命令行对话：能查订单、取消订单、申请退款。
商家侧是浏览器后台：登录后审批退款、查看队列与累计退款进度。

业务规则、身份校验、金额累加全部在服务端裁决，模型只负责理解意图和编排调用。

## 设计立场

这个项目把绝大部分精力放在「模型不该有能力做的事」上，而不是「让模型更聪明」。

- **身份不进工具 schema**。`user_id` 在三个工具的参数里根本不存在，模型无从伪造，也就谈不上被提示词注入绕过。
- **业务规则在服务层**，不靠提示词约束。取消只能一次、退款额度累加、越权不可见，全部由服务层的持锁事务保证。
- **审批闸门在商家后台**。提交申请不等于放款，钱只有在商家点「通过」的那一刻才动。
- **模型只做两件事**：判断用户想要什么、把结果翻译成人话。

一句话概括：**让模型做它擅长的，让系统做它必须做的。**

## 功能

| 能力 | 说明 |
|---|---|
| 查询订单 | 只读。按订单号精查或按状态列列表，范围强制限定为调用者本人 |
| 取消订单 | 仅待支付/已支付可取消，每单一次，不可恢复 |
| 申请退款 | 未发货直接退；已发货或已签收生成申请，进商家队列 |
| 累计退款 | 订单记录已退金额，分次退款累加，**累计绝不超实付** |
| 商家审批 | 后台登录后按队列顺序审批，通过/驳回，驳回后可人工解锁 |
| 退款追踪 | 每笔记录金额、原因、审批人、结果、时间，可事后追溯 |

## 架构

```
┌─────────────────────┐        ┌──────────────────────────┐
│  顾客端 CLI          │        │  商家后台 (FastAPI)       │
│  agent/main.py      │        │  web/app.py              │
│  · 账号密码登录       │        │  · 登录 / 队列 / 审批      │
│  · ReAct 对话        │        │  · 订单与累计进度          │
└──────────┬──────────┘        └────────────┬─────────────┘
           │ stdio                          │ 直接调用
┌──────────▼──────────┐                     │
│  MCP Server          │                     │
│  · 三个工具           │                     │
│  · 身份注入           │                     │
└──────────┬──────────┘                     │
           └────────────┬───────────────────┘
                        ▼
              ┌───────────────────┐
              │  服务层 service.py │
              │  规则 / 累计 / 锁   │
              └─────────┬─────────┘
                        ▼
              ┌───────────────────┐
              │  JSON 文件存储      │
              │  users/orders/     │
              │  refunds.json      │
              └───────────────────┘
```

GUI 和 CLI 共享同一个服务层，所以「后台看到的状态」和「Agent 看到的状态」永远是同一份数据，不存在两套逻辑对不上的情况。

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

# 数据目录
ORDER_DATA_DIR=data

# 后台会话签名密钥，生产环境务必修改
MERCHANT_SECRET=change-this-in-production
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

会生成 4 个账号、9 笔订单、3 条退款记录。

> 仓库里的 `data/*.json` **是有意提交的**，所以克隆下来不跑这步也能看到完整数据形态。
> 代价是跑完 demo 后 `data/*.json` 会被改写、工作区变脏。想回到初始状态，
> 重跑 `python -m data.seed`，或者 `git checkout -- data/` 丢弃改动。
> 同理，`.preview/*.html` 是后台界面的离线快照（CSS 已内联），
> 不启动服务也能用浏览器直接打开核对界面。

### 4. 启动

**顾客端**（另开一个终端）：

```bash
python -m agent.main
```

会提示输入账号密码。调试时可以用 `--as u_1001` 跳过登录：

```bash
python -m agent.main --as u_1001
```

**商家后台**：

```bash
python -m web.app
# 或指定端口
python -m web.app --port 8080
```

打开 http://127.0.0.1:8000 即可。

## 演示账号

| 账号 | 密码 | 角色 | 说明 |
|---|---|---|---|
| `zhangwei` | `123456` | 顾客 | 张伟，6 笔订单，覆盖各种状态 |
| `lina` | `123456` | 顾客 | 李娜，含部分退款订单 |
| `wangqiang` | `123456` | 顾客 | 王强，用于越权测试 |
| `merchant` | `admin123` | 商家 | 陈明，登录后台审批 |

密码用 PBKDF2-HMAC-SHA256（26 万轮 + 随机盐）哈希后存在 `users.json`，不存明文。
演示密码可通过 `DEMO_CUSTOMER_PASSWORD` / `DEMO_MERCHANT_PASSWORD` 环境变量覆盖。

## 三个工具

工具的 description 里写了四要素：功能、前置条件、返回内容、**不可调用场景**。最后一项是刻意设计的——比起告诉模型「什么时候用」，告诉它「什么时候不要用」更能减少误调用。

| 工具 | 读/写 | 必填 | 可选 |
|---|---|---|---|
| `query_order` | 只读 | — | `order_id`、`status`、`limit` |
| `cancel_order` | 写 | `order_id` | `reason` |
| `refund_order` | 写 | `order_id` | `amount`、`reason` |

`refund_order` 的返回值里 `result` 字段说明走了哪条路：`REFUNDED`（已直接退款）或 `PENDING`（已提交申请，等待审批）。模型据此决定是报「已退款」还是「已提交，等待商家审核」。

三个工具都挂了 MCP 官方 annotations：`query_order` 标 `readOnlyHint`，两个写工具标 `destructiveHint`。

## 业务规则

### 状态机

```
CREATED ──pay──▶ PAID ──cancel──▶ CANCELLED ──refund──▶ REFUNDED
                  │                    ▲
                  └──ship──▶ COMPLETED ┘（签收后也可申请退款）
```

### 退款的两条路径

```
                     ┌── 未发货 ──▶ 立即放款
                     │              result=REFUNDED
refund_order ────────┤
                     └── 已发货 / 已签收
                              │
                              ▼
                        生成 PENDING 申请
                              │
                     ┌────────┴────────┐
                     ▼                 ▼
                商家通过           商家驳回
                累加金额           订单锁定
                result=APPROVED    result=REJECTED
                                   （需人工解锁）
```

### 金额累加

这是这套系统的核心约束。订单上记 `refunded_cents`，每次退款前校验：

```
已退金额 + 本次金额 ≤ 订单实付
```

支持多次部分退款。举例：iPad 实付 ¥4,799，先退 ¥1,000，再退 ¥500，累计 ¥1,500，余额 ¥3,299。第三次想退 ¥5,000 会被拦下并提示「最多还能退 ¥3,299」。

省略 `amount` 参数表示退剩余全部余额，不是退全额——这个语义差别在提示词和工具描述里都写清楚了。

退到余额为 0 时，订单状态自动置为 `REFUNDED`。

### 约束怎么保证

| 规则 | 实现手段 |
|---|---|
| 只能操作自己的订单 | 查询按 `owner_id` 过滤，写入前校验归属 |
| 取消只能一次 | `cancelled_at` 非空即拒绝 |
| 退款须已取消或已完成 | 状态白名单 `REFUNDABLE_STATUS` |
| 累计不超实付 | 每次退款前计算余额，审批时再校验一次 |
| 并发不重复提交 | 文件锁内读-改-写，进锁后二次检查 PENDING |
| 并发不重复审批 | 审批时在锁内检查状态必须仍为 PENDING |
| 驳回后不可重提 | `refunds.json` 里的 locks 记录 |

越权统一返回「订单不存在」而不是「无权限」——否则别人能拿订单号试探系统里存在哪些单。

## 商家后台

三个页面，都是服务端渲染，没有前端构建步骤。

**待审批队列** `/queue`
按提交顺序排队，显示排队号、等待时长。每张卡片包含：商品、用户、申请编号、订单实付、已退金额、本次申请、退款后余额，以及一条双色进度条（绿色=已退，黄色=本次申请）。底部两个操作：通过放款、驳回申请（驳回必须填原因）。

**退款记录** `/refunds`
全部历史，可按订单号或状态筛选。显示审批人、备注、处理时间。

**订单** `/orders`
跨全部顾客的订单列表，含累计退款进度。被驳回锁定的订单会高亮，并提供「解锁」按钮。

顶部有四个统计卡片：待审批笔数与金额、今日已通过、累计驳回、锁定订单数。

登录态是签名 cookie（HMAC-SHA256），8 小时过期，密钥来自 `MERCHANT_SECRET`。

## 数据存储

三个 JSON 文件在 `data/` 下，由 `ORDER_DATA_DIR` 指定目录：

| 文件 | 内容 |
|---|---|
| `users.json` | 账号、密码哈希、角色 |
| `orders.json` | 订单，含 `refunded_cents` 累计已退 |
| `refunds.json` | 退款申请、审批记录、驳回锁 |

JSON 文件不像数据库那样自带事务，所以这里自己补了两件事，都在 `mcp_server/store.py`：

1. **跨进程互斥锁**：每个文件配一个 `.lock` 伴生文件，Windows 用 `msvcrt`，Unix 用 `fcntl`。Agent 的 MCP 子进程和商家后台是两个独立进程，会同时读写同一份文件——没有文件锁就会丢更新。
2. **原子替换**：先写同目录临时文件，再 `os.replace` 覆盖。同一文件系统内这是原子操作，读者要么看到旧内容，要么看到新内容，不会读到写了一半的 JSON。

`store.transaction()` 把「加锁 → 读取 → 修改 → 原子写回」包成一个上下文管理器，异常时丢弃改动不落盘。

> 这套机制在单机多进程场景下是可靠的。如果要横向扩展到多台机器，JSON 文件就不是合适的选择了，应该换成支持事务的数据库。

## 项目结构

```
.
├── agent/
│   ├── llm.py             # ChatOpenAI，任何 OpenAI 兼容端点
│   ├── tools_bridge.py    # MCP 客户端，长连接 + 身份注入
│   ├── prompt.py          # ReAct 系统提示词
│   └── main.py            # CLI：登录 + 对话 + 取消确认
├── mcp_server/
│   ├── server.py          # FastMCP 实例，三个工具的契约定义
│   ├── service.py         # 业务规则：状态机、累计退款、审批、锁定
│   └── store.py           # JSON 存储：文件锁、原子写、密码哈希
├── web/
│   ├── app.py             # FastAPI 路由 + 签名 cookie 会话
│   ├── templates/         # Jinja2 模板
│   └── static/style.css   # 样式
├── data/
│   ├── seed.py            # 生成演示数据
│   ├── users.json         # 账号（密码只存哈希）
│   ├── orders.json        # 订单
│   └── refunds.json       # 退款申请与驳回锁
├── .preview/              # 后台界面离线快照（CSS 内联）
├── scripts/
│   ├── probe_model.py     # 探测模型与 function calling
│   ├── check_tools.py     # 工具契约检查
│   ├── smoke_test.py      # MCP 协议层冒烟
│   ├── show_trace.py      # 退款记录查看
│   └── unlock_refund.py   # 驳回锁人工解锁
├── tests/
│   ├── conftest.py
│   └── test_rules.py      # 37 项规则测试
├── .env.example
└── requirements.txt
```

## 运维命令

```bash
# 查看退款记录（含累计金额、审批人、锁定状态）
python scripts/show_trace.py
python scripts/show_trace.py --order ORD20261010007
python scripts/show_trace.py --status PENDING

# 驳回锁定后，运营复核完解锁
python scripts/unlock_refund.py --list
python scripts/unlock_refund.py ORD20261008009 --note "已核实照片"

# 检查 MCP 工具契约
python scripts/check_tools.py

# 重置演示数据
python -m data.seed
```

## 测试

```bash
python -m pytest tests -q        # 37 项业务规则测试
python scripts/smoke_test.py     # 29 项 MCP 协议层冒烟
```

两个套件分工不同：

- **规则测试**直接调 `service` 层，不经过 MCP 和模型，跑得快（约 3 秒），覆盖认证、越权、状态机、累计退款、并发、审批幂等、驳回锁定。
- **冒烟测试**走完整的 stdio MCP 协议，验证工具契约、schema 生成、身份注入、错误码转换。不依赖大模型，可以单独作为回归手段。

关键的几个测试：

| 测试 | 验证什么 |
|---|---|
| `test_cumulative_across_two_approvals` | 分两次审批，累计金额正确累加 |
| `test_concurrent_submit_creates_single_pending` | 4 线程并发提交，只落一条 PENDING |
| `test_concurrent_approve_only_once` | 4 线程并发审批，只放款一次 |
| `test_reject_locks_order` | 驳回后锁定，用户无法重提 |
| `test_password_is_hashed` | 数据文件里没有明文密码 |

## 测试数据

`data/seed.py` 生成 3 个顾客、9 笔订单，价格按真实行情设定：

| 订单号 | 用户 | 商品 | 金额 | 状态 | 物流 | 用途 |
|---|---|---|---|---|---|---|
| `ORD20261028001` | 张伟 | 索尼 WH-1000XM5 | ¥2,499 | 已支付 | 未发货 | 可取消 |
| `ORD20261015002` | 张伟 | 罗技 MX Master 3S | ¥699 | 已取消 | 未发货 | 直接退款 |
| `ORD20261008009` | 张伟 | 罗技 G Pro X 2 | ¥1,099 | 已取消 | 已发货 | 申请审批 |
| `ORD20260920003` | 张伟 | AirPods Pro 2 | ¥1,899 | 已退款 | 已签收 | 终态 |
| `ORD20261101004` | 张伟 | 小米移动电源 | ¥129 | 待支付 | 未发货 | 未付款 |
| `ORD20261102005` | 张伟 | CHERRY 机械键盘 | ¥458 | 已完成 | 已签收 | 签收后退款 |
| `ORD20261103006` | 李娜 | 戴森 HD15 | ¥2,999 | 已支付 | 已发货 | 越权测试 |
| `ORD20261010007` | 李娜 | iPad Air 11 | ¥4,799 | 已完成 | 已签收 | **部分退款 ¥1,000** |
| `ORD20261104008` | 王强 | 德龙咖啡机 | ¥2,680 | 已支付 | 未发货 | 越权测试 |

iPad 那笔预置了一条 ¥500 的待审批申请，后台一打开就能看到内容。它同时也演示了累计逻辑：已退 ¥1,000，余额 ¥3,799。

## 关键实现决策

**为什么用 JSON 文件而不是数据库？** 按需求选择了文件存储，好处是数据可直接阅读、便于演示和排查。代价是失去了数据库的事务和并发控制，所以用文件锁 + 原子写补上。这个取舍在单机多进程场景成立，跨机器部署就需要换回数据库。

**为什么身份要放在子进程环境变量里？** 因为模型能控制的只有工具参数。参数里没有 `user_id`，提示词注入就没有着力点。

**为什么退款要拆成「申请」和「放款」两步？** 因为审批是一个跨越人工环节的异步过程。如果一次调用里既提交又放款，中间没有可插入的环节，钱就绕过了人。

**为什么审批放在独立的后台而不是终端？** 商家和顾客不在一台机器上。「终端里按 y」只是演示手段，真实场景里审批人是运营人员，需要一个能登录、能排队、能留痕的界面。

**为什么 MCP 用长连接？** `get_tools()` 是无状态的，每次工具调用都会重新拉起子进程并初始化协议。实测 3 次调用从 8.82 秒降到 0.02 秒（`client.session()` + `load_mcp_tools`）。

## 已知限制

- **身份仍是「注入 + 会话」，不是完整认证**。顾客端密码校验后把 `user_id` 注入子进程，商家端用签名 cookie。生产环境应换成 JWT/OAuth，并接入真实的用户体系。
- **JSON 存储不适合横向扩展**。文件锁只在单机多进程内有效。
- **审批队列没有分页**。后台一次最多渲染 100 条。
- **没有通知机制**。用户提交申请后不会收到推送，只能自己查进度。
- **无风控**。没有「同一用户月累计退款超阈值」这类规则。
- **会话密钥默认值不安全**。`MERCHANT_SECRET` 未配置时用了固定的演示密钥。

## License

MIT

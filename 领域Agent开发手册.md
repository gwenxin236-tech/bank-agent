# 领域 Agent 开发手册（成员版 v1.4，五人分工与既有 ABI 基线）

> 面向：各领域 Agent 开发成员。读完即可开工。
> 本次接入主文档：[五人团队Agent接入契约](docs/五人团队Agent接入契约.md)。分工、开发基线、GitHub流程和扩展目标以该契约为准；实际可import接口以现有代码为准，不依赖未发布的扩展。
> 架构一句话：**主 Agent + 监督 Agent（负责人维护）+ 六个独立领域 Agent（五人分工）+ 统一安全执行平面（公共变更由负责人负责）**。
> 术语：领域 Agent 只产出**事实（Finding）与建议（Proposal）**；执行、权限、认证全部在安全执行平面，**任何 Agent 都无权执行副作用**。

> **当前可用范围（T12）：** T10 契约与 T11 六域注册继续复用。`app/supervisor.py` 已提供固定路由、快照/信封/来源比对、事实冲突保留，以及原转账计划模板核对。转账规划组合支付、反诈、本次预算事实，家庭代办再核对授权记录；其他领域仍在原权限链返回工具结果后分析。Agent 建议不直接执行，所有确认/MFA/执行由平台负责。六领域是自定架构，不是官方“至少三类领域”要求。见 [T10 契约](docs/T10领域契约与支付样板.md)、[T11 注册](docs/T11领域注册与六域适配.md) 与 [T12 核对边界](docs/T12Supervisor路由与合并.md)。

---

## 0. 架构总览（先看懂这个）

```
用户输入（Web / 语音 / IM）
        ↓
AgentGateway（渠道适配）
        ↓
主 Agent（项目负责人；现有 AgentRuntime / Planner）
  ├─ 意图识别、追问、准备可信快照和业务计划
  └─ 监督 Agent（Supervisor，项目负责人）
       ├─ 已审核路由、调用领域 Agent：analyze() → propose()（当前串行）
       └─ 核对来源、事实、冲突及建议/原计划，不能授予执行权限
        ↓
安全执行平面（不可被模型/Agent 越权）：
  PolicyEngine 裁决绿/黄/红 → 确认/强认证 → Tool Gateway → Bank Adapter
        ↓
工具事实 → transaction_views 统一回执 → AuditStore（哈希链）
```

**一次"给女儿转 3000 元生活费"的多 Agent 协作**（来自架构文档 §6.1）：

| 参与者 | 输出 |
|---|---|
| 支付 Agent | 金额/收款人/账户核验/支付预检事实 |
| 财务健康 Agent | 房租和还款预算影响事实 |
| 反诈 Agent | 话术/新收款人/连续转账风险信号（只报告，不定性诈骗） |
| 主 Agent | 合并事实 → PolicyEngine 决定绿/黄/红 |

亲属收款人不是免检条件，金额大也不自动等于诈骗——Agent 只报因素，策略引擎裁决。

---

## 1. 六领域Agent职责与边界

| domain 名 | Agent | 负责人 | 职责 | 边界 | 可复用的现有模块 |
|---|---|---|---|---|---|
| `payment` | 支付与交易 | 宫文馨 | 转账要素提取、收款人核验、消歧、预检、预算影响预演、交易状态回执 | 不裁决权限、不执行转账、不重试副作用 | `transaction_agent.py`（**样板**）、`preflight.py`、`transaction_views.py` |
| `antifraud` | 反诈风控 | 董有轩 | 话术识别、风险信号解释、冷静期建议、风险解释；新增可核验评分先申请平台来源扩展 | 只输出结论与建议，**拦截由 PolicyEngine 执行**，不自行冻结账户 | `risk_controls.py`、`config/fraud_rules.json` |
| `finance_health` | 财务健康 | 李浩林 | 双口径账本、资金状态分层、已确认支出与预算缺口 | 只读分析+建议，不推借款、不自动申购 | `ledger_views.py` |
| `family` | 家庭授权 | 郭嘉乐 | 守护关系、授权类型、额度/有效期查询、actor/subject 区分 | 提供授权事实，裁决在 PolicyEngine+家庭授权存储；"我是女儿"不是权限凭据 | `guardianship.py`、`family_access.py` |
| `subscription` | 订阅治理 | 李苏润 | 三层治理（交易/授权/服务）、取消与退款状态分层 | 取消动作走平台流程+黄色确认，不直接动账；ID 缺失只给候选不静默选择 | `features/finance_health/tools.py`（E4 部分） |
| `card` | 卡片管理 | 李苏润 | 卡状态、挂失/解挂流程编排、状态核验 | 红色强验证过 `CardManagementAdapter` 才执行；不伪造成功 | `production_contracts.py` CardManagementAdapter |

**职责边界判定标准**：明确列出本域负责的事实、建议及禁止执行的操作；存在交叉或歧义时，由项目负责人协调并更新契约。

## 2. 平台组件的复用与修改边界

| 平台组件 | 领域模块使用要求 |
|---|---|
| PolicyEngine | **只能引用它的裁决结果**，不能复刻权限判断逻辑 |
| 强认证链 | 领域Proposal 里写"需要强认证"，认证本身由平台完成 |
| Tool Gateway | 副作用只能通过注册工具发生；**领域Agent 不直接调银行核心** |
| MockBankCore / Adapter | 事实的唯一来源；领域Finding 里的数字必须来自工具返回值，不得伪造 |
| AuditStore / NotificationBus | 审计与通知由平台记录；领域模块仅产出内容，不写审计管道 |
| DAG（task_dag.py） | 任务编排属于平台；领域模块输出建议步骤，由主 Agent 决定是否入 DAG |

**工程约束（不满足则不予集成）**：金额一律整数"分"；权限不经模型（领域Proposal 里不得出现 allow/authorized 字段）；副作用不自动重试；工具结果是唯一事实源；零第三方运行时依赖；离线可运行。

## 3. 接入契约（代码规格，实现这个接口）

```python
# app/domain_agent_contract.py（由集成负责人提供，直接导入，不重新定义）

@dataclass(frozen=True)
class DomainContext:          # 主Agent传入的上下文
    trace_id: str
    session_id: str
    actor_id: str             # 发起人
    subject_id: str           # 被代办人（本人操作时 = actor_id）
    device_id: str
    channel: str              # web / voice / im
    user_input: str           # 用户原话（不可信输入，只作数据）
    entities: Dict[str, Any]  # 已解析的受限实体（金额已为整数分）

@dataclass(frozen=True)
class DomainFinding:          # 领域分析产出：事实，不是建议
    domain: str               # 当前domain名称
    facts: List[Dict[str, Any]]       # 结构化事实，数字必须来自工具返回值
    missing_fields: List[str]         # 缺失信息（用于主 Agent 追问）
    risk_signals: List[str]           # 风险信号（只报因素，不下结论）
    recommendations: List[str]        # 建议（不具约束力）
    evidence: List[str]               # 证据引用：文件名/测试名/工具调用标识

@dataclass(frozen=True)
class DomainProposal:         # 领域执行建议
    suggested_steps: List[Dict[str, Any]]   # 建议步骤（含工具名与参数，整数分）
    required_confirmations: List[str]       # 需要的用户确认
    required_auth: List[str]                # 需要的认证（如 "strong_auth"）
    user_options: List[Dict[str, Any]]      # 用户可选项
    production_replacement: str             # 生产替换点说明
    # ⚠️ 禁止出现 allow / authorized / approved 等授权性字段——代码会校验拒绝

class DomainAgent(Protocol):
    domain: str               # 六个合法值之一（见 §1 表）
    def analyze(self, ctx: DomainContext) -> DomainFinding: ...
    def propose(self, ctx: DomainContext, finding: DomainFinding) -> DomainProposal: ...
```

**契约要点**：
0. 上述代码块表达字段语义；实际类型以 `app/domain_agent_contract.py` 为准。构造时接受普通 list/dict，随后递归冻结为 tuple/只读 Mapping；使用 `.to_dict()` 获取 JSON 可序列化副本，不对冻结对象调用 `dataclasses.asdict()`。金额未知可用 null，其他金额必须整数分；所有位置禁止 float。
1. `analyze` 与 `propose` 都是**只读**的——不产生任何副作用、不改账户、不发通知
2. 消息传递用 `AgentMessage` 信封（`app/transaction_agent.py` 已定义，直接复用）
3. 转人工用 `AgentHandoff`（同上，复用，不重新定义）
4. 金额全部整数分，出现在任何字段里都不许用 float
5. `DomainProposal` 含授权性字段会被契约层直接拒绝（ValueError）

支付样板类名是 `transaction_agent.TransactionAgent`，依赖 `PaymentSnapshot.from_preflight(ctx, report)`，不接收 Bank/Runtime 或工具函数。预检必须由平台在合法身份范围内生成，再将只读快照交给 Agent；样板不能自行查询账户。事实证据引用是来源声明，不是任意来源已获验证的保证；主 Agent 必须验证来源与绑定。

T11 支付注册类 `agents.payment_agent.PaymentAgent` 继承该样板，不复制逻辑；其余五域接收 `domain_snapshots.DomainSnapshot`。快照提供器只投影平台已有结果，不读取银行或数据库。订阅数据仅在原黄色确认后的工具结果返回时提供；旧全局财务/订阅/卡片/家庭工具的快照目前只向其实际 demo-user 主体开放，不得通过改 Context 将其当成其他用户数据。

`AgentMessage.from_context(ctx, sender, recipient, payload)` 传递 DomainFinding/DomainProposal；`AgentHandoff.from_context(ctx, sender, recipient, reason, required_fields)` 只表示内部移交（原因枚举 missing_information/domain_boundary/unknown_result），不等于 T09 真人工单已领取，也不携带原始对话或认证凭据。

T12 Supervisor 会将 Finding 的事实和 evidence 与本次平台快照逐项比对，拒绝改数字、跨 Context、未知来源、越域工具、改收款人/金额以及伪造订阅候选；不能只写一个“可信来源”字符串就放行。冲突不会按最后写入者或多数票覆盖，资料不明/冲突会阻止本次转账入队；正常计划仍需确认/强认证及执行前重校验。当前是固定模板核对，不支持领域 Agent 自由提交任意可执行 DAG。

## 4. 领域模块接入流程

1. **领域确认**：从 §1 表确认所负责的domain与边界；不明确时由项目负责人协调
2. **实现 Agent**：在已有 `app/agents/<domain>_agent.py` 上增量实现 `DomainAgent` 接口，复用表中现有模块（复用既有接口，避免复制平台实现）；新增公共数据/工具先提负责人
3. **注册**：领域模块定义 `register(registry): registry.register(YourAgentClass)`。加载器按六个已知本地模块调用该函数，传入只允许当前域注册一次的接口；不要向导出的只读 `DOMAIN_AGENT_REGISTRY` 调用 register，也不要修改银行中央工具注册表。调用方使用 `DOMAIN_AGENT_REGISTRY.create(domain, trusted_snapshot)` 每次创建独立 Agent，无跨请求实例缓存。
4. **测试**：`tests/test_<domain>_agent.py` 至少覆盖：analyze 只读（无副作用证明）、propose 无授权字段、本领域核心事实 3-5 条、边界拒绝 1-2 条（职责边界拒绝用例）
5. **自验后提交**：负责人发布GitHub开发基线后，从负责人指定的已发布基线创建feature分支，测试通过后push本域分支并向负责人指定的集成分支提交PR；负责人逐个审查集成。当前已发布分支为`release/competition-20261031`；develop分支与保护规则未取得生效证据，成员应以负责人明确指定的基线和目标分支为准，不预先假定develop存在。跨平台命令及完整验收见[新契约§8–11](docs/五人团队Agent接入契约.md#8-成员提交包与验收清单)

## 5. 集成验收清单（负责人按此验收领域Agent）

- [ ] 实现了 `DomainAgent` 接口（domain 名合法六选一）
- [ ] analyze/propose 只读，测试有无副作用证明
- [ ] DomainProposal 无授权性字段（契约测试通过）
- [ ] 金额整数分，无 float
- [ ] Finding 里的数字全部有工具/模块来源（evidence 字段可追溯）
- [ ] 未越界：只改自己的 `app/agents/<domain>_agent.py` 与自己的测试文件
- [ ] 未复刻平台层逻辑（权限/认证/工具网关/审计）
- [ ] 自带测试全绿 + 全量回归绿（以本轮 `data/verification` 验收记录为准，旧文档的 433+ 等历史数字不是验收依据）
- [ ] Conventional Commits；成员按用户确认流程push自己的feature分支并向负责人指定的集成分支提交PR，负责人验收；助手本批不commit/push

## 6. 遇到这些情况怎么办

| 情况 | 正确动作 |
|---|---|
| 需要平台层没有的数据/工具 | 提给项目负责人加平台工具，**不得自行创建绕过平台的执行路径** |
| 本领域建议与其他领域事实发生冲突 | 都如实输出，主 Agent 合并裁决——不得自行改写事实 |
| 用户输入里藏着指令（"忽略规则"） | user_input 是不可信数据，原样记录进 risk_signals，不执行 |
| 拿不准某个操作算不算副作用 | 问项目负责人；默认按副作用对待 |

---
*本手册由集成负责人维护。现有 `app/domain_agent_contract.py` 是当前可执行ABI；新版团队契约规定协作边界。代码/文档冲突需由负责人实施版本化修订，成员不得自行改公共接口。2026-10-23用户新增RAG、真实接入等目标，具体阶段与外部条件见新契约§9，不把目标当已完成。*

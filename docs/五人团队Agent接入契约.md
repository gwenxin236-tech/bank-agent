# 五人团队 Agent 接入契约与平台先行交付流程

版本：2026-10-23 / 团队协作契约 v1.0。适用范围：当前副本及其后经负责人发布的团队开发基线。

本文件依据负责人本次确认的分工与范围编写，复用已存在的 Python 接口；不是另建一套 Agent 框架。本文中的“冻结”是团队兼容规则，不表示本批新增了自动化版本锁。主 Agent、监督 Agent、支付 Agent 和公共平台由负责人维护，其他四位成员在同一基线上完善五个独立领域模块。

**状态约定：**“当前可用”必须有代码/测试证据；“团队约定”从本文件执行；“待实现”仅为下一阶段目标；“待外部接入”必须取得实际平台、账号与接口后联调，不能用 Mock 或大模型回答替代。

## 1. 五人分工与文件归属

| 成员 | domain / 职责 | 默认可修改的现有文件 | 建议功能分支 |
|---|---|---|---|
| 项目负责人（你） | payment：支付与交易；主 Agent、监督 Agent和全部公共平台 | `app/agents/payment_agent.py`、`app/transaction_agent.py`、对应测试；公共文件统一由负责人维护 | `feature/lead-payment`；公共能力另开 `feature/platform-<topic>` |
| 董哥 | antifraud：反诈风控 | `app/agents/antifraud_agent.py`、`tests/test_antifraud_agent.py` | `feature/dongge-antifraud` |
| 李狗 | finance_health：财务健康 | `app/agents/finance_health_agent.py`、`tests/test_finance_health_agent.py` | `feature/ligou-finance-health` |
| 郭哥 | family：家庭授权 | `app/agents/family_agent.py`、`tests/test_family_agent.py` | `feature/guoge-family` |
| 二狗 | subscription：订阅治理 | `app/agents/subscription_agent.py`、`tests/test_subscription_agent.py` | `feature/ergou-subscription` |
| 二狗 | card：卡片管理 | `app/agents/card_agent.py`、`tests/test_card_agent.py` | `feature/ergou-card` |

一个人可以维护两域，但二狗的两个域保留各自模块、注册函数、测试及 PR，不合并为一个 domain。表中中文称呼是用户确认的团队称呼，不是 GitHub 用户名；目标仓库URL已提供为https://github.com/gwenxin236-tech/bank-agent；四位成员GitHub账号和邀请/接收状态尚未提供。

成员是在已有实现上增量完善，不能直接用个人项目覆盖整个 `app/`。确需辅助模块、样例语料或额外测试文件时，在 PR 中列出路径与理由并由负责人确认，不把其他域和公共平台混入同一 PR。成员已有独立项目时，先提取纯分析逻辑适配本接口，不要求平台兼容四套运行框架。

依据：`app/domain_agent_registry.py:14` 校验六域和所属模块；`:52` 加载六个既定模块。领域文件当前存在，不代表成员版本已经交付。

## 2. 三类 Agent 与执行平面

| 层 | 负责人 | 职责 | 不承担的职责 |
|---|---|---|---|
| 主 Agent | 你 | 接收请求、识别/澄清意图、准备授权范围内的事实、组织业务计划、把监督结果和工具回执呈现给用户 | 不把模型判断当权限；不伪造执行成功 |
| 监督 Agent（Supervisor） | 你 | 按已审核路由调用领域模块，核对 Context、来源、事实、工具、参数与冲突，审核候选计划 | 不直接动账、不签发强认证凭据、不替代 PolicyEngine |
| 领域 Agent | 表中对应成员 | 根据绑定的只读快照输出 Finding 和 Proposal，说明缺失信息、风险及建议 | 不直接查询银行/数据库、不发通知、不改授权、不调用业务写工具 |
| 安全执行平面 | 你 | PolicyEngine、确认、强认证、Tool Gateway、Adapter、幂等、DAG、审计、熔断及接管 | 不接受 Agent 自报的“已授权/已执行” |

当前主流程位于 `app/banking_agent.py:2359` 的 AgentRuntime，Planner 位于同文件 `:2166`；Supervisor 是独立类，初始化接入位于 `:2368`。本契约区分逻辑角色，不要求复制一个新的主 Agent 类。

当前 Supervisor 是固定顺序的本地调用：`app/supervisor.py:62` 提供路由，`:145` 收集结果，`:196` 协调，`:233` 核对原转账模板；现状没有并发/分布式调用承诺。其他业务主要在既有工具返回后观察，不把六域注册说成任意跨域任务都能自动执行。

## 3. 冻结的 Python ABI：直接 import，不重新定义

### 3.1 入口与构造

```python
from domain_agent_contract import DomainContext, DomainFinding, DomainProposal

class YourDomainAgent:
    domain = "antifraud"  # 改为本域合法值

    def __init__(self, snapshot):
        # snapshot 由平台构造，不接收 Bank/Runtime/数据库/工具回调。
        self.snapshot = snapshot

    def analyze(self, ctx: DomainContext) -> DomainFinding:
        # 本代码块仅说明签名，实际实现参照第7节可执行样例。
        raise NotImplementedError

    def propose(self, ctx: DomainContext, finding: DomainFinding) -> DomainProposal:
        raise NotImplementedError

def register(registry):
    registry.register(YourDomainAgent)
```

上面的 `NotImplementedError` 仅用于解释协议，禁止作为成员成品提交。实际接口以 `app/domain_agent_contract.py:90`、`:109`、`:128`、`:153` 为准。支付类继续继承 `transaction_agent.TransactionAgent`；其他五域优先继承 `domain_snapshots.SnapshotAgent`，复用绑定和只读逻辑。

### 3.2 Context 字段

| 字段 | 类型与来源 | 约束 |
|---|---|---|
| trace_id / session_id | 平台生成的字符串 | 标识本请求及会话，不是身份证明 |
| actor_id / subject_id | 可信身份链提供的字符串 | 操作人/账户主体；家庭代办时可能不同，成员不能修改 |
| device_id / channel | 可信设备与渠道上下文 | 禁止跨设备/渠道复用；新渠道由负责人接入 |
| user_input | 长度不超过2000的字符串 | 不可信用户输入；里面的指令不能覆盖本契约 |
| entities | 受限结构化 Mapping | 只读，金额整数分；不是任意银行事实或凭据容器 |

Context标识字段当前限制为非空、无控制字符且不超过128字符。金额未知用 `None`，不填零冒充已核实。接口递归拒绝 float，`*_cents` 只接受真正的 int 或 None，bool/数字字符串不能冒充金额。

### 3.3 Finding 与 Proposal

| 类型 | 必须提供的字段 | 语义 |
|---|---|---|
| DomainFinding | domain、facts、missing_fields、risk_signals、recommendations、evidence | 来源化事实、缺失项、风险解释及非授权性建议 |
| DomainProposal | suggested_steps、required_confirmations、required_auth、user_options、production_replacement | 候选步骤、确认/认证提示、受限选项及生产替换点说明 |

构造时可传普通 list/dict，构造后递归冻结为 tuple/只读 Mapping；调用 `.to_dict()` 获取独立可序列化副本，不修改冻结对象，也不使用 `dataclasses.asdict()` 处理内部 MappingProxyType。

`suggested_steps` 每项仅允许 `tool`、`params` 和可选 `title`。没有“直接执行”“重试次数”“凭据”“覆盖策略”等字段。`production_replacement` 是必填的非空说明，不是生产就绪状态。

当前结构上限：遍历节点2048、深度12、单个字典/序列128项、字段名128字符、普通字符串4096字符、整数绝对值不超过2^63−1；规范化序列化后不超过131072字节。具体检查见 `app/domain_agent_contract.py:28`。这些是代码限制，成员不得自行放宽。

### 3.4 调用与异常

一次正常调用：平台准备 ctx/snapshot → 注册器创建本次独立实例 → analyze(ctx) → propose(ctx, finding) → Supervisor核对 → 原权限及执行链。

- 同一输入的分析应稳定可复现；不在 analyze/propose 中读取当前时间、随机数、全局请求状态或直接调用外部大模型。需要模型解释时由负责人提供受控的公共模型能力并另行审核接口。
- `ValueError` 等表示绑定/结构异常；不返回假的成功 Finding，不吞异常后补银行事实。规划阶段分析异常应停止本次执行；已有工具返回后的分析异常不能重新提交银行操作。
- `SnapshotAgent.validate_finding()` 会再次调用 analyze 比较结果，非确定性的网络调用或随机结果会导致校验不通过。新领域逻辑须考虑这一现状。
- 领域模块导入和构造不得联网、读写数据库、启动线程/子进程或加载大模型。每次 `create()` 建独立实例；不缓存不同客户/会话的 Context。

依据：`app/domain_snapshots.py:124`；`app/domain_agent_registry.py:38`；`app/supervisor.py:145`。

## 4. 事实与输入快照：当前严格兼容点

### 4.1 两种快照

| domain | 构造参数 | 由负责人准备的当前内容 |
|---|---|---|
| payment | PaymentSnapshot | 预检报告及Context摘要；六维：available_funds、daily_limit、beneficiary、card_status、authentication、budget |
| antifraud | DomainSnapshot | risk_score、risk_factors、aml_suspect、aml_count、recent_transfer_count，或工具已产生的detected/signals/is_new_recipient；字段以本次快照实际存在为准 |
| finance_health | DomainSnapshot | 转账规划中的budget/budget_status/coverage；财务工具返回后的consumer/cash_flow/funds/coverage，两种投影不是同一份完整家庭资产 |
| family | DomainSnapshot | grant与participants，或旧家庭成员工具返回的members；缺失授权不能凭亲属称呼补造 |
| subscription | DomainSnapshot | subscriptions：id、merchant、amount_cents、history、debit_state、terminable、service_state、valid_until、refund_state、refund_amount_cents |
| card | DomainSnapshot | cards：id、name、status；选择目标由ctx.entities提供card_id和card_operation |

DomainSnapshot的字段为domain/context_key/facts/evidence/status，status仅为known或unknown。非支付facts元素采用 `{name, value, source}`；支付facts由TransactionAgent按 `{check, status, evidence, source, source_kind}` 生成，不能用一种格式覆盖另一种。

快照提供器在 `app/domain_snapshots.py:36`、`:45`、`:57`、`:72`。支付快照在 `app/transaction_agent.py:25`。旧财务/订阅/卡片/家庭成员全局工具只接受其实际demo-user主体；成员不得改Context绕过这一限制。真实多用户数据源由负责人后续替换。

### 4.2 facts/evidence 不可擅自增加或改写

**当前Supervisor要求 Finding.facts 和 Finding.evidence 与本次平台预期逐项相等**，不是“字段大致相似即可”。继承SnapshotAgent时使用 `self.finding(...)` 保留原始快照；不要在facts里自行加新评分、新余额、OCR结果或RAG片段，也不要改排序/来源字符串。当前facts相等校验位于 `app/supervisor.py:151`，来源集合位于 `:19`。

成员可在既有事实基础上完善missing_fields、risk_signals、recommendations及已审核Proposal，但这些内容不是授权，也不能成为银行数值回执。需要新增可核验派生事实时，提交“字段名/类型/单位/来源/推导规则/未知语义/测试样例”的平台扩展申请，由负责人先扩展快照和监督校验，再发布兼容基线。

风险、财务等如需更深入算法而当前快照数据不够，必须明确提出数据需求；不能为了“接入成功”仅把新结果塞进自由文本，也不能让Supervisor无条件信任成员输出。P03知识引用已作为独立只读知识工具结果发布，含资料/片段ID、源文件行号、版本、SHA256及配置证据；它不是银行业务事实，不进入现有Finding.facts/evidence快照。领域侧阅读契约尚未开放，成员不要直接读取知识文件、取得模型密钥或提前依赖未发布的扩展。

### 4.3 Proposal 当前白名单

| domain | 当前候选工具范围 | 当前主体/参数绑定 |
|---|---|---|
| payment | resolve_beneficiary、check_transfer_constraints、create_transfer | beneficiary、amount_cents必须等于绑定实体，正式转账仍是原三步模板 |
| subscription | 仅在operation=cancel时提出cancel_subscription | subscription_id绑定；查询/取消都经黄色确认 |
| card | list_cards、report_card_loss或unreport_card_loss、verify_card_status | card_id及card_operation绑定；强认证由平台负责 |
| antifraud / finance_health / family | 当前不接收写工具候选 | 只读解释及审核过的用户选项；需要新工具由负责人扩展 |

确认标签只接受 `explicit_user_confirmation`；认证标签只接受 `strong_auth`、`owner_verification`。标签是提示，不能降低原策略要求。

当前user_options动作：review_risk_factors、human_handoff、review_budget、review_owner_grant、select_subscription、clarify_card_or_operation、adjust_amount、adjust_budget、defer、clarify、recheck_facts。普通选项仅含action；select_subscription还必须带本次快照中的subscription_id/label；clarify仅允许amount_cents/beneficiary/beneficiary_account字段。新增动作、参数或UI按钮由负责人联动接入。

依据：`app/supervisor.py:32`、`:107`。订阅/卡片当前建议并不直接生成可执行DAG，也不会因注册成功而覆盖原Planner。

## 5. 六领域各自的验收任务

下表是成员应交付的业务行为，不声明超出当前基础实现的能力已经完成。

| 成员/领域 | 必须验证的正常行为 | 必须验证的异常与边界 |
|---|---|---|
| 你/payment | 收款与金额澄清、六维预检、预算缺口解释、候选三步、工具事实回执 | 新收款人/未知卡状态不猜测；金额不静默修改；超时查原交易；不自动再转一次 |
| 董哥/antifraud | 解释已有风险因子、话术/新收款人/频次信号，提出核对/人工建议 | 不把大额或亲属称呼当诈骗定论；不自行重算权限或冻结账户；缺失来源明确未知 |
| 李狗/finance_health | 区分消费、现金流、可用/受限/处理中资金；说明预算缺口和coverage | 还款不重复算消费；退款和跨源关联遵守平台口径；预计收入不充当已到账余额 |
| 郭哥/family | 区分actor/subject，解释守护关系、授权类型、额度、有效期和撤销状态 | 缺失/过期/撤销授权阻断建议；“我是家属”不产生权限；不可跨账户复用快照 |
| 二狗/subscription | 区分历史交易、未来扣费授权、服务有效期、退款；缺ID只提供真实候选 | 未知退款不说到账；不可终止项不建议取消；不提前读取需黄色确认的数据 |
| 二狗/card | 对已知卡和状态提出挂失/解挂及核验步骤 | 未知卡/状态先澄清；不重复建议挂失；本页隐藏不是挂失；不跳过红色强认证 |

成员如已做超出上述范围的功能，在PR交付说明中分列“可直接接入”“需要公共接口”“尚未实现”；负责人据此安排平台扩展，不直接丢弃已有成果，也不把未接主链的功能写成已交付。

## 6. 注册、消息与内部移交

- 每个模块保留 `register(registry)`，注册自身类。类的 `__module__` 必须属于 `agents.<domain>_agent`，domain必须匹配；不得给导出的只读DOMAIN_AGENT_REGISTRY直接注册。
- 正常创建入口为 `DOMAIN_AGENT_REGISTRY.create(domain, trusted_snapshot)`。六域当前须全部注册成功才原子发布；开发一个域时保留其他五域已有实现，不删除“不是我负责”的文件。
- AgentMessage和AgentHandoff复用 `app/transaction_agent.py:110`、`:138`。Supervisor为领域结果创建信封，成员不伪造trace/session/sender/context_key。
- 内部移交原因只有missing_information/domain_boundary/unknown_result。它不等于真实IM消息已送达或客服已接单，也不能携带OTP、口令或完整聊天记录。
- 域名/模块路径不能从用户输入决定。注册器是受信Python代码协作边界，不是运行恶意插件的隔离沙箱；合并前仍需代码审查。

## 7. 可直接运行的本地离线示例

下面示例只构造虚构的离线测试快照，不查询银行、不联网。生产入口禁止把HTTP/LLM提供的字典包装成可信快照。该示例在项目根目录运行，可确认本地代码和调用方式匹配。

```python
import sys
sys.path.insert(0, "app")

from domain_agent_contract import DomainContext
from domain_agent_registry import DOMAIN_AGENT_REGISTRY
from domain_snapshots import DomainSnapshot
from transaction_agent import context_digest
from supervisor import Supervisor

ctx = DomainContext(
    "trace-contract-demo", "session-contract-demo",
    "demo-user", "demo-user", "demo-device", "web",
    "离线测试：请解释风险因素", {},
)
source = "risk_controls.py:assess_transfer_risk"
snapshot = DomainSnapshot(
    "antifraud", context_digest(ctx),
    [{"name": "risk_factors", "value": [{"code": "new_recipient"}], "source": source}],
    [source],
)
agent = DOMAIN_AGENT_REGISTRY.create("antifraud", snapshot)
finding = agent.analyze(ctx)
proposal = agent.propose(ctx, finding)
assert finding.facts == snapshot.facts
assert finding.evidence == snapshot.evidence
assert "new_recipient" in finding.risk_signals
assert not proposal.suggested_steps
review = Supervisor(DOMAIN_AGENT_REGISTRY).observe(ctx, "antifraud", snapshot)
assert review.status == "ready"
assert not review.reviewed_tools  # 观察通过也没有执行权限
print("contract demo: PASS")
```

其他领域在各自测试文件中用合成快照覆盖业务情形，完整参考既有 `tests/test_domain_snapshots.py`、`tests/test_<domain>_agent.py`。不要修改公共快照/监督器来让自己的测试临时通过。

## 8. 成员提交包与验收清单

每个PR正文必须包含：

1. 成员、domain、基线commit、分支名、修改文件清单。
2. 新增/修复的业务行为，与现有基础实现相比具体改变了什么。
3. 输入Context/快照示例、Finding/Proposal示例、正常与失败示例；只使用虚构数据。
4. 实际测试命令、运行结果、是否跳过、环境依赖；旧测试数字不是本次验收。
5. 新数据/工具/模型/依赖需求、未完成项和兼容影响。

负责人验收：

- [ ] 签名/domain/构造参数兼容，注册成功，六域仍能加载。
- [ ] facts/evidence与可信快照一致，未知不冒充确定事实。
- [ ] 金额整数分、无授权字段、无凭据、无越域工具与参数。
- [ ] 无银行/DB/网络/文件写/通知等副作用，无导入时线程或下载。
- [ ] 正常、缺失、未知、跨身份/上下文、来源篡改、重复调用/跨请求隔离都有测试。
- [ ] 本域行为测试确实验证业务差异，不能只检查类名和注册名。
- [ ] 原确认、强认证、预算重校验、幂等、审计、恢复和注入拦截回归通过。
- [ ] 存储、调度器、进程等测试资源正确清理；完整验收无环境跳过、无资源泄漏警告。
- [ ] PR范围和依赖符合本契约，合并后保存对应源码快照与实际记录。

普通本地检查（PowerShell，项目根目录；替换test模块名为本域）：

```powershell
$env:BANK_AGENT_OFFLINE = '1'
$env:VOICE_ENABLED = '0'
$env:PYTHONPATH = 'app;tests'
python -m unittest test_antifraud_agent test_domain_agent_contract test_domain_agent_registry -v
python -m unittest discover -s tests -v
git diff --check
```

macOS/Linux将PYTHONPATH分隔符改为冒号：`PYTHONPATH=app:tests BANK_AGENT_OFFLINE=1 VOICE_ENABLED=0 python -m unittest test_antifraud_agent test_domain_agent_contract test_domain_agent_registry -v`。

负责人完整回归：`python scripts/browser_qa.py --full`，须准备可选Node/Playwright/浏览器工具，核对0跳过和ResourceWarning。普通unittest或CI里的unittest不能自动替代此验收。现有CI文件 `.github/workflows/ci.yml` 包含静态检查、unittest、preflight和镜像构建；尚无远端运行成功证据，完整浏览器/真实外部联调不是现有CI已覆盖项。

## 9. 你先完成什么：平台先行阶段

按负责人本次意图，正式成员开发基线的发布顺序为：**你先补齐不依赖四位成员的公共平台 → 验收并发布GitHub基线 → 四位成员分支开发 → 你逐个集成 → 最终贯通和交付**。现有领域基础模块保留，可用于公共平台测试；成员不必先重做这六个模块才能启动平台开发。

“不依赖成员”不等于“不依赖外部资源”。截至本次用户说明：没有银行开放平台/测试环境、IM/客服或认证供应方，只有SiliconFlow大模型API资源。接口、替身测试和故障测试可以先做；真实系统联调、生产身份系统验收不能据此完成，也不能把大模型API当银行/客服接口。

| 建议顺序/编号 | 你独立负责的工作 | 复用基础 | 完成证据/外部条件 |
|---|---|---|---|
| P01 | 固定本契约、成员边界、基线版本和接入验证 | domain_agent_contract / registry / snapshots / Supervisor | 本契约、可运行示例和兼容检查；本批仅形成文档，不宣称新增平台功能完成 |
| P02 | SiliconFlow统一模型适配、结构化输出、超时/错误/预算/隐私控制 | app/llm_adapter.py:28、:77 | 虚构输入的真实API专项记录＋离线替身测试；需可用模型ID和本机安全注入的密钥，不能只证明配置存在 |
| P03 | RAG检索与来源体系 | 现有模型适配及审计边界 | 切分/索引/检索/引用、权限过滤、无命中拒答、检索注入测试；RAG资料不能成为余额或交易终态 |
| P04 | OCR接入与人确认 | 主入口、结构化实体与安全执行链 | 图片格式/体积/隐私校验、字段定位与置信度、低置信度/冲突人工核对；已选RapidOCR 3.10.0本地CPU引擎，接入与实测见《P04OCR接入与验收》；通用文本API不等于OCR |
| P05 | 音频hash证据与录音生命周期 | app/speech_adapter.py:26、语音同意流程、审计 | 对明确字节范围计算SHA256并关联trace/同意/格式；不同编码/规范化音频明确分开，hash不代表识别人或不可抵赖；留存/删除规则及泄露测试 |
| P06 | 主Agent动态DAG提案及监督审核 | app/task_dag.py:79、:128、:199、:551；固定Supervisor | 任意合法拓扑的结构提案→模式/环/节点上限/工具/参数/身份/预算/确认/补偿检查→原执行器；环、越权、缺依赖、运行中中断、未知结果和崩溃恢复测试 |
| P07 | 银行/卡管真实服务替换边界 | production_contracts、bank_service、原执行链 | 已提供本地协议Adapter、持久幂等、原请求查询、回调校验与只读对账，联通证据见《P07银行与卡管服务替换》；真实受理/状态查询/正式对账仍需具体银行协议、授权账户与环境，外部待接入 |
| P08 | 真实IM与客服接入边界 | app/channel_contracts.py、app/local_channel.py、app/handoff_queue.py | 已接入自建Web用户／坐席文字服务、原工单领取与结单、消息去重／分页／显示回执及审计；自动双浏览器与离线组合验收通过，真人双端按用户要求待验；第三方IM／银行官方客服仍未接入，见《P08自建IM与真人坐席》 |
| P09 | 生产身份与强认证接入 | production_contracts IdentityProvider / DeviceRiskProvider / StrongAuthProvider | 已增量接入可选自建密码＋TOTP、持久会话与浏览器设备绑定、角色／本人账户授权、操作摘要挑战及原子单次消费；接口及运行见9.10，最终验收见当前摘要。生产部署、硬件设备证明、真人独立验证器和机构认证仍待验，不把自建或固定OTP当作生产认证 |
| P10 | 性能与稳定性压测体系 | 现有测试、审计、熔断和临时数据库 | 统一本地压测工具与预登记阈值已实现，覆盖阶梯／持续负载、认证／客服、模型、限流、故障恢复及独立开源核心只读组；最终证据见9.11。模型API、持牌银行／第三方生产压测未测，不能用本地成绩替代 |
| P11 | 可复制基线、CI与离线发行 | 现有CI、T14打包与验收脚本 | 已提供当前工作区确定性源码快照、严格CI分组和离线发行工具；本批实际成绩与未验收边界见9.12及公开发行记录。GitHub上传／远端CI、物理异机仍须独立核验 |

P06中的“自由生成任意DAG”明确为**动态生成任意合法拓扑**；节点仍受已注册工具、类型、配额和原权限链约束，不开放任意Python/shell或无限规模图。自由规划不是绕过确认/认证、无限重试或改变资金事实的授权。

P02已在`app/llm_adapter.py`增量提供`ChatCompletionClient.complete_json(system, text)`和保留原协议的`LLMIntentAdapter.parse(text)`；配置、安全限额、真实API及回归证据见[《P02模型接入与验收》](P02模型接入与验收.md)。P03已提供平台`KnowledgeService`和“知识问答：问题”主链入口；扩展批增加可选embedding混合检索、中国银行两份公开产品摘要及家庭阅读上下文核验，具体完成证据/未验收项见[《P03RAG知识库与验收》第7节](P03RAG知识库与验收.md)。家庭阅读必须绑定实际actor、subject、有效关系和显式资料共享，不能从金额代办范围推断任意资料可读；私有/家庭资料不外发云端embedding或聊天模型；经核验授权的本地embedding与本地重排可处理可读资料。上述接口不授予领域Agent直接联网、读取模型密钥或执行工具的权限，现有Finding银行事实协议保持原样。P04已提供图片识别/人工核对草稿接口，见《P04OCR接入与验收》；P05已提供音频摘要、来源关联和受限真实录音验收，见《P05音频证据与生命周期》；P06已提供下文9.7的动态提案/采纳接口，P07协议接口见9.8，真实银行仍待接入；P08自建接口已落地，见9.9及《P08自建IM与真人坐席》；P09自建接口见9.10；P10压测入口与统计口径见9.11；生产容量未作承诺；扩展平台ABI须先实现、测试、通知成员。

依赖策略：默认标准库离线演示保持可跑；模型/检索/OCR/认证的新增第三方客户端须单列可选依赖与版本、安装方式和离线失败行为，经负责人明确确认后再引入，成员不得直接修改requirements文件。真实外部服务不开启时应明确不可用，不能偷偷调用公网或回落成“Mock成功”。

完整压测区分本地Mock、模型API、渠道/认证、银行沙箱与端到端几组；本地结果不外推真实银行吞吐。生产/第三方负载测试需要相应环境与服务方授权，不能向真实资金接口自动灌压。缺少环境的组标“未测”，不能让其他组通过覆盖它。

### 9.1 银行替换接口规格

以下银行方法已在 `app/production_contracts.py:58` 定义，真实实现替换Mock，不交给领域Agent直接调用：

| 方法 | 现有入参 | 真实实现必须补的验收行为 |
|---|---|---|
| get_balance | AdapterContext | 核对实际账户主体和数据范围，返回明确单位/时间的余额事实，不能用模型文本充当余额 |
| create_transfer | ctx、amount_cents、beneficiary_id、note | 操作人/主体/收款人/金额绑定；受理键幂等；超时不重新提交；受理标识与终态分开 |
| query_transaction | ctx、transaction_id | 只核查原交易；处理成功/失败/处理中/未知/撤销等真实服务状态映射，不能因查询失败推断资金失败 |
| reverse_transfer | ctx、transaction_id、reason | 仅在银行支持、状态允许且已授权时发起；不可撤销时明确拒绝；超时先查原冲正请求 |
| CardManagementAdapter三方法 | ctx、card_id | report_loss/unreport_loss/query_card接真实卡管；强认证、状态复核、幂等和审计闭环 |

P07已在AdapterContext原trace_id、actor_id、device_id、idempotency_key之后增补subject_id、account_ref、authorization_ref、authorization_version、operation_digest；旧Mock兼容默认值，BankServiceAdapter提交时必须有平台可信绑定。AdapterResult原字段不变。BankBinding仅支持本人账户，当前为本地协议演示映射，真实主体/银行授权和家庭代办仍未验收；不把秘密或授权声明塞到DomainContext.entities里。接口实际定义见`app/production_contracts.py`及`app/bank_service.py`，旧文档行号以本次源码为准。

真实Adapter的凭据、证书、地址白名单和会话由平台安全配置提供，日志只记录必要请求标识和脱敏状态。一次支付路径选择明确的银行/支付受理接口，不同时向多个Adapter提交同一笔资金操作。`retryable=True`也不授权自动重试副作用；未知结果必须只读核验原请求。

协议测试先用可注入替身覆盖：拒绝授权、金额类型、重复受理、响应丢失、处理中、状态查询失败、重复回调、冲正不支持及审计故障。真实联调再保存服务方环境、授权范围、脱敏请求标识及对账结果；缺少这些证据时只能标“协议测试通过/外部待接入”。

### 9.2 IM与客服替换接口规格（待实现，尚不可import）

| 计划接口 | 输入/输出约定 | 验收点 |
|---|---|---|
| 渠道入站校验 | 原始请求字节、必要头、接收时间 → 已验签的外部消息ID、渠道用户ID、时间和正文 | 签名/时效/重放拒绝；外部用户映射为可信内部身份；正文不能自报actor/device取得权限 |
| 渠道发送 | 经审核的脱敏消息、发送幂等键 → 服务方消息ID和送达状态 | 按服务方协议处理失败/未知与状态核验；不会再次发银行操作；严格区分已提交/已送达 |
| 客服建单 | 最小必要交接卡、来源幂等键 → 外部工单引用和状态 | 重复/超时只核查原请求，工单成功不等于银行成功；不能泄露OTP/token或原始敏感对话 |
| 工单查询/领取/结单 | 工单引用、可信操作者及设备、受限动作 → 原工单新状态 | 工单用户、坐席权限与版本/状态转移核验；结论不能把未知转账改成成功 |
| 客服回调校验 | 原始回调、签名信息与事件ID → 经核验的状态事件 | 拒绝伪造、重复、乱序、跨主体和过时回调；审计与状态更新一致 |

接口返回可用/不可用/超时/未知等受控错误，供应方字段先规范化再进入现有UnifiedMessage/接管投影；本地LOCAL_*状态与外部状态的映射须单独审核。平台账号、回调URL和服务方协议未确定前，不生成可用性承诺或伪造送达回执。

无第三方账号时也可以由负责人自建Web聊天和坐席界面，完成用户端—服务端—真人坐席的实际双向通信与接管验收。这是可独立开发的实现路线，不应因没有第三方平台就把所有客服开发停住；仍须有真人操作记录，不能把自动回复或本地建单冒充真人接通。具体采用自建还是外接尚未选定，本批只固定替换边界。

### 9.3 身份与强认证替换接口规格

复用 `production_contracts.py:45` 的IdentityProvider.verify_subject、`:49` 的DeviceRiskProvider.assess、`:53` 的StrongAuthProvider.begin/verify。真实供应方认证结果须绑定操作人、账户主体、设备、会话、操作摘要和挑战，明确有效期、尝试限制与单次消费。客户端不能自己发一个passed字段绕过服务方核验。

身份映射、凭据/挑战存储、密钥轮换、注销/过期/撤销、防重放和认证回调由负责人接入；领域Agent只收到核对所需的非秘密事实，不收到access token、密码、验证码或服务方密钥。第三方身份系统故障时，不回退固定OTP放行真实资金。

没有身份服务账号/测试租户时，可先实现适配边界与伪造/过期/重放/跨操作等替身测试，状态为“外部待接入”。“生产认证”若指外部认证/合规资质，而非登录和交易强认证，必须另有机构出具的证据，代码自测不能替代。

身份服务亦可自建：真实登录、会话、角色/主体授权、挑战与强认证的代码开发并不要求先取得银行接口。选择自建后应独立实施和测试，生产启用仍需安全部署、密钥管理、实际认证设备及运维验收；选择外接IAM时才需要相应租户与服务方环境。当前未决定具体路线，不把“尚无第三方IAM”误写成无法开发身份认证。

**发布闸口：**负责人先完成并验证能独立开发的能力；P07银行真实接入、P08第三方渠道及P09外接IAM在资源未落实时保持“待外部接入”，P08/P09自建路线分别以9.9、9.10和当前验收摘要核对自动与真人范围。若必须在上传开发基线前也全部真实验收，则银行等仍存在外部阻塞，负责人需补资源或明确调整阶段顺序；本文件不擅自将其降为已完成。资料可先供成员阅读，正式基线及开始开发时间由负责人发布。

## 10. SiliconFlow配置边界

现有LLMConfig从进程环境读取LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL / LLM_API_KEY / LLM_TIMEOUT_SECONDS及P02的输出/频率/并发上限，代码位于`app/llm_adapter.py`。P02增加受限JSON传输客户端；后续RAG及P06各有独立入口与验收，不能仅凭配置文本模型就推定这些能力完成。P06模型拓扑调用仍须额外明确同意，真实模型DAG推理未验收。SpeechAdapter也有独立条件；P05真实麦克风/ASR仅已验本次固定短句，证据见9.6，不能外推任意场景识别率。

配置约定（字段说明，不含任何实际密钥；`.env`能否加载取决于启动方式，Python适配器本身只读进程环境）：

```text
LLM_PROVIDER=siliconflow
LLM_BASE_URL=https://api.siliconflow.cn/v1
LLM_MODEL=Qwen/Qwen3.5-35B-A3B
LLM_API_KEY=<通过本机环境或密钥管理注入，不提交>
LLM_TIMEOUT_SECONDS=20
LLM_MAX_OUTPUT_TOKENS=512
LLM_CALLS_PER_MINUTE=20
LLM_MAX_CONCURRENT=2
BANK_AGENT_OFFLINE=1  # 默认离线基线；真实API专项另以显式非离线配置验收
VOICE_ENABLED=0
```

SiliconFlow官方接口为兼容Chat Completions的请求，Base URL不包含末尾 `/chat/completions`；现有适配器会追加该路径。上述35B模型于2026-10-23通过本账号模型列表和一次“Web→主Agent→模型→余额工具→审计”验收；主链采用成功、Mock余额、订阅与核心交易快照未变、审计链有效。该结果仅证明本次文本JSON链路，不能外推全部意图、视觉能力或复杂规划质量。其他模型仍须实际验证。依据：[SiliconFlow对话接口](https://docs.siliconflow.cn/docs/api/chat-completions-post)及本地`data/verification/p02-20261023/live-model.json`；复验命令见P02文档。

密钥不得写入代码、Markdown、截图、PR、测试fixture、公开日志或Git历史。聊天中出现过的密钥建议在平台轮换；文档不保存其原文。成员各自通过受控方式配置凭据，不把一个明文密钥复制给所有人。未启用、无权限、欠费、限流或超时须有明确失败路径，不能影响资金权限或自动重试写操作。

## 11. GitHub协作：同一仓库、独立分支、负责人集成

### 11.1 分支约定与真实状态

团队采用下述流程；这是本次确定的协作规则，实际创建/保护尚未执行。目前本地只有main，无远端配置，且有未提交与未跟踪成果；HEAD不是全部当前代码。发布前先逐文件核对范围与隐私，保留五个既有删除现状，不用恢复或重写历史来“清理基线”。

- `main`：已验收的稳定/竞赛交付版本。
- `develop`：负责人逐个合并成员PR的集成分支；合入后立即运行集成检查，不等四个人全部交完才第一次联调。
- `feature/<成员>-<领域>`：从已发布的develop基线创建，仅包含本域改进。负责人公共功能使用独立平台分支。
- 成员是同仓库协作者：clone后在自己的电脑建功能分支并push到该仓库，PR目标是develop；不要求每人另开一个仓库。无写权限时可另行选fork流程，不把fork和branch混为一谈。

负责人保留合并权；成员不直接push main/develop、不替别人批准自己的业务变更。GitHub分支保护和必需检查能否强制执行取决于仓库可见性、权限和套餐，不能假定已生效。默认建议成员PR至少1名其他人审查；负责人自己提交的平台PR也需其他指定成员审查，不能把“自己批准自己”当有效审查。根据实际支持配置，而不是简单关闭全部审查。

依据：[GitHub分支保护](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)、[branch与fork区别](https://docs.github.com/en/pull-requests/reference/forks)。这两页仅支持协作机制，不证明本项目仓库/权限已配置。

### 11.2 成员操作示例（未来执行，本批不运行Git写操作）

前置：负责人已给仓库实际地址、权限、完整开发基线和指定基线commit；成员在新的干净目录clone。以下占位符必须换成实际值，不能用本地f9d0cc2冒充未来发布基线。

```bash
git clone <负责人提供的仓库URL>
cd <实际克隆目录>
git fetch origin
git switch --track origin/develop
git switch -c feature/dongge-antifraud
# 阅读契约，在已有antifraud模块和自己的测试上增量修改，运行第8节检查。
git status --short
git diff -- app/agents/antifraud_agent.py tests/test_antifraud_agent.py
git add -- app/agents/antifraud_agent.py tests/test_antifraud_agent.py
git commit -m "feat(antifraud): improve reviewed domain analysis"
git push -u origin feature/dongge-antifraud
# 在GitHub创建PR：base=develop，compare=feature/dongge-antifraud。
```

已存在本地develop时用 `git switch develop`，在工作区干净且分支无本地未推提交时 `git pull --ff-only`；不要重复执行 `--track`。其他成员替换第1节分支和路径；二狗分别提交两个域。上面代码块是成员操作说明，不构成助手现在执行commit/push的授权。

同步其他域最新改动：先提交自己已完成的本域改动，再在自己的功能分支 `git fetch origin`、`git merge origin/develop`；遇到冲突停下查看，公共契约冲突交负责人协调。不要以删文件、强推、reset --hard或覆盖整个目录解决。个人分支提交不等于测试已通过或负责人已验收。

### 11.3 负责人本机贯通

1. 发布前记录完整基线commit与源码哈希，确保六域、契约、配置、必要语料和说明都在经审查的发布范围，排除密钥、日常DB及过程目录。
2. 每个PR先审diff/归属/接口/来源/依赖，再在干净集成工作区检出测试；不覆盖负责人当前有未提交成果的工作区。
3. 本域测试＋契约/注册器＋Supervisor/主链测试通过后合入develop；每合一域做针对性集成，关键节点跑完整浏览器/恢复回归。
4. 全员交付后进行组合场景、故障、认证、动态DAG和恢复贯通，重建最终镜像及离线包；成员单域通过不能替代此阶段。
5. 功能冻结后同步PPT、视频、技术文档、安全自评、部署与测试证据，审查后把已验收develop合入main并按最终授权发布/上传。

GitHub上传源码并不附带Docker引擎或全部外部环境；离线包和真实外部服务配置按各自交付物管理，不能承诺任何电脑下载源码就能离线使用真实银行或在线模型。

## 12. 变更、交付状态与本批范围

当前ABI未修改。新增ABI字段、快照、证据类型、工具、选项或依赖须提出变更申请：原因 → 新旧输入输出 → 兼容/迁移规则 → 测试 → 负责人发布新版本和基线。未发布的扩展只在平台分支开发，成员不得自行依赖同名自定义类。

本次用户将RAG、真实银行、真实IM/客服、生产身份认证、动态DAG、OCR、音频hash及完整压测纳入目标，取代先前“RAG暂缓”的安排；这些是项目扩展目标，不自动成为官方额外硬要求。截止仍为2026-11-01，2026-10-31完成上传复核；全部新增目标能否在此前真实验收尚无证据，尤其外部资源未确定，不能承诺固定天数全部完成。

成员提交时将每项标为“未开始/开发中/本域测试通过/集成通过/外部待接入/最终验收”，并填基线、测试报告和剩余阻塞。当前队员在个人电脑上的代码与完成度未找到本地证据；本文件仅确认职责，不代填成员进度。

本批产物是本契约及关联文档校准，不创建远端、不邀请成员、不发送群消息、不修改银行代码、不执行Git提交/推送，也不使用API密钥或运行真实资金/负载请求。负责人下一批优先启动P02统一模型适配与P03检索基础，并同步完成P07–P09替换接口设计；平台接口/测试就绪后再按发布闸口安排成员正式基线。


### **9.5 P03/P04平台接口补充（2026-10-24）**

- **RAG**：新增可选本地cross-encoder重排，继续使用既有知识问答入口。120题为合成回归；真人样本待测。本地负载测试仅覆盖本机CPU/Mock，不能代替P10全部外部测试。细节见《P03RAG知识库与验收》第9节。
- **OCR**：GET `/api/v1/ocr/capabilities`；POST `/api/v1/ocr/extract`上传PNG/JPEG原始字节并带可信会话及`X-OCR-Consent: yes`；POST `/api/v1/ocr/review`显式确认字段。review只返回未执行草稿；可选`ocr_review_token`随原`/api/chat`关联来源trace，一次性且绑定用户/设备。OCR只支持本人账户草稿，家庭代办拒绝。
- **成员边界**：领域Agent继续接收现有DomainContext、产生原Finding/Proposal，不读取图片、OCR内存缓存或模型密钥；不将识别字段当BankFact，不自行调用银行工具。所有交易仍通过Supervisor、PolicyEngine、确认、必要认证和原执行器。
- **账户核对**：图片卡号只显示掩码；存在账户信息时强制用户重新选择平台已核验收款账户，不按同名联系人自动替换。当前不是任意真实收款卡号导入功能。


### **9.6 P05音频来源契约补充**

- 语音仍是未受信输入。三个摘要分别对应上传WAV、解码PCM和去元数据标准WAV，不代表说话人认证或资金事实。
- 原语音接口增加本地证据模式；云识别须同时具备本次本机和云端同意。旧客户端未带云同意时只返回本机摘要，不静默外发。
- 原chat可带voice_source_token与严格布尔voice_reviewed，令牌绑定实际用户和设备、300秒、一次性。平台关联voice.linked后仍走原权限/确认/认证链。
- 成员DomainContext/Finding/Proposal ABI不变，不直接接收录音、转写缓存或令牌，不把摘要塞入BankFact。用户取消只清理本应用采样与请求引用，不宣称供应商已删除。
- **供应商处理**：服务端生成`asr_`请求标识并核对响应追踪头；`POST /api/v1/voice/deletion-request`只能为当前本人/设备、审计链完整的来源生成处理/删除申请草稿，不发送邮件。ASR逐条录音删除API未找到官方文档证据；音色删除接口不能代用，`deletion_verified`保持false。依据`SpeechAdapter.transcribe/provider_retention_info`、`AgentRuntime.voice_deletion_request`。
- **真实验收**：`data/verification/p05-real-20261024/physical-cloud.json`记录用户原生麦克风、试听/勾选/发送/核对；最终6.57秒固定短句，HTTP200、追踪回显一致、CER=0，累计2次云调用。仅保存元数据；这不是生产身份认证、泛化准确率或云端擦除认证。

### **9.7 P06动态任务契约补充（2026-10-24）**

- **入口**：`POST /api/dag/propose`生成/审核结构；`POST /api/dag/adopt`带原计划token采纳，撤销旧确认和认证并返回新确认卡。采纳不会执行业务；之后复用原confirm/verify/DAG控制接口。
- **范围**：在原请求全部步骤及可信实体上调整拓扑，支持合法分叉/汇合、原策略允许的额外只读检查、人工门、有界延时；最多32节点/128边/16层。不能遗漏原检查、重复付款、伪造参数或调用任意代码。家庭代办不扩权，模型不裁决三色权限。
- **执行**：`dag_planner.review`→`Supervisor.review_dynamic_dag`→`AgentRuntime.dynamic_dag`→原执行器；执行前核对拓扑摘要，恢复重新确认，未知结果不重试。延时到点仍须手动继续，不是后台定时自动扣款。
- **成员接口**：原DomainContext/Finding/Proposal ABI不变；领域Agent不直接提交执行凭证或执行DAG。详见《P06动态DAG提案与监督审核》及当前验收摘要；真实模型生成、跨业务自由组合与生产并发调度仍未验收。

### **9.8 P07银行与卡管协议边界补充（2026-10-24）**

- **可用范围**：`BankServiceAdapter`与`ProtocolBankFacade`已接入原本人余额、转账及卡管执行链；默认仍是Mock，协议演示须显式注入LOCAL_PROTOCOL_TEST Transport且offline=True，生产/live模式拒绝启用。服务失败不回落Mock余额或成功，不支持的域工具在协议执行阶段拒绝。
- **受理与恢复**：持久预留本地请求/幂等键后才发一次；重复同键读取原记录，修改业务参数拒绝；响应丢失保留unknown与本地引用。恢复只查原请求；卡管处理中/未知不继续验证或宣称完成。真正银行端原子幂等和资金终态仍需外部证据。
- **接口**：BankCoreAdapter/CardManagementAdapter原方法保留；新增Adapter的query_by_idempotency、内部apply_callback及reconcile_statement。受信会话下GET `/api/v1/bank/capabilities`与`/api/v1/bank/reconciliation`；未配置时明确不可用。回调为项目协议HMAC方法，未开公网供应方回调入口；对账只报告请求级差异，不补扣/退款或改资金事实。
- **成员边界**：成员ABI不变，不接收账户凭据、授权引用或原始银行请求；Finding只认原平台工具事实。正式Transport/银行SDK、证书、可信主体/设备授权、银行风控、正式对账与真实回调仍是后续接入闸口。当前证据见《P07银行与卡管服务替换》及验收摘要，不能标真实银行已接入。
- **独立开源核心扩展**：已增加`FineractTransport`，接固定提交的Apache Fineract/PostgreSQL，实际核心25项检查见`data/verification/p07-public-20261024/reusable-core-qa-final.json`。只支持本人储蓄账户路径，卡检查可在受信证据下明确不适用，不能伪造银行卡正常；卡管未接。实际原生转账/查询/双边undo和WebHook已验，回调仍是未受信线索，账务以认证查询为准；不是正式银行证书、对账单、签名或实际资金验收。默认Mock和成员ABI保留；P08已完成联合自动验收及离线发行，当前版本见《P08自建IM与真人坐席》第3.4节。

### **9.9 P08自建IM与坐席边界补充（2026-10-30）**

- **实现与入口**：复用原HandoffQueue、身份和trace，用户从原一键人工客服入口留言；坐席页 `/support/operator` 使用本机受信运维会话，领取工单后收发文字。新增 `POST /api/v1/support/send`、`poll`、`ack`；字段、权限、分页及错误处理见《P08自建IM与真人坐席》第2节。
- **成员边界**：DomainContext/Finding/Proposal不变。领域Agent不得读取坐席令牌、客服正文或直接调用IM／银行副作用接口；客服文字不进入Agent执行链，结单不执行银行业务，不把未知交易改成成功。
- **完成口径**：专项11项、全量730项、镜像禁网65项、标准包迁移53项与新卷核心组合25项检查通过；源码哈希与最终镜像绑定，见 `data/verification/p08-20261024/report.json`。这些是本机自动证据；真人双端为 `PENDING_HUMAN_USER_DEFERRED`，物理第二台电脑、商业IM、银行官方客服及生产认证未计通过。

### **9.10 P09自建身份与强认证（2026-10-30）**

#### **9.10.1 实现范围与权限**

- 【事实】在 `app/identity_service.py` 增量实现原 `IdentityProvider.verify_subject`、`DeviceRiskProvider.assess`、`StrongAuthProvider.begin/verify` 契约。受保护HTTP请求先校验会话及设备，再调用身份／设备Provider；高风险操作在原 `AgentRuntime.confirm/verify` 中发起、核验、消费强认证，不绕过PolicyEngine、确认、熔断或执行器。
- 使用 `IDENTITY_MODE=self-hosted` 显式启用；默认仍为 `demo`，兼容既有离线场景。非法模式拒绝启动；`APP_ENV=production/prod` 不接受该自建演示模式，不回落到固定OTP。成员DomainContext/Finding/Proposal ABI不变。
- 银行业务只绑定现有虚构账户 `demo-user`；其他已登录主体没有此账户授权。`admin` 坐席仅能使用原工单／通信接口，不能读取本人账户余额或发起银行业务。家庭代办在此身份模式中明确拒绝，保留原demo模式的家庭演示；不能把家庭演示Bearer当真实家庭身份。

#### **9.10.2 凭据、会话、设备与强认证**

- 密码15—128字符，独立随机盐，PBKDF2-HMAC-SHA256 600000次；不存明文密码。TOTP为RFC6238/HMAC-SHA1、6位、30秒，容忍前后各1步，按账户持久记录最后接受的计数器，成功的动态码不再接受。登录用过的码不能继续批准转账，等下一枚；没有固定成功码，也没有HTTP下发TOTP种子或当前码的接口。
- 登录须同时核验密码和TOTP。会话及设备各256位随机值，SQLite只存摘要；Cookie为HttpOnly、SameSite=Strict、主机专用。会话15分钟闲置／8小时绝对到期；登录轮换已有会话，退出、账户撤销、凭据轮换及账户版本变化后旧会话失效。此处设备是浏览器持有凭据的绑定，不是硬件证明；同时窃取两个Cookie仍存在劫持风险。
- POST必须同源Origin，身份登录接口有客户端及账户失败限制；登录失败5次锁5分钟，客户端5分钟最多20次尝试。强认证挑战绑定实际主体、账户主体、设备、会话摘要、操作摘要和credential_id，5分钟到期、每挑战最多3次错误；跨挑战账户5次错误锁5分钟。限制持久保存，重启不清零。
- 本地银行设备映射仅在身份／设备Provider均通过的请求上下文中扩展原可信BankBinding，保留原账户、授权引用、版本和原设备许可；请求结束即撤回。GET查询与原执行链共享该设备上下文，不能关闭银行设备白名单来接入新登录。真实银行是否承认此设备仍未验收。
- 因子核验与TOTP计数器更新在SQLite写事务中原子提交；执行前再核对会话、主体、设备、摘要、期限、已通过状态，以单向UPDATE消费挑战。原AuthCredentialStore及恢复检查仍保留。崩溃后的不确定结果只能查原交易，认证失败不触发重做资金业务。
- 审计记录主体、设备、挑战、摘要、方式、拒绝／轮换／撤销，不写密码、TOTP种子、动态码或原始Cookie。因子、会话及消费的关键变更与其审计同事务；审计失败回滚／拒绝。数据库或身份服务异常返回503，不降级为演示身份。

#### **9.10.3 HTTP与本机启动**

| 方法／路径 | 用途与限制 |
| --- | --- |
| GET `/identity/login`、`/identity/client.js` | 本机登录页与同源脚本；密码／动态码不写浏览器存储 |
| GET `/api/v1/identity/capabilities` | 公开当前模式与生产／硬件／家庭边界 |
| POST `/api/v1/identity/login` | 仅JSON的 `username,password,code`；成功只设置Cookie，不返回令牌 |
| GET `/api/v1/identity/me` | 当前主体、角色与设备标签；无原始会话、种子或验证码 |
| POST `/api/v1/identity/logout` | 撤销当前持久会话并清Cookie |
| 原业务／坐席接口 | 使用上述会话；客户端不能通过user_id、device_id或Bearer伪造身份 |

- 本机首次开通：`python app/identity_service.py provision --directory data/identity-demo --user demo-user --role user`。交互输入全新的本机密码（不要使用银行密码／API密钥），种子仅写 `data/identity-demo/demo-user-enrollment.json`，手动录入独立验证器的账户、密钥、SHA1、6位、30秒参数。这个私有文件及SQLite库不能放入Git或交付包。
- 坐席同法开通：`python app/identity_service.py provision --directory data/identity-demo --user local-operator --role admin`；应使用独立密码与验证器种子。角色只由本机受信CLI开通，HTTP没有注册或提权接口。
- 启动：`python app/identity_service.py serve --directory data/identity-demo --port 8092`；访问 `http://127.0.0.1:8092/identity/login`。用户登录后进入原首页，坐席登录后进入原工作台，点击使用已登录会话即可，不输入演示令牌。
- 撤销：将action换为 `revoke --user demo-user`；密码／种子轮换与解锁：使用 `rotate --user demo-user`，交互确认新密码并重新登记生成的新种子。旧会话全部废止，角色保持原值。会话／设备失效后重新登录、重新核对计划；未知银行结果先查原交易，不能盲目重做。
- 默认包与自建身份配置分开：`.qa-tools/offline-releases/p09-20261030-final-r2/` 为默认离线演示包；配合 `.qa-tools/offline-releases/p09-identity-20261030-final-r2/` 的独立Compose配置可启用自建身份。运行与容器内开通步骤见对应START_HERE，目标机只需兼容Docker Linux引擎／Compose，不需宿主Python或联网依赖。

#### **9.10.4 验收和未完成边界**

- 自动专项覆盖RFC向量、密码／角色、OTP重放、设备／会话不匹配、期限、失败次数、撤销／轮换、跨主体、操作变更、并发单次核验、审计故障、原交易确认与重启、浏览器HttpOnly／同源／坐席隔离；命令及最终源码／镜像记录以 `data/verification/p09-20261030/report.json`、`docs/验收摘要.json` 为准，不把说明中的历史数字当作新版本验收。
- 【待验证】真人在独立验证器／真实第二设备完成登记、登录和交易挑战；本机脚本计算TOTP及自动浏览器不能代替真人多设备验收。生产TLS／反向代理、硬件／WebAuthn、防钓鱼认证、真实身份实名／机构授权、独立KMS托管TOTP种子、恢复码／账户恢复、生产HA和外部认证仍无通过证据。
- 私有TOTP种子目前存本机受保护目录及SQLite，未做独立KMS封装。文件权限依赖目标OS／容器数据卷；Windows ACL未做生产认证。当前HTTP仅限本机回环，HTTPS来源才设置Secure Cookie；本机HTTP不算生产TLS。不要把此自建实现声明为生产认证或持牌银行身份。
- 依据：[RFC6238](https://datatracker.ietf.org/doc/html/rfc6238)、[OWASP密码存储](https://cheatsheetseries.owasp.org/cheatsheets/Password_Storage_Cheat_Sheet.html)、[OWASP会话管理](https://cheatsheetseries.owasp.org/cheatsheets/Session_Management_Cheat_Sheet.html)。仅将其作为实现参考，不声称通过标准认证。

#### **9.10.5 本批最终验收快照**

| 检查 | 实测结果 | 记录 |
| --- | --- | --- |
| P09专项 | 22项通过，0跳过／ResourceWarning | `identity-tests.json` |
| 全量回归 | 752项通过，0跳过／ResourceWarning；源码冻结哈希一致 | `full-suite.json` |
| 最终r2镜像 | 86项禁网只读测试通过，0跳过／ResourceWarning | `docker-core.json` |
| 标准包迁移 | 53项检查通过，260项资产匹配 | `data/verification/t14-20261030-172732-386741/report.json` |
| 独立身份配置 | 32项实际HTTP、撤销、角色及重启检查通过 | `identity-profile.json` |
| 身份＋独立Fineract核心 | 19项通过；虚构2000元双边验证并显式撤销恢复 | `fineract-identity.json` |
| 启动工具补修 | 用户额外批准仅补一行标准库导入，start实际完成 | `startup-tool-fix.json` |
| 真人／生产 | 独立验证器、硬件设备、生产认证和物理异机保留待验 | `report.json` |

- 短文件名位于 `data/verification/p09-20261030/`；当前联合报告为 `report.json`。当前r2镜像 `sha256:9ea848d2cbd5e42efa1acb32bac5718b8944eaa5a5369ade188226e74af021f7`。无r2后缀的P09包及before-device-map记录为修复前快照，不代替最终验收。
- 复用Fineract中4条历史银行记录与本轮新请求台账不同，真实保留MISMATCH；本轮请求无对账差异，显式清理后双方余额恢复。没有以修改资金、补扣或把差异改成成功的方式通过测试。
- 最终原范围8份文件加获批启动工具1份，共9份。未新增依赖、未commit／push；Git HEAD、成员ABI、日常数据库和五个删除文件保持原状。自建身份模式暂不开放家庭代办，默认演示中的家庭功能保留；真人TOTP与生产认证不计自动通过。

### **9.11 P10性能与稳定性压测（2026-10-30）**

#### **9.11.1 计划、环境与复现**

- 【事实】新增 `scripts/platform_load_test.py`，复用现有AgentRuntime／Handler、原权限与执行链、P09身份Provider、P08工单、`rag_load_test.percentile`及`drill_recovery.RecoveryDrill`；不改成员ABI，不重造交易执行器。`scripts/fineract_load_test.py`复用P07固定FineractAPI，只发GET，构成独立开源核心只读组。
- 正式工程阈值先写入 `config/performance_plan.json` 并保存在验收目录 `preregistered-plan.json`；每次运行再写不可覆盖的 `frozen-plan.json` 及SHA256。阈值属于L3本机交付目标，不是L1赛题规定或生产SLO；失败不能通过改阈值转成通过。
- 主压测限4CPU／4GiB、Docker Linux/amd64、禁止外网，SQLite与崩溃恢复使用独立Linux Docker具名数据卷，不使用tmpfs数据库或日常数据。输出记录写本机验收目录；测试卷只在所属进程结束后移除。报告列出Python／模型依赖版本、CPU型号、cgroup配额、源码与模型清单摘要。主进程CPU/RSS包括服务端和负载生成器，不能当成单独服务端利用率。
- 负载为有界闭环，不模拟固定外部到达率，也不宣称生产容量。1／2／4／8并发各10秒、每档最多200个逻辑操作；触及操作预算会提前结束，报告真实时长和budget_exhausted，不能写成完整10秒。持续负载4并发、目标2逻辑操作/秒、300秒、上限1000操作，等待在途请求结束计入实测时长。
- 逻辑工作比例固定6成余额查询、3成完整1分钱转账、1成本地客服领取／留言／结单。转账始终先计划、显式确认，再检查旧确认拒绝；查询和客服不能扣款。并发重复确认另用10笔／80次请求检查单次执行。逻辑操作、HTTP请求及拒绝数量分别报告，不把1次转账中的多个HTTP请求当作多笔转账吞吐。

#### **9.11.2 统计和验收口径**

| 项目 | 冻结目标／解释 |
| --- | --- |
| 成功吞吐goodput | 成功逻辑操作/真实运行秒数；429和预期拒绝不计入成功 |
| 总吞吐throughput | 全部尝试/秒；与成功吞吐并列，不能混用 |
| 延迟 | 全部样本P50/P95/P99，含失败请求耗时；无样本为null，不能当0ms通过 |
| 错误／超时／未知 | 主负载各为0；HTTP200但业务失败仍算错误；包装在URLError／供应方异常中的超时沿cause/reason识别 |
| 单并发 | 查询P95≤1500ms、完整转账P95≤5000ms、客服P95≤1500ms；整体P99≤10000ms |
| 持续负载 | 实际运行≥299秒、P95≤5000ms、成功吞吐≥0.5逻辑操作/秒 |
| 资金与审计 | 实际扣款分数／交易笔数与成功1分钱操作精确一致，重复扣款为0；校验最后事件所属trace的全链前缀，覆盖此前所有散列事件 |
| 资源 | 主进程峰值RSS≤3GiB；持续阶段末较初RSS增加≤256MiB；另报CPU秒数／单核百分比，不以CPU低当成正确性通过 |
| 身份 | 12个不同虚构主体的真实密码＋TOTP登录，4并发，P95≤3000ms；不向这些主体授予银行账户，不把脚本TOTP当真人验证器验收 |
| 模型 | 单并发RAG P95≤10000ms、独立重排应用率≥95%；高并发降级、busy和完整重排goodput单独报告。OCR P95≤15000ms，固定合成图片字段正确且不办理业务 |
| 冷启动／恢复 | 3次新进程／新库启动至ready≤30秒；原C8强制终止／重启RTO≤30秒、已确认等待任务不丢、完成交易不重复。OS文件缓存不清除，不称物理冷盘；分位数为当前样本的经验统计，不承诺生产尾延迟置信度 |

- 容量组仅在压测Harness内提高HTTP限额至100000，原业务权限、确认、认证和审计继续生效；这是隔离容量测量，不能宣称默认部署支持此速率。默认限流单独保持120次／60秒配置，以144次突发核对成功与429，未修改服务配置文件。
- 故障组明确注入Mock processing，查询不能重发扣款；只有测试代码显式模拟供应方终态更新后才出现一次扣款。3次错误认证触发原熔断且无扣款。故障注入中的预期processing与主压力场景的非预期unknown分别列出，不混在零未知成绩中。
- RAG记录新进程首次知识请求及清空内存索引后的重建请求耗时；它们含原HTTP／审计／检索与重排开销，不冒称纯模型或纯索引时间。OCR固定既有合成图片SHA256，音频为本地合成WAV的规范化和hash，不请求云端ASR。
- Fineract只读组对固定虚构账户运行1／2／4／8并发，每worker最多50个GET，最多750次；沿用单并发查询P95≤1500ms、错误／超时0及双边余额不变闸门。它不覆盖真实银行、平台认证后转账吞吐、写入容量或长时间银行业务稳定性。

#### **9.11.3 命令与交付边界**

- 固定合成图片随源码交付于 `docs/assets/P10合成OCR样本.png`，内容仅为虚构演示字样、妈妈和123.45元；先复制到 `data/verification/p10-run/ocr.png` 再运行。SHA256与最终报告对应，不需要私人图片或外部下载。
- 主工具只接受本项目验收输出／图片路径，无任意target_url参数。先安装已批准的本地模型依赖及模型资产，或使用包含这些资产的离线镜像，再执行：`python scripts/platform_load_test.py --plan config/performance_plan.json --output data/verification/p10-run/report.json --ocr-image data/verification/p10-run/ocr.png --workdir data/verification/p10-work`。输出目录须为新目录；正式复验不得覆盖原冻结计划。
- Docker封版运行：用最终镜像、`--network none --read-only --tmpfs /tmp --cpus 4 --memory 4g`，为`/app/data`挂独立测试卷、验收输出单独挂载；按默认离线包配置RAG_EMBEDDING_BACKEND=local／本地重排／OCR。不得用日常数据库或导出带密钥的运行卷。
- 原生只读组：先按P07说明启动已批准的本地虚构核心，再执行 `python scripts/fineract_load_test.py --directory data/verification/p07-public-20261024/runtime --output data/verification/p10-run/fineract-read.json`。它读取私有运行配置但不打印凭据，不新增账户，不发POST／PUT／DELETE。不得改指向真实银行或网关。
- 【未测】付费模型／embedding／ASR API负载、持牌银行沙箱、商业IM／外接IAM生产负载、开放到达率／分布式容量、真人语料及物理第二台电脑。既有功能测试或本地压测通过不能覆盖这些组。完整输出与版本对应以 `data/verification/p10-20261030/report.json` 和当前验收摘要为准。

#### **9.11.4 最终验收快照**

| 检查 | 实测结果 |
| --- | --- |
| 指标／边界专项 | 10项通过 |
| 全量回归 | 762项通过，0跳过／ResourceWarning |
| 最终镜像 | 96项禁网只读测试通过，0跳过／ResourceWarning |
| 标准包迁移 | 53项通过；264项源码／模型／依赖资产匹配 |
| 冻结阈值 | PASS_LOCAL_SCOPE；与预登记计划一致 |
| 持续负载 | 600个混合逻辑操作／302.19秒；成功吞吐1.986/秒；P95 3198.00ms，P99 4094.97ms |
| 资金／审计 | 240分实际扣款精确匹配；重复扣款0；最新事件全链前缀核验通过 |
| 默认限流 | 120成功／24次429，拒绝不计成功吞吐 |
| 原生开源核心 | 750个只读请求，双边余额未变；不是写入或持牌银行容量 |
| 公开证据 | docs/assets/P10性能压测验收.json；固定图片与SHA256一并交付 |

| 主混合负载并发 | 样本数 | 实际秒数 | 成功逻辑操作/秒 | P95 ms | P99 ms | 错误／超时／未知 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 53 | 10.05 | 5.276 | 437.60 | 461.86 | 0/0/0 |
| 2 | 54 | 10.10 | 5.346 | 938.86 | 1041.21 | 0/0/0 |
| 4 | 51 | 11.38 | 4.480 | 2043.29 | 2124.93 | 0/0/0 |
| 8 | 49 | 11.85 | 4.134 | 2809.66 | 2891.85 | 0/0/0 |

- 当前记录 `data/verification/p10-20261030/report.json`，正式明细 `acceptance-r2/report.json`，原生只读 `fineract-read.json`。smoke为内存临时目录短程，smoke-disk因1秒缺客服样本未通过延迟闸门；第一轮acceptance为测量增强前快照，均保留，不代替当前最终镜像成绩。
- 离线包 `.qa-tools/offline-releases/p10-20261030-final/`，迁移副本 `.qa-tools/offline-releases/迁移复验 P10 20261030/`；最终镜像 `sha256:41f179bfe1371caae3d32aa9bb7251fdf6743d2b1c0267af8b713b491cf0ceb2`。源码目录中的公开证据和图片随Git交付；原始data日志不自动入库。
- 当前8份文件改动，未新增第三方依赖，未commit／push；日常数据库、Git HEAD、成员ABI和五个删除文件保持原状。本地工程闸门不替代外部生产组、真人评测、生产认证或物理异机验收。

### **9.12 P11可复制基线、CI与离线发行（2026-10-30）**

#### **9.12.1 基线与团队交接**

- 【事实】目标仓库由负责人提供为 `https://github.com/gwenxin236-tech/bank-agent`。提供URL不能证明已经上传、配置远端、设置成员权限或运行Actions；本批继续不commit／push，真实远端状态见发行记录。
- 【事实】`scripts/source_release.py`按白名单读取当前工作区，包括尚未提交／未跟踪的代码；不使用旧HEAD冒充最新版本。ZIP固定文件顺序、时间和权限，同一内容应生成相同SHA256；`SOURCE_MANIFEST.json`逐文件记录字节数和SHA256，snapshot_id为文件描述清单的规范JSON摘要。
- 源码快照包含app／config／scripts／tests／eval、CI和文本设计文档、明确允许的合成OCR图片；排除Git历史、私有目录、实际.env、数据库／WAL、验收过程目录、模型权重／wheel、竞赛Word／PPT／视频。附件交付与开发源码分开管理，不能把源码ZIP称为完整竞赛材料包。
- `python scripts/source_release.py build .qa-tools/offline-releases/source.zip`生成新包；`verify <ZIP>`校验；`extract <ZIP> --destination <全新目录>`先完整校验再解包。拒绝既有输出、路径穿越、重复／大小写冲突、符号链接、缺项／额外项、内容篡改和超限资产。
- 扫描复用`release_preflight.scan_text`。阻断级密钥特征使打包失败，不输出其值；REVIEW／FIXTURE_REVIEW保留相对路径／行号／规则，不能自动认定隐私审查通过。校验和证明内容一致，不是发布者签名，也不替代人工审查；Git旧历史另审，源码ZIP不携带历史。
- 成员从批准发布的同一源码snapshot起步，按既有领域契约在自己的feature分支开发并提PR；支付／反诈／财务健康／家庭授权／订阅及卡片职责不变。`.github/CODEOWNERS`目前仍是待替换的团队占位账号，不代表成员真实GitHub权限已经设置。

#### **9.12.2 CI执行与证据**

- 【事实】`.github/workflows/ci.yml`分为Linux／Windows Python3.12核心组、Linux Chromium浏览器组和后置默认容器冒烟组；Actions固定核验SHA、最小contents读取权限，checkout不持久化凭据。CI安装已批准的Pillow12.3.0与Playwright1.64.1测试依赖，不新增应用默认运行时依赖。
- `python scripts/ci_verify.py --scope core|browser|full --output <全新JSON路径>`是统一入口。先启用独立临时数据库／离线Mock、去掉API密钥，再导入测试；强制C8真实子进程恢复组。按测试方法的显式Playwright工具依赖分组，输出分组排除清单；core排除项必须在browser组执行，不能把core单组说成全量。
- 所选集合为空、任何测试失败／跳过、ResourceWarning、源码或日常数据库变动均不算通过；记录源文件SHA、Python／平台、所选及发现数量。本机运行只能证明本机成绩；YAML静态检查、固定Actions SHA或Docker本地测试均不等于GitHub托管执行。
- 工作流在线安装测试工具并构建默认stdlib容器，基础镜像tag的在线重建不承诺逐字节复现；离线发行则使用本地已缓存镜像、固定wheel校验值和模型manifest，按实际image ID及SHA验收。本地模型／OCR的完整验证以离线包验收为准，不能用默认轻量CI代替。

#### **9.12.3 离线运行与付费云样本**

- 离线包沿用`offline_release.py build <全新目录> --local-rag --local-reranker --local-ocr`与`offline_qa.py`，不pull、不联网安装。包含Linux/amd64应用／沙箱镜像、模型／依赖及SHA256清单；按START_HERE导入并启动，目标电脑须已有兼容Docker Linux引擎。Windows原生免Docker、ARM／GPU及物理第二台电脑不在本轮已验范围。
- 源码ZIP与离线8文件包分别迁移核验；普通源码仓库不存images.tar或密钥／运行卷。GitHub普通Git不接收大于100MiB文件，[官方大文件说明](https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github)；二进制离线包适合批准后作为Release资产，单资产须小于2GiB，[官方Release说明](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases)。本批不上传。
- 【事实】已有SiliconFlow／Qwen/Qwen3.5-35B-A3B真实付费链路共6次串行调用，全部HTTP200并接受为余额查询；含1次临时Mock银行主链、5次固定合成问法。总1721tokens，P50约1640ms、P95约2285ms；主链审计有效、余额未改。最多512输出tokens／次，不自动重试，不外发实际账户、音频、知识资料或历史。
- 六次调用只证明有限功能／延迟样本，不等于完整云端并发／稳定性压测；未核实实际账单。后续云端容量需单独约定并发、调用上限和费用。本批回归／镜像验收不再调用付费API。
- P10已有冻结本地压测保留为P10版本成绩；本批不改业务模块与性能阈值，不将其改标为P11重新压测。P08人工双端、RAG真人语料、独立验证器、生产认证／持牌银行／外部IM及物理第二台电脑继续保留原未验收状态。

#### **9.12.4 本批最终记录**

- 当前公开证据：`docs/assets/P11发行验收.json`；完整验收和逐文件改动检查：`data/verification/p11-20261030/report.json`；全量及分组原始日志留在同一验收目录。
- 源码包／离线包的外部SHA清单置于`.qa-tools/offline-releases/p11-20261030-handoff-r2/`，避免将包自身SHA写入包内文件造成循环。最终数量和镜像ID以公开证据及包manifest为准，草测快照不替代最终封版。

| P11本轮最终检查 | 结果与边界 |
| --- | --- |
| 全量／核心／浏览器 | Windows全量774项；最终Linux镜像核心761项；独立Windows浏览器分组13项，均0跳过／ResourceWarning |
| CI最小依赖 | Linux Python3.12的标准库＋Pillow可见环境，核心761项通过；GitHub托管及其Linux浏览器仍未运行 |
| 离线迁移 | 标准包53项、密码＋TOTP配置32项、Fineract配置25项通过；均使用P11同版应用镜像 |
| 最终镜像 | `sha256:42f2234dcd997bdea0faba10d72eae9d2d79969a1a6d74c3161f61e535fb25b2`；267项源码／模型／依赖资产 |
| 真实付费API | 6次HTTP200，1721tokens；主链审计有效且余额未改，不是完整云端压测 |
| 远端与真人边界 | URL已提供；未配置远端／commit／push，托管CI未运行；物理第二台及既有人工作业仍待验 |

- 默认CI容器已从源码包构建并禁网通过渠道业务冒烟。首次命令因只读根目录无法写QA报告失败；工作流增加`/app/data`临时内存挂载后通过，未修改业务代码。最终交接以`handoff-r2`为准，首版及日志保留。


### **9.13 2026-10-31最终修订与成员交接入口**

- 当前基线仍是工作区及确定性SOURCE_MANIFEST，不是旧HEAD。RAG段落修订新增5项测试，受控DAG真实模型补验新增1项；最终完整回归与三配置镜像状态以docs/assets/最终交付验收.json为准，不覆盖P11历史774/761。
- RAG同120题现为关键词113、混合116、独立重排120；原问题/标签和阈值不变、无新增退步，真人PENDING。旧资料在eval/knowledge_p03_v2.json可重现117/120；默认eval/rag_eval_v3.json绑定新资料SHA。
- 模型DAG仅公开目录，2次989tokens，第一次非法安全拒绝、第二次原图通过并经新卡确认在临时Mock恰扣100分。review及原权限链未放宽；不是任意跨业务、生产并行或真人点击验收。
- 成员仍沿第1节各自feature分支、仅改本人域与测试、提PR、不自行合并公共平台。没有成员PR交付证据，不将现有文件当成员已完成。主Agent、监督Agent和支付域仍由负责人维护，DomainContext/Finding/Proposal ABI未变。
- 仓库地址已给定；本机2026-10-31只读ls-remote返回Repository not found，可能涉及访问认证或地址/可见性，尚不能判定。未commit/push、远端CI及邀请没有成功证据，CODEOWNERS只是路径归属，不代表技术分支保护已生效。
- 接收方运行有两种文件：源码ZIP供开发；包含模型/依赖/镜像的独立离线包供已有Docker Linux引擎的电脑运行。当前目标及待办见docs/最终推进清单.md，不能只git clone就声称获得全部离线AI环境。

# T12 Supervisor 路由、来源比对与冲突合并

## 本批目标与层级

落实内部多领域架构（L3）的主 Agent/Supervisor，继续复用 T10 契约、T11 注册与快照、现有 Planner/PolicyEngine/确认/MFA/Tool Gateway。多领域是本项目自定目标；官方安全、审计与 DAG 要求继续由原链负责。本批未增加模型服务、第三方依赖或真实银行访问。

## 实际能力与接入

实现位于 `app/supervisor.py`，`AgentRuntime` 持有 Supervisor：

| 场景 | 路由与来源 | 对原流程的作用 |
|---|---|---|
| 转账规划 | payment：已有支付预检；antifraud：已有策略风险特征；finance_health：同一预检的预算投影 | 核对三个域的事实、建议与原计划模板；没有新增账户查询 |
| 家庭代办转账 | 上述三域 + family：匹配 guardian/subject 的既有授权记录 | 增加身份与授权有效性事实核对，不替代权限矩阵或本人认证 |
| 原工具结果 | 财务健康/订阅/家庭/卡片/反诈按已执行工具所属域核对 | 后置来源核对；不提前读取订阅、不跳过卡片强认证、不重做工具 |
| 未知意图或没有 reviewed route | 不创建通用自由任务 | 继续原澄清路径，不由模型指定模块/工具 |

`route()` 提供固定路由；`coordinate()` 对转账规划进行多域收集；`observe()` 核对一个既有证据节点。当前按确定顺序调用本地只读 Agent，没有跨进程消息系统、任意域路由或并发调度承诺。

`domain_snapshots.budget_snapshot()` 投影本次支付预检中已经读取的预算证据，不另查账本、不复制预算计算公式，也不在黄色订阅确认前增加订阅查询。预算快照仍标注仅覆盖本次预检，不能当作完整家庭资产。

## 来源比对的含义

1. 严格匹配需要的域集合；缺域、多域、快照类型或 Context 摘要不符则拒绝。
2. payment 根据本次 PreflightReport 的检查项重建预期事实；其他域与平台提供的不可变 DomainSnapshot 逐项比对 facts/evidence，并核对已知来源集合。改数字、换来源字符串、转发别的域事实均拒绝。
3. 信封必须与同一 trace/session/context 摘要一致，sender 必须是该域，recipient 必须是 supervisor。
4. 旧全局账本/订阅/家庭成员/卡片来源只接受实际 demo-user 主体，守护授权参与者须与 Context 相符。
5. Proposal 再校验结构、域工具与参数白名单；金额/收款人/订阅 ID/卡 ID 只能等于当前绑定实体。订阅候选 ID 和标签必须来自本次快照；追问字段不能换成 OTP/token。未审查的选项参数、工具、重试或认证标签会被拒绝。

这里的“验证”是与**本次可信平台快照**比对，不是银行数字签名、远程证明或对不可信 Python 插件的隔离。快照必须由平台在原身份与权限链内构造，不能把 HTTP/LLM 字典包装成 ToolResult/快照当作真凭据；引用字符串本身不证明事实真实。Agent 自由文本的建议和风险解释不决定权限，也不作为本批用户回执的数值事实源。

## 合并与未知状态

Supervisor 为金额、可用资金、预算缺口、转账后可支配额及同一卡状态保留来源化的 claims。同一字段不一致时，保留冲突值及各来源，不采用最后值覆盖或多数投票。未知值不补零；风险评分不在 Supervisor 重算，也不映射为权限颜色。

| 状态 | 含义与办理行为 |
|---|---|
| ready | 本次事实与原转账模板可核对；不是执行授权，仍走确认/MFA |
| needs_review | 来源失败、冲突、未知必要资金事实或无效授权记录；本次转账不进入 pending，提供重新核对/人工选项 |
| needs_user_choice | 预检已确认预算缺口；沿用原调整金额/调整预算/延后/转人工四项选择，不借款、不静默降额 |
| needs_clarification | 收款资料未核验；沿用原补齐账户与新计划确认路径 |

实际权限裁决仍由 PolicyEngine 负责，Supervisor 不新增授权。即使 Proposal 缺少 strong_auth 提示，原红色策略仍要求本人验证。上述状态是业务资料核对结果，不得转换为 allow/authorized。

## 受控计划接入与恢复

当前主动计划核对仅覆盖原转账模板：resolve_beneficiary → check_transfer_constraints → create_transfer。`review_plan()` 确认 Context、原实体和这三步一致；Agent 建议不能增加工具、修改收款人或金额，也不直接传给执行器。原 Planner 仍生成正式 AgentPlan，原 token、摘要绑定、一次性确认、MFA、工具前重校验和幂等保持有效。

`AgentPlan.coordination` 保存精简核对记录（status/domains/conflict_keys/reviewed_tools/静态说明），随原 checkpoint 持久化；不保存整段对话或原始跨域事实副本。该记录仅作解释，重启不自动执行，也不能替代新鲜权限、预算和凭证核验；确认时继续读取最新预算并检查一次性凭证。API 请求体不能用 coordination 自报核对通过。

新增审计：supervisor.reviewed（资料核对）、supervisor.plan.checked（原转账模板核对）、supervisor.rejected（构造/来源/计划失败）、supervisor.evidence.verified（既有证据节点核对）。同时保留原 domain.* 记录，mode=ADVISORY_ONLY 表示建议未直接执行。

转账规划中 Agent/来源/计划检查异常会阻止本次入队，不泄露异常详情。既有银行工具已经返回后的分析失败仍沿用 T11：记录 unavailable，不重查、不重提、不伪造另一个银行结果。审计存储故障继续走原恢复/失败关闭机制。

## 验收与边界

新增 `tests/test_supervisor.py` 和 `tests/test_supervisor_integration.py`：路由、三域/家庭四域、来源与信封绑定、伪造事实/候选拒绝、冲突保留、未知/预算状态、工具/参数越界、原模板核对、失败不重试、阻止 pending、原红色验证、确认前预算变更、重启元数据与注入先拦截。

```powershell
$env:PYTHONPATH = 'app;tests'
python -m unittest test_supervisor test_supervisor_integration -v
python scripts/browser_qa.py --full
```

实测数字以本轮记录为准，旧文档历史数字不是验收依据。T10/T11 文档描述的是当时阶段，本说明与最新版成员手册描述当前能力。

未实现自由生成任意跨域可执行 DAG、通用多租户数据源、真实银行签名来源、生产 IAM 或分布式协作。既有 DAG、中断/回退与本地人工接管仍复用原模块；needs_review 的人工选项不表示已接通真人或工单已领取。Docker 镜像/离线包需后续重建验收；RAG 继续暂缓。

## 本轮实测（2026-10-22）

- 新增 Supervisor 11 项、主链 7 项测试；`data/verification/t03-20261022-134829-230854/report.json` 完整回归 533 项通过、0 跳过、0 ResourceWarning，正常数据库指纹不变。
- `data/verification/t12-http-20261022-135227-083072/report.json`：21 项真实环回 HTTP 检查通过，覆盖三域核对、原确认/重放拒绝、跨身份拒绝、冲突禁止入队且原 token 不能执行、篡改收款人建议拒绝、红色 OTP 与再次确认、订阅无提前查询、预算变化重新阻断、注入先拦截；全部 trace 审计有效且非空。
- 家庭四域路由/失效授权检查、来源/信封伪造、未知事实和模板越界在定向测试中验收；未将这些说成真实银行场景已联调。所有写操作仅发生在独立测试 Mock 数据库；本批未重建 Docker 应用镜像。

# Agent 5 订阅治理交接包

成员：李苏润（二狗）；domain：`subscription`。发布基线 `0c6608f`，领域分支 `feature/lisurun-subscription`，领域提交 `7ea8591`；本机集成分支 `feature/lisurun-agent56-live`。本次主链修复前基线 `3be21a0`；交付源码以[联合验收记录](Agent56验收记录.json)的文件摘要及包含本包的提交为准。

## 可用于 PR 的变更说明

订阅取消原先可能把询问当成办理请求；目标缺失时还会提前读取黄色查询数据，并移除候选。本版将目标缺失处理为“确认查询→选择项目→重新确认取消”，不在查询确认前调用订阅工具，也不生成取消步骤。询问、否定和单纯提及取消不会生成写计划；明确申请仍保留黄色确认。候选显示商户和每期金额，显示信息相同时停止提供取消按钮，避免选错订阅。

领域模块区分历史交易、未来扣费授权、服务权益和退款；未知不解释成零或成功，选中目标的解释不受其他订阅污染。不可取消、未知扣费、重复ID及领域核验失败均不会产生有效取消写建议；执行适配器再次检查当前办理条件。

## 文件与集成边界

- 领域交付：`app/agents/subscription_agent.py`、`tests/test_subscription_agent.py`、`docs/Agent5订阅治理设计与交付.md`。
- 已集成的公共支撑：`app/domain_snapshots.py`、`app/supervisor.py`、`app/banking_agent.py`、`app/static/app.js`；以 Git 差异区分平台基线与成员增量，不将这些公共文件重复作为两个域各自的完整替换件。
- 本次修复：`app/banking_agent.py`、`tests/test_domain_live.py`、`tests/test_d1_security.py`；公共改动在集成分支只合入一次。
- 无新增运行依赖、外部模型调用或自主执行权。PolicyEngine负责分级，主链负责确认、幂等、审计及恢复。

## 输入输出及验证

[交接示例](Agent56交接示例.json)包含可携带的Context、可信快照、Finding、Proposal及Supervisor结果。`subscription_ready`为正常建议；`subscription_choose_target`为缺目标候选；`subscription_unavailable`和`subscription_failed_read`为阻断情形。全部数据为虚构示例，不含认证凭据。

执行完整验收时设置项目已安装的 `C7_NODE_BINARY`、`C7_PLAYWRIGHT_MODULE`，运行 `python scripts/ci_verify.py --scope full --output <新的报告路径>`；不能把浏览器用例跳过视为完整通过。最新实际命令、计数、环境和源码摘要见联合验收记录。该记录证明本机自动测试，不代表远端CI或真实银行验收。

负责人可按以下流程手工核验：输入“取消订阅”，确认前无订阅结果；确认后出现真实候选；选择项目后生成新确认计划；取消后区分扣费结果、记录中的服务日期及退款未知。再输入“查询视频会员是否已经取消”“我不是要取消视频会员”，确认不会生成取消步骤。

## 状态与待办

领域实现和本机集成已完成；本次验收状态以联合记录为准。两域保留独立提交包，团队PR、评审及合入目标分支尚未获得验收记录。负责人确认实际目标分支后再提交，不能假定远端存在 `develop`。按 `AGENTS.md`，本次仅本地提交，不自动推送或合并。

真实账单／代扣／服务商接口、真实多客户数据源及目标用户理解测试仍待接入或人工验收。续费提醒、年度节省和卡片关联不是本包已交付能力；扩展前须由平台提供可信周期、下一扣费时间、关联和提醒授权。不能把预计节省或待处理退款计入当前可用资金。

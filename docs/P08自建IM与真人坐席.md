# **P08 自建 IM 与真人坐席接入**

## **1. 目标与边界**

### **1.1 本批实现范围**

- 【事实】在原有一键求助、HandoffQueue 与审计链上增量接入本机用户／坐席文字通信。用户仍从原首页发起求助；坐席使用 `/support/operator` 领取原工单后交流。实现见 `app/live_support.py`、`app/static/app.js`、`app/static/operator.html`，HTTP 路由见 `app/banking_agent.py`。
- 需求分层：这是 L3 的 P08 自建接入方案，用于演示 L1 人工接管路径；不将商业 IM、持牌银行客服或生产身份认证视为已经验收。结单、消息送达、对方页面显示均不执行银行业务，不改变未知交易的事实。
- 【事实】成员 Agent ABI `app/domain_agent_contract.py` 未改动。权限判定、黄色确认、强认证、交易审计与原请求查询仍走既有主链。

### **1.2 明确未包含的能力**

- 未接入微信／企业微信等第三方 IM、银行官方坐席、生产 SSO／MFA 或供应方 SLA。当前令牌为本机演示凭据；不得暴露公网或当作生产认证。
- 对方页面显示回执仅证明页面执行显示确认，不证明真人阅读、理解或提供银行授权。
- 没有消息附件、文件外发、模型自动回复、自动冲正、客服远程办理转账；正文敏感模式检查也不是完备 DLP。

## **2. 接口与状态**

### **2.1 最小接口契约**

| 方法与路径 | 输入／用途 | 权限与限制 |
| --- | --- | --- |
| GET `/api/v1/support/capabilities` | 查询 self-hosted 能力与人工验收开关 | 受信会话；普通服务不开放验收结束接口 |
| POST `/api/v1/customer-service` | `request_id`；复用原工单入口 | 本人／设备绑定，同一请求重放不新建工单 |
| POST `/api/v1/handoff/inbox`、`claim`、`resolve` | 原工单队列、领取、固定结论 | 原管理员会话；领取后绑定具体坐席与设备 |
| POST `/api/v1/support/send` | `case_id,message_id,text` | 本人或已领取坐席；同编号同正文重放复用原消息，异正文拒绝 |
| POST `/api/v1/support/poll` | `case_id,after_seq` | 每页最多50条；返回 `next_cursor,more` |
| POST `/api/v1/support/ack` | `case_id,upto_seq` | 只允许确认该身份已拉取范围内的消息 |

- 身份来自原 HTTP 会话；客户端不能通过正文伪造角色、坐席或工单所有者。未认证／越权为403，参数或敏感正文为400，不确定存储结果为503。
- 状态沿用 `LOCAL_QUEUED → LOCAL_CLAIMED → LOCAL_CLOSED`。排队时用户可留言；未领取坐席不能读对话；结单后只读历史，不再发送新消息。
- `delivery=SERVER_STORED` 表示本机服务器已持久化；`peer_displayed` 表示对方页面确认显示。`operator_page_online` 仅为15秒内轮询心跳。

### **2.2 存储、重放与审计**

- 原 SQLite 中新增 `support_messages`、`support_peers`，消息正文仅存本机业务库；审计记录工单、主体、消息编号、正文摘要与长度，不写原文。工单 trace 贯通收发与显示确认。
- 正文最多2000字符／8000 UTF-8字节；每工单最多1000条、全库最多10000条。拒绝控制字符和常见密码、验证码、长账号、API key 模式。
- 消息和审计在同一事务提交；审计失败会回滚消息／显示确认。同工单、角色、主体、设备和消息编号形成持久去重键，重启后仍有效。
- 页面只以 `textContent` 显示正文；坐席令牌仅在页面内存使用。sessionStorage 只保留待确认消息编号及正文摘要，不保留令牌／正文。关闭页面后应妥善保护本机数据库和 Docker 数据卷；当前没有生产级保留期清理、加密密钥托管或完备外发 DLP。

## **3. 运行与验收**

### **3.1 开发与人工验收入口**

- 原默认离线演示继续使用 Mock；生产模式不会因此自动接通银行。Fineract 可选配置见 `docs/P07银行与卡管服务替换.md`。
- 安装兼容 Python 的开发机可运行 `python scripts/support_acceptance.py --help` 查看工具参数；使用 `--output data/verification/p08-20261024/human-acceptance.json` 启动独立临时数据库与两端页面，按生成的 `人工验收操作.md` 操作。本机 `operator-login.txt` 是短期坐席凭据，不是 LLM 密钥，不得提交 Git、发送聊天或随包发布。
- 工具只在本机回环地址提供人工验收接口。人工核对固定短句、双向显示后勾选保存；报告仅保存摘要与元数据，不保存正文或令牌。一次真人可扮演两角色，不冒称银行真实客服。
- 【事实】2026-10-24 用户选择暂不操作；当前真人状态为 `PENDING_HUMAN_USER_DEFERRED`，临时服务已停止。自动浏览器通过不能将此项改为通过。

### **3.2 自动测试覆盖**

- `tests/test_live_support.py`：所有者／设备／坐席绑定、未领取与未认证拒绝、字段伪造、并发去重及重启、错误游标与越界 ack、分页、心跳、结单只读、容量与敏感值、审计故障回滚、注入文字不调用 Agent、不改变银行余额；另有两个独立 Chromium 上下文的真实 HTTP 收发、页面刷新、XSS 字面显示和390px布局。
- 专项实测11项通过，0跳过／连接警告，记录 `data/verification/p08-20261024/support-tests.json`。全量、镜像、迁移与开源核心组合实测以同目录 `report.json` 为最终结果；不能用本段历史测试数字代替后续版本验收。

### **3.3 离线交付与复验**

- 标准包 `.qa-tools/offline-releases/p08-20261024-final-r2/`：原默认 Mock，包含本地 RAG、独立重排及 OCR 模型、依赖和应用镜像，运行见 `START_HERE.md`。无需联网拉取依赖，前提是目标电脑已安装兼容 Docker Linux 引擎／Compose。
- 可选核心包 `.qa-tools/offline-releases/fineract-p08-20261024-final-r2/`：和标准包一起复制、校验并导入；独立 PostgreSQL、Fineract、种子初始化与应用均在容器内运行，不需要宿主机 Python。默认发布本机8091；令牌只在首次启动后的本机数据卷生成，取用方法见该包说明。
- 镜像包含代码／模型，不包含源电脑运行数据库、私有 API 密钥或本次人工验收令牌。可选包的可丢弃本机初始化口令不是银行证书；校验和只检出损坏，不等于发布者签名。
- 迁移验证只代表当前电脑的隔离网络和新目录演练；物理第二台电脑、真人双端、第三方 IM 与生产认证保持待验／待接入，不合并计为自动通过。

### **3.4 本批最终验收快照**

| 检查 | 实测结果 | 记录 |
| --- | --- | --- |
| P08专项 | 11项通过，0跳过／ResourceWarning | `support-tests.json` |
| 全量回归 | 730项通过，0跳过／ResourceWarning | `full-suite.json` |
| 最终镜像禁网只读 | 65项通过，0跳过／ResourceWarning | `docker-core.json` |
| 标准包迁移 | 53项检查通过；258项资产匹配 | `data/verification/t14-20261030-163432-171918/report.json` |
| 可选核心新卷／重启 | 25项检查通过，100分虚构转账及原生撤销双边恢复 | `core-profile.json` |
| 真人双端 | 用户暂缓，保留待验；临时服务已关闭 | `human-acceptance.json` |

- 短记录名位于 `data/verification/p08-20261024/`；当前联合报告 `report.json`。本批最终镜像 `sha256:c1da2bd558bc92febacc5ab089205e7c7a8bf1163e7387593b24303b5f90d5b8`。无r2后缀的P08包为提示修复前快照，不代表最终版本。
- 原日常数据库、Git HEAD、成员ABI及五个删除文件保持原状；未commit／push。以上为本机自动验收，真实银行、真人双端和物理异机不计通过。

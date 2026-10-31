# API 说明

基础地址：`http://127.0.0.1:8090`

## 本地演示认证与边界（2026-10-22 校准）

主要业务接口要求 `Authorization: Bearer demo-user-session`，这是源码内固定的本地演示身份，不是生产登录。服务端据会话绑定 user/device/role；请求体中的身份若与会话不一致会被拒绝，不能通过传 role 提权。家庭会话和本地管理员使用各自服务端配置的身份。依据：`app/family_access.py:16/58`、`app/banking_agent.py:3848/4011`。

下列示例省略 HTTP 头，调用时仍须携带上述认证。健康/能力接口公开；旧 `/api/v1/notifications*` 仅按 user_id 过滤本地模拟通知，该标识不是认证，不能据此宣称所有端点均已具备生产租户隔离。

## 响应视图原则

接口为前端同时返回机器视图和用户视图：

- `plan`：结构化内部计划，供运行时、审计和测试使用；普通用户界面不渲染；
- `plan_view`：自然语言计划卡片，展示风险、对象、金额、步骤和确认提示；
- `result`：结构化工具执行结果，供审计与调试；
- `result_view`：由工具事实生成的自然语言结果，普通用户界面展示。

## `GET /api/health`

返回服务状态与熔断状态。

## `GET /api/state`

要求可信演示会话。demo-user 返回演示账户、交易、订阅、理财、卡片；其他已支持身份返回对应账户与交易视图。当前仍是本地 Mock，不代表真实银行或完整多租户业务隔离。

## `POST /api/chat`

请求：

```json
{"message":"给妈妈转 200 元，备注午餐"}
```

黄色请求示例返回（节选，标识均为占位值，不可直接执行）：

```json
{
  "ok": true,
  "trace_id": "trace_xxx",
  "message": "已生成黄色 · 需要确认操作计划，请确认后继续。",
  "plan": {"plan_id": "plan_xxx", "risk_level": "yellow"},
  "plan_view": {
    "title": "本次操作计划",
    "summary": "向妈妈转账200 元",
    "risk_label": "黄色 · 需要确认",
    "action_token": "服务端本次生成的opaque token",
    "details": ["收款人：妈妈", "金额：200 元"]
  }
}
```

绿色查询会额外返回 `result` 和 `result_view`。工具失败时返回 `result.status=FAILED` 和失败的 `result_view`，不得使用“已成功”话术。

## `POST /api/confirm`

请求：

```json
{"plan_id":"plan_xxx","action_token":"从 plan_view.action_token 原样取得"}
```

黄色计划经确认和平台复核后执行；红色计划首次确认返回 `needs_auth=true` 与 `auth_challenge.credential_id`。确认 token 绑定原计划，默认有效期 600 秒；过期、改参、跨身份/设备或已消费的确认会拒绝。以返回的 ok/error_code 和结果状态判断，不只看 HTTP 200。

## `POST /api/verify`

请求：

```json
{"plan_id":"plan_xxx","action_token":"原确认token","credential_id":"auth_challenge中取得","code":"123456"}
```

这是固定演示验证码，没有真实短信、人脸或 U-Key。认证凭证默认有效期 300 秒，绑定计划摘要、身份与设备。验证通过后，仍须携带原 plan_id、action_token 和返回的 credential_id 再次调用 `/api/confirm`，不会因验证成功直接扣款。生产替换尚未完成。依据：`config/policy.json:47/48`、`app/banking_agent.py:3011/3023/3140`。

## `POST /api/cancel`

请求同样需要 plan_id 与 action_token。取消尚未执行的计划后，原确认不能再作为授权凭证使用；不代表撤销银行已受理交易。

## `GET /api/audit`

要求演示会话，按现有身份可见范围返回审计事件；`/api/v1/audit/events`、`verify` 另按 trace_id 做归属校验。当前为 SQLite 哈希链，生产 WORM 存储未实现。

## 已接入的扩展入口

| 入口 | 当前行为与依据 |
|---|---|
| `GET /api/gateway/capabilities` | 渠道能力声明；Web 和本地终端演示，不代表真实 IM 上线；见 `docs/T08本地第二渠道.md`，运行入口为 `scripts/channel_demo.py` |
| `GET /api/readiness` | 生产准备度仍为 NOT_READY_FOR_PRODUCTION；配置健康不等于生产验收 |
| `POST /api/v1/customer-service` | 演示会话 + request_id，登记本地工单，不能声称接通真人 |
| `POST /api/v1/handoff/status` | case_id；仅本人同一设备查询 |
| `POST /api/v1/handoff/inbox/claim/resolve` | 分别为三个端点；本地管理会话才能领取/结单，普通用户不能提权。inbox 使用 offset；claim 使用 case_id；resolve 使用 case_id + resolution |
| `/api/dag/*` | 状态与节点边界控制；接口/回退限制见 `app/banking_agent.py:3431`、`app/task_dag.py` 与 `tests/test_dag_integration.py` |

接管结论仅 GUIDANCE_PROVIDED、REFERRED_OFFICIAL、NEEDS_RECONCILIATION；未知交易只允许 NEEDS_RECONCILIATION。LOCAL_CLOSED 只是本地登记结束，不代表交易成功、退款或真实客服处理完成。管理令牌由操作者在服务启动前通过 BANK_AGENT_DEMO_ADMIN_TOKEN 配置，无随包默认管理员密码。完整操作见 `docs/T09本地人工接管.md`、`scripts/handoff_demo.py`。

T12 的 plan.coordination 仅保存固定 Supervisor 协作元数据，不是执行授权，不替代 PolicyEngine、原确认、MFA 或 ToolRegistry。代码：`app/supervisor.py:196/233`；测试：`tests/test_supervisor_integration.py`。当前源码基线见 `docs/需求追踪矩阵.md`。

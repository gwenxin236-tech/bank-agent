# 渠道无关 Agent 网关设计

## 一 设计结论

项目只维护一套 Agent 核心。Web、手机 App、企业微信、飞书、钉钉、Telegram、Slack 等都只是入口，通过 Channel Adapter 转成统一消息，再交给 Agent Gateway。

```text
Web / App / IM / Voice
          ↓
   Channel Adapter
          ↓
     UnifiedMessage
          ↓
     Agent Gateway
          ↓
 Agent Runtime + PolicyEngine
          ↓
 Tool Gateway / 银行核心
```

当前版本提供 Web 和独立本地终端适配器（`local_demo`）两个演示入口，不连接任何第三方平台；App/外部 IM 仍只有契约和安全边界设计。终端入口、权限与去重验收见 [T08 本地第二渠道](T08本地第二渠道.md)。本地回执不代表真实 IM 发送成功，真实平台接入要求仍保留。

## 二 统一消息契约

代码位置：`app/channel_contracts.py`。

```python
UnifiedMessage(
    channel="web",
    user_id="内部用户标识",
    conversation_id="渠道会话标识",
    message_type="text",
    text="给妈妈转 200 元",
    attachments=[],
    metadata={},
)
```

字段说明：

- `channel`：入口类型，不作为业务权限依据；
- `user_id`：必须映射到已经认证的内部用户，不能直接信任 IM 昵称；
- `conversation_id`：用于同一会话的上下文关联；
- `message_type`：当前先处理文字，图片、文件和语音入口预留；
- `text`：经过长度限制的用户文本；
- `attachments`：只能引用经过病毒扫描和权限检查的对象；
- `metadata`：平台事件 ID、时间戳、签名校验结果等，不得把平台原始字段直接当成银行事实。

## 三 渠道适配器职责

适配器只做四件事：

1. 验证平台回调签名、时间戳和 nonce；
2. 把平台事件转换为 `UnifiedMessage`；
3. 将 Agent 的自然语言回复和确认卡片转换回平台格式；
4. 处理平台特有的重试、消息去重和发送失败。

适配器不应该：

- 自己判断转账风险；
- 自己决定是否需要短信或刷脸；
- 直接调用银行核心；
- 把模型输出直接发送为成功结果；
- 在不同渠道复制一套业务规则。

## 四 会话和异步任务

生产实现建议：

- `channel + platform_user_id + conversation_id` 映射到内部 `session_id`；
- Redis 保存短期会话状态和幂等键；
- PostgreSQL 保存用户授权、任务状态和业务关联；
- 需要较长时间的任务进入队列；
- Webhook 先在平台要求的时间内返回“已收到，正在处理”；
- 任务完成后由适配器主动发回；
- 跨渠道查看同一任务时，以内部 `task_id` 和授权上下文为准。

当前 `InMemorySessionStore` 只用于演示，不能作为生产会话存储。

## 五 App 接入路线

### 路线 A 自研 App

使用 Flutter、React Native 或原生技术实现登录、聊天、确认页、推送和设备能力。App 只调用 Agent Gateway 的 REST、SSE 或 WebSocket 接口，业务规则仍在后端。

### 路线 B 云 IM SDK

App 使用成熟 IM SDK 处理消息、离线、推送和多端同步，服务端把机器人账号接入 Agent Gateway。需要单独核验 SDK 的数据区域、日志留存、密钥托管和合规边界。

## 六 IM 接入路线

第一阶段可以选择飞书、企业微信或钉钉中的一个做查询型 MVP。高风险操作不建议在普通群聊里完成，应通过一次性安全链接跳转到受控确认页，并绑定登录用户、设备和有效期。

平台接入前必须具备：

- 公网 HTTPS 和可信域名；
- 回调签名验证；
- 时间戳和 nonce 反重放；
- 消息事件去重；
- 用户身份映射；
- 限流和黑名单；
- 高风险操作回 App 或安全 H5；
- 平台故障时的人工兜底。

## 七 已落地的内部接口

- `POST /api/chat`：统一 Web 消息入口，支持 `channel`、`user_id`、`conversation_id`；
- `GET /api/gateway/capabilities`：查看已连接和计划渠道；
- `GET /api/readiness`：查看生产阻断项；
- `verify_hmac_signature`：未来 Webhook 使用的 HMAC 校验函数；
- HTTP 安全响应头和演示级限流。

## 八 上线前必须替换的演示实现

- `MockBankCore` → 银行核心 API Gateway；
- `InMemorySessionStore` → Redis/PostgreSQL；
- 演示验证码 → 短信、FIDO2、活体或 U-Key；
- SQLite 哈希链 → 追加写入/WORM 审计与 SIEM；
- 进程内限流 → WAF/API Gateway/分布式限流；
- 单机服务 → 高可用集群、队列、灾备和可观测平台。

# C3 强认证防篡改防重放与 Adapter 接口

## 交付范围

本任务把演示运行时的“通过验证码后允许执行”升级为结构化、一次性、可审计的认证凭证。实现仍是离线 Mock，不连接短信、人脸、FIDO 或银行核心。

### 凭证结构

`AuthCredential` 绑定以下字段：

- `user_id`：服务端可信用户身份（演示默认 `actor_id`）；
- `device_id`：服务端可信设备身份（演示默认 `demo-device`）；
- `task_id`：操作计划 ID；
- `node_id`：需要认证的副作用节点 ID；
- `operation_type`：工具/操作类型，例如 `create_transfer`；
- `amount_cents`：金额，始终为整数分；
- `recipient`：收款人标识；
- `created_at`、`expires_at`：默认有效期 5 分钟；
- `nonce`：随机一次性秘密，审计只保留 `nonce_digest`；
- `used_at`：原子消费时间戳，非空后再次提交返回 `AUTH_REPLAY`。

认证同时保存操作摘要 `operation_digest`。运行时每次 verify/confirm/副作用执行前重新计算绑定内容；金额、收款人、节点、身份、设备或操作变化都会使旧凭证失效，写入 `auth.invalidated`，不得沿用原认证。

## 状态与接口

现有 Runtime 兼容旧 Web 调用：

```text
confirm(plan_id, action_token, credential_id="", device_id="", actor_id="")
verify(plan_id, action_token, code, credential_id="", device_id="", actor_id="")
```

- 第一次确认红色操作返回 `needs_auth=true` 和 `auth_challenge.credential_id`；
- verify 成功后只返回“请再次确认执行”，不会执行副作用；
- 第二次 confirm 原子设置 `used_at`，再进入执行；
- processing/成功/失败等业务结果仍由工具返回，认证不替代账务状态查询；
- 三次错误验证码锁定任务（`AUTH_LOCKED`），并保留既有全局 CircuitBreaker；
- 服务端 API 的 `user_id`、`device_id` 应来自可信网关。演示接口允许 body 传入默认值，仅用于离线验收，不能视为生产身份认证。

## 生产替换点

| 演示实现 | 生产替换 |
| --- | --- |
| `AuthCredentialStore` 内存字典和进程锁 | Redis/PostgreSQL/认证服务中带 TTL、CAS/事务唯一消费的凭证记录，多实例共享 |
| `MockStrongAuthProvider` | 银行认可的短信 OTP、FIDO2/U-Key、生物识别或统一认证中心；assertion 必须验证签名、受众、设备和交易摘要 |
| `device_id` 请求字段 | 登录会话/设备证明、设备风险服务、mTLS 或网关注入的不可伪造设备上下文 |
| 演示验证码 `123456` | 生产 OTP/FIDO 验证服务；不在 Agent 或日志保存验证码 |
| `operation_digest` SHA-256 | 生产统一 canonicalization、密钥服务/HSM 签名与密钥轮换；跨服务传递 trace/task/context |
| 本地审计 SQLite | WORM/append-only 审计、集中 SIEM，保留哈希链、密钥和访问控制 |

生产接入不得把模型输出当作认证结论；模型只能提出操作请求，PolicyEngine、可信身份上下文和认证服务共同决定是否允许副作用。

## 证据

`tests/test_auth_credentials.py` 覆盖字段完整性、5 分钟 TTL、金额/收款人篡改、一次性消费和并发双击、过期、错误三次任务锁定、跨身份/设备/任务拒绝、直接 execute 越权和 Mock Adapter 重放拒绝。

# C1 核心账务一致性与 Adapter 接口

> 版本：C1 · 2026-09-20
> 定位：离线可验证的银行生产替身，不代表已接入真实银行核心。

## 1. 交付边界

本阶段把 `MockBankCore` 从“同步扣款函数”升级为可查询、可恢复、可审计的交易状态机。金额始终使用整数分；转账副作用不自动重试；Agent 的成功/失败话术只使用核心交易查询结果。

| 演示实现 | 生产替换点 |
| --- | --- |
| `app/banking_agent.py::MockBankCore` | 真实核心账务或支付网关实现 `BankCoreAdapter`，提供原子扣账、状态查询、幂等键持久化、冲正和对账接口 |
| SQLite `core_transactions` | 核心交易表/支付网关幂等存储，要求唯一约束、事务日志和灾备 |
| `fault_injection`、`finalize_transaction()` | 仅用于演练；生产使用受控故障注入平台和核心侧异步结果回调/轮询 |
| `AgentRuntime` 最终状态查询 | 生产使用核心查询 API 或消息队列回执，不能用模型推断交易结果 |

## 2. 状态机

```text
created ──> processing ──> succeeded
                   └──────> failed
succeeded ────────────────> reversed
```

- `created`：已生成交易记录和幂等键映射，尚未进入扣账处理。
- `processing`：已受理，结果尚未确定；不会因为超时自动再次扣款。
- `succeeded`：核心确认扣账成功。
- `failed`：核心确认失败；余额不足、收款人不存在、日累计超限等故障均不扣款。
- `reversed`：已成功交易完成冲正，余额补回并保留原交易与冲正流水。

每笔交易持久化 `transaction_id`、`idempotency_key`、金额分、收款人、状态、错误码、扣账标记和 `state_history`。状态迁移由白名单约束，非法迁移会被拒绝。

## 3. 幂等与恢复语义

- `MockBankCore.transfer(..., idempotency_key=...)` 对 `idempotency_key` 建唯一约束。
- 同一键重复调用返回同一 `transaction_id`、同一状态和同一业务结果，并附带 `idempotent_replay=true`；不会创建第二条交易，也不会再次扣款。
- 超时故障只把交易停在 `processing`，不扣款；使用同一幂等键重发只读回原交易。
- `MockBankCore(db_path=同一 SQLite 文件)` 重启时恢复交易表和已生效扣账，`query_transaction()` 仍可查询 `processing`。
- `finalize_transaction(transaction_id, outcome=...)` 是离线演示的核心结果落库入口；生产由真实核心回执替代。

## 4. Agent 事实来源规则

`AgentRuntime.execute()` 在 `create_transfer` 后立即调用只读 `query_transaction()`，并将查询结果作为用户可见事实源：

- `succeeded`：允许生成“已向……转账”的成功文案；交易号、收款人和金额均来自查询结果。
- `failed`：返回失败，不生成成功文案，并保留错误码/错误消息。
- `processing`：外层结果为 `PROCESSING`，用户必须看到：**银行还在确认这笔交易，请不要重复操作**。
- 不会把超时、受理成功或计划步骤完成误报成扣账成功。

## 5. 故障注入

离线演练可通过构造参数或 `inject_fault()` 设置：

```python
core = MockBankCore(db_path=path, fault_injection="timeout")
# 可选：timeout / insufficient_funds / beneficiary_not_found
pending = core.transfer(20_000, "妈妈", "演练", "idem-demo")
status = core.query_transaction(pending.data["transaction_id"])
```

真实核心替换时，不应把故障开关暴露给普通业务调用方；应由隔离的演练环境、审批后的注入控制和可回滚策略提供。

## 6. Adapter 契约

`app/production_contracts.py` 中的 `BankCoreAdapter` 定义最小生产替换边界：

```python
class BankCoreAdapter(Protocol):
    def get_balance(self, ctx: AdapterContext) -> AdapterResult: ...
    def create_transfer(self, ctx, amount_cents, beneficiary_id, note) -> AdapterResult: ...
    def query_transaction(self, ctx, transaction_id) -> AdapterResult: ...
    def reverse_transfer(self, ctx, transaction_id, reason) -> AdapterResult: ...
```

`AdapterContext.idempotency_key` 由上游业务请求生成并贯穿调用链。演示服务还提供只读接口 `GET /api/v1/transactions/status?transaction_id=...`；它只调用 `query_transaction()`，不会触发扣款或重试。

真实实现必须：

1. 在核心侧做唯一幂等约束，而非只依赖 Agent 内存；
2. 返回规范化状态 `created / processing / succeeded / failed / reversed`；
3. 将 `amount_cents` 作为整数分传递，禁止浮点金额；
4. 查询接口只读，不触发重试或扣账；
5. 冲正接口具备自身幂等键和可对账回执；
6. 返回核心交易号、状态、错误码和可审计请求号。

`MockBankCoreAdapter` 仅用于契约测试，采用内存状态；它不代表真实银行联调、支付清算或监管资质。

## 7. 证据

验收用例位于 `tests/test_core_transaction_state.py`：

- 状态机完整迁移与最终查询；
- 同一幂等键在 `processing` 时重发不重复扣款；
- 余额不足、收款人不存在故障分支；
- 重启后恢复 `processing`，最终成功只扣一次；
- Agent 在 `processing` 时不得宣称成功；
- 审计 `tool.result` 的交易状态与核心查询状态一致。

适配器契约用例位于 `tests/test_production_contracts.py`，覆盖幂等、最终状态查询和冲正。

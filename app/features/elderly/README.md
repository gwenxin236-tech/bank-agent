# 长辈体验域协作说明

## 所有权与入口

本成员独占 `app/features/elderly/` 与 `tests/test_elderly.py`。当前可运行示例是 `tools.py` 的 `PlainLanguageTool`，工具名 `elderly_plain_language`：将两条预设术语转为白话；未收录的术语只提示向官方客服核实，不回显任意输入。

在本目录增加工具类，继承 `from tool_contract import BaseTool`，声明 `name`（必须以 `elderly_` 开头）、`authz_required`、`title`、`parameters`，实现 `execute(ctx, **params)`。在本域 `__init__.py` 的 `register(registry)` 中通过 `registry.register(工具实例)` 注册。域内可增加子模块，但工具实现必须来自本域；注册失败不会向中央注册表发布半套工具。

返回契约：`{"ok": True, "data": {"message": "来自工具的说明", "demo_only": True}}`；失败必须同时给出 `ok=False`、`data`、`error_code`、`error_message`。不可伪造顶层工具名或 trace。参数只支持 `text` 和 `cents`，字段必须与声明完全一致；金额字段必须用 `*_cents` 整数分，禁止浮点数、布尔值冒充金额和字符串金额。显示元时才用整数除法格式化。

`ctx` 是服务端创建的不可变上下文，含 trace/task/node/actor/subject/device 与授权结果，不含 bank、数据库、令牌或可变 runtime。权限由 **PolicyEngine** 与服务端确认/认证状态决定；模型或参数中的 `authorized`、身份字段不能授权。

## 配置与本地 Web 演示

只修改 `config/features.json` 的 `elderly` 对象，不修改其他域的 key、不合并为共享数组。字段为 `enabled`、`demo_tool`、`demo_params`；需增加公共字段时先请负责人评审。配置启动时校验，修改后重启生效，关闭域后不导入其注册模块。

1. 在仓库根目录运行 `./run.sh`，进入其输出的本地 Web 地址，使用已有演示会话。
2. 在聊天输入 `功能演示 elderly`；无需网络或 LLM API。
3. 核对计划卡片并点击确认。示例仍走网关→计划→PolicyEngine→确认→已注册工具→审计，不是新的直接执行接口。
4. 回复使用现有通用完成提示；点击页头“服务记录”图标（审计链回放抽屉），查看本次 trace 的 `tool.result` 中 `tool_result.message` 与结构化字段，那里才是实际工具结果。没有为 D4 新增前端页面。

## 测试与验收

在 `tests/test_elderly.py` 添加用例（从根目录运行，必要时设置 `PYTHONPATH=app`）。既测试工具结构化输出，也测试 Agent 主链：确认前不执行，拒绝后余额/状态不变，审计包含正确 trace/工具名，失败不得报成功，金额精确到整数分。不要只断言“没有异常”。

```sh
PYTHONPATH=app python3 -m unittest discover -s tests -p 'test_elderly.py' -v
PYTHONPATH=app python3 -m unittest discover -s tests -p 'test_feature_registry.py' -v
python3 -m unittest discover -s tests -v
```

脚手架已由 `tests/test_feature_registry.py` 覆盖四域注册、隔离、真实 HTTP 调用、权限和失败分支；成员补充自己域的行为测试。隔离测试递归检查域内模块，允许域内相对导入，但禁止越出本域。需要扩大公共契约或导入范围时请负责人评审，不要擅自修改共享测试来绕过边界。

## 绝对不能碰

- 不修改其他成员域、`app/banking_agent.py`、`production_contracts.py`、共享注册器/契约、核心策略、前端及共享测试；有需要提请负责人处理。
- 不导入/获取银行核心、数据库、其他域实现；不根据模型或工具参数决定权限。
- D4 仅开放无副作用工具，声明 `side_effect=True` 会被拒绝。转账/取消订阅/卡片操作须负责人单独接入策略、幂等、确认与恢复；不得自动重试。
- 不引入第三方运行时依赖或外部网络作为演示必要条件，不记录真实个人信息、验证码、API Key。
- 目录与注册器是受信团队代码的协作边界，**不是恶意 Python 插件的安全沙箱**。

## 分支与 PR

遵循《团队分工与GitHub协作规范》：由最新 `develop` 建立 `feature/elderly` 分支，只提交本域与本域测试（配置变动仅本域对象）。先写失败测试，再实现并运行回归；提交采用 Conventional Commits。由成员人工推送到 GitHub、向 `develop` 提 PR，附改动范围、测试证据、Web 演示步骤与生产替换点；负责人 review 后合并，负责人负责整合到 `main`。AI 代理只 commit、不 push。若远程尚无 `develop`，先由负责人建立，不擅自改变团队分支约定。

## 生产替换点

接入经审核的白话文案库与适老无障碍验收；当前示例不代替既有长辈模式、认证或客服流程。 接入前还需生产身份与访问控制、审计与脱敏、持久化/容灾及合规审批；本脚手架不代表银行生产上线验收。

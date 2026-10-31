# E7-B 适老化读屏浏览器级验收

## 1. 验收定位

本页是 N08/T21 的演示级浏览器验收说明，覆盖从联系人消歧、金额输入、计划确认、强认证到最终回执的最后一步。浏览器自动化只用于测试，不是运行时依赖；项目运行仍保持 Python 标准库、离线可演示，`requirements.txt` 不增加 Node 或 Playwright。

通过标准不等同于无障碍合规认证，也不替代真实银行终端、读屏软件和辅助技术的现场评估。

## 2. 可选环境配置

### 推荐：隔离安装并自动配置

在项目根目录执行，首次需要可访问 npm 和 Playwright 官方下载源：

```sh
python scripts/browser_qa.py --install
```

该入口将 Playwright 1.64.1 和 Chromium 安装到 `.qa-tools/`，不修改应用依赖。
此目录已排除出 Git 和 Docker 构建。后续重跑四项浏览器验收无需下载：

```sh
python scripts/browser_qa.py
python scripts/browser_qa.py --full
```

默认运行转账交互、读屏友好流程、长辈模式和语音草稿四项测试；`--full` 同时运行
全量测试与 C8 恢复演练。入口自动设置 Node/Playwright 路径，使用临时数据库，
在 `data/verification/t03-*/report.json` 留存版本、命令、通过/跳过信息及原始输出。
任何测试失败、跳过或原演示数据库发生变化，入口均返回失败；不将跳过记为通过。
每个浏览器用例自行启动临时本地服务，无需提前运行 `run.sh`。

以下手动配置方式用于已有自备工具链的环境。

### macOS / Linux

```sh
# 在项目根目录执行
command -v node
export C7_NODE_BINARY="$(command -v node)"
export C7_PLAYWRIGHT_MODULE="$(node -e "process.stdout.write(require.resolve('playwright'))")"
printf 'node=%s\nplaywright=%s\n' "$C7_NODE_BINARY" "$C7_PLAYWRIGHT_MODULE"
```

`C7_NODE_BINARY` 填 Node 可执行文件的绝对路径，例如 `/opt/homebrew/bin/node`；`C7_PLAYWRIGHT_MODULE` 填 `require.resolve('playwright')` 输出的入口 JavaScript 文件绝对路径，不是目录。

### Windows PowerShell

```powershell
$env:C7_NODE_BINARY = (Get-Command node).Source
$env:C7_PLAYWRIGHT_MODULE = (node -e "process.stdout.write(require.resolve('playwright'))")
"node=$env:C7_NODE_BINARY"
"playwright=$env:C7_PLAYWRIGHT_MODULE"
```

如果 `require.resolve('playwright')` 失败，说明当前 Node 环境没有安装测试模块。请由开发者在本机按团队依赖管理方式安装 Playwright，并确认浏览器驱动可启动；不要把它写入 `requirements.txt`，也不要把它作为演示服务运行时依赖。

## 3. 运行命令

先启动演示服务：

```sh
./run.sh
```

另开终端，在仓库根目录运行 E7-B 浏览器用例：

```sh
PYTHONPATH=app python3 -m unittest tests.test_banking_flow_ux.BankingFlowBrowserTests -v
```

运行已有 C7 浏览器用例：

```sh
PYTHONPATH=app:tests python3 -m unittest test_c7_elder_mode.ElderModeTests.test_browser_mode_toggle_fraud_and_customer_service -v
```

运行全量回归：

```sh
PYTHONPATH=app python3 -m unittest discover -s tests -v
```

未设置 `C7_NODE_BINARY` 或 `C7_PLAYWRIGHT_MODULE` 时，浏览器用例显示 `skipped` 是预期结果；不应因此把浏览器依赖加入生产运行环境。若要保存答辩截图，可先设置：

```sh
export FLOW_SCREENSHOT_DIR="$PWD/docs/assets/e7b"
mkdir -p "$FLOW_SCREENSHOT_DIR"
```

现有 C7 用例也支持 `C7_SCREENSHOT_DIR`：

```sh
export C7_SCREENSHOT_DIR="$PWD/docs/assets/e7b-c7"
mkdir -p "$C7_SCREENSHOT_DIR"
```

## 4. 自动化验收步骤与通过标准

| 步骤 | 操作 | 通过标准 | 建议截图 |
|---|---|---|---|
| 1 | 打开首页，检查长辈模式、输入框、语音按钮 | 控件有可读名称；聊天区 `role=log` 且 `aria-live=polite`；计划区有动态播报 | 首页、焦点落在输入框 |
| 2 | 点“转一笔钱”，输入 0 或非法金额 | 错误提示进入 live region，明确说明金额必须大于 0；不只用红色表达 | 金额错误提示 |
| 3 | 输入“给小王转100元” | 两位同名候选同时展示脱敏后四位，要求补充信息，不自动选择 | 联系人消歧卡 |
| 4 | 输入“给妈妈转5000元”，点击确认 | 计划卡显示风险等级文字、非颜色图标和 `aria-label`；确认后进入验证码对话框 | 风险计划卡、认证框 |
| 5 | 输入演示验证码并继续 | 验证码输入框、验证按钮有可读名称；动态状态可被读屏读取 | 验证中/验证通过 |
| 6 | 再次确认并等待回执 | 回执明确显示结果；`aria-live` 状态同步更新；不宣称未查询到的数字 | 已完成回执 |
| 7 | 切换长辈模式及公共播报保护 | 正文与按钮达到适老化字号；隐私开关开启时金额/账号不进入播报文本，关闭后由用户承担公共环境风险 | 长辈模式、隐私开关 |

风险等级必须同时提供文字（绿色/黄色/红色及状态）、图标（✓/!/⚠）和 `aria-label`，颜色只作辅助。错误、认证、计划状态和回执均使用可读的动态区域；焦点应按输入→确认→认证→回执的业务顺序移动，不能跳过关键确认节点。

## 5. 读屏人工复核建议

- macOS VoiceOver：`⌘F5` 开关，使用 `Control+Option+Right Arrow` 逐项检查联系人候选、金额、确认按钮和回执；关闭“语音播报时隐藏金额和账号”时只在私密环境操作。
- Windows NVDA：使用 `NVDA+空格` 切换浏览/焦点模式，检查 `role=dialog`、`role=status`、`role=alert` 和按钮名称。
- 验证错误提示、风险等级和回执均不依赖颜色；缩放或长辈模式下检查焦点轮廓仍可见、页面不横向溢出。

## 6. 证据留存与边界

答辩截图至少保留：联系人消歧、风险计划、强认证、最终回执、长辈模式和隐私播报开关六张。测试输出应记录 Node/Playwright 路径、用例命令、通过/跳过明细和日期。

本验收验证的是本地 Mock 银行与演示前端的可访问性路径。真实生产需由银行终端、IAM/核心系统、读屏兼容矩阵和无障碍测试团队重新验收；本项目不将演示级结果表述为监管或生产认证。

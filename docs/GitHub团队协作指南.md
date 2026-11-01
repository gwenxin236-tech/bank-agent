# 银行 AI 智能体应用与 GitHub 团队协作指南

## 0 五人协作流程与当前发布状态（2026-11-01校订）

本节说明项目协作流程，后文保留安装、Git操作和故障排查方法。负责人负责A—D阶段，团队成员执行E阶段，日常协作遵循F阶段；交付检查通过后方可发布。

当前已存在仓库https://github.com/gwenxin236-tech/bank-agent，已发布基线为`release/competition-20261031`分支的`0c6608f4e136da4dd440410fad7b20ef90dc4ce4`。该版本的托管CI证据不代表本地N16新增源码已通过远端验证；本地N16尚未提交或推送。原工作目录与独立发布目录分开管理，不应为匹配下列通用示例而改写原仓库、重建远端或覆盖已有分支。成员邀请、分支保护及真实账号归属仍需按仓库设置核验。依据：`data/verification/finalize-20261031/publication/`历史发布证据；N16当前状态见`docs/assets/N16发行验收.json`。

### A. 负责人确认交付范围与可复制性

位置：本机仓库根目录终端。

1. `git status --short` 看已修改、删除和未跟踪文件；`git diff --name-status` 看本次变更边界；`git remote -v` 确認是否已有远端。不要为了清空状态执行 reset/clean。
2. 本次勘察仍有未跟踪 `app/production_backends.py` 等运行相关文件，以及文档删除/修改。工作区测试成功**不等于当前提交的干净克隆可跑**。先由负责人确认这些文件归属、是否应交付，分任务测试与提交。不能把本指南当作允许批量提交全部残留的授权。
3. 对照 `git ls-files app tests config scripts .github` 检查应交付文件；执行 `git ls-files '*.db' '*.sqlite3' '.env'`，不应跟踪运行数据库/真实密钥。历史若泄露 Key，应先作废并换 Key；仅删除文件不等于删除历史泄露。
4. **审核后**按明确路径暂存：`git add 路径1 路径2`；`git diff --cached --stat`、`git diff --cached` 逐项检查，测试后 commit。文档、运行源码、容器配置不得遗漏，但不要纳入 `_internal/` 或非指定 `submission/`。
5. 已批准的提交准备好后，在原仓库执行 `git clone --no-local . ../banking-release-check`，进入新目录运行 `python3 -m unittest discover -s tests -v`、`python3 scripts/preflight.py`、`./run.sh`。不要从原目录手工复制缺失文件“补绿”；回原仓库正常修复、提交，再在新的空目录克隆。
6. 验证完成：干净克隆测试通过、健康接口正常、网页能办查询/确认流程，且 `git status --short` 没有意外受跟踪变更。浏览器/故障演练需按其说明显式启用，默认 skip 不能当作已跑。

### B. 负责人管理仓库访问权限（建仓步骤仅适用于新项目）

位置：GitHub 网页（负责人自己的账号）。

1. 登录 → 右上角 **+ → New repository** → Owner 选团队组织（没有组织可暂用负责人个人账号）→ Repository name 填 `banking-ai-agent` → 选择 **Private**（除非赛事要求公开并已审核无敏感数据）。
2. **不要**勾选 Initialize/Add README、`.gitignore`、License，本地已存在 Git 历史。点击 **Create repository**。验证：进入空仓库 Quick setup 页面，复制 SSH 或 HTTPS 地址；此时还没有上传任何本地代码。
3. 仓库 **Settings → Collaborators**（组织仓库通常为 **Collaborators and teams / Manage access**）→ **Add people** → 输入团队成员真实 GitHub 用户名 → Invite。四位团队成员分别登录自己的账号，在通知/邮件接受邀请。组织仓库成员通常授予 Write，不授予 Admin；个人仓库权限粒度以实际界面为准。
4. 验证：访问列表里不再是 Pending invite，团队成员能打开私有仓库。仅发邀请没有接受，不能算配置完成。团队成员确认 GitHub 用户名，后面替换 CODEOWNERS 用。
5. 公开仓库免费计划可用分支保护；私有仓库保护取决于 Pro/Team 等计划。若页面提示升级或不可用，请负责人选择合适计划/组织，**不要为解锁功能未经审核改成公开**。手工约定不是技术保护。

### C. 发布与CI检查（下列为新项目通用示例）

位置：已通过 A 验证的本机仓库终端 + GitHub 网页。下列 push 全部由人执行，本轮 AI 不代执行。

```bash
# 先确认批准的版本在 main，不要盲目重命名正在开发的分支
# 若当前不是 main，由负责人核对 git log --all --oneline 后确定发布提交
git branch --show-current
git log -5 --oneline
git remote -v
# 无 origin 时才 add；将 OWNER 替换为真实组织/用户名
git remote add origin git@github.com:OWNER/banking-ai-agent.git
git remote -v
git push -u origin main
# 若 develop 已存在，切换并核对即可，不要 -B 强行覆盖
git switch -c develop main
git push -u origin develop
```

- GitHub **Code** 页切换 main/develop，均应看到批准的提交；**Actions → ci → 最新一次运行 → quality**，检查每个步骤。
- 当前工作流包含 Python 编译、JS 语法、测试、preflight、Docker 构建，不调用付费模型。失败点击红色步骤展开日志，修复后再提交，不用重跑掩盖源码缺失。
- 如果 SSH 未配置，看本文第 5 节；可用 HTTPS + GitHub Desktop 登录，不把账号密码当 Git 密码。`origin already exists` 时先 `git remote get-url origin`，确认再 `git remote set-url origin 正确地址`。
- 验证：两个远程分支存在、main 的 CI 全绿、团队成员能 clone。CI 首次跑过后，`quality` 才可能出现在保护规则的检查下拉框中。

### D. 负责人：分支门禁与 CODEOWNERS（不只是口头约定）

位置：仓库 **Settings → Branches → Add classic branch protection rule**；若界面使用 Rulesets，创建 Branch ruleset，Enforcement 设 Active，目标分支选同名，逐项启用等价选项。

1. 对 `main` 和 `develop` **各建一条**规则。
2. 勾选 Require a pull request before merging、Require approvals（至少 1）、Dismiss stale approvals、Require conversation resolution。
3. 勾选 Require status checks to pass，选第一次 CI 实际产生的 **quality**（界面可能显示 `ci / quality`），可要求分支与目标最新版本同步。不要凭空输入一个从不产生的检查名。
4. 不允许 force push / deletion；可启用 Do not allow bypassing 让管理员也遵守。组织计划支持时限制 main 合并/推送主体为负责人，负责人自己的变更仍由另一位成员评审。
5. 维护 `.github/CODEOWNERS`：当前文件的 `@banking-agent-maintainers` 等是占位，**不代表已经邀请了真实团队**。负责人在单独 PR 中将其改成可访问仓库的真实账号/组织团队：共享核心与安全归负责人；四域和对应测试归本人；负责人负责最终复核。域规则应在共享默认规则之后（后匹配生效）。不把不存在的 `security_ext/` 当作已落地目录。
6. 确认 CODEOWNERS 无错误且评审人有 Write 后再开启 Require review from Code Owners。目录归属是评审规则，**不是对 Write 成员的文件级写入隔离**；审查 PR 文件清单仍必需。
7. 验证：开一个只改自己域 README 的演示 PR 到 develop，未批准时不能合并，CI 出现并通过、负责人审查后可合并；保护规则页面能看到两分支处于生效状态。不要用真实破坏性 force push 来测试保护。

官方依据：[GitHub 分支保护与计划边界](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches)。界面标签随版本/语言可能略有不同，以当前权限页为准。

### E. 团队成员首次配置开发环境（无需模型密钥）

位置：团队成员自己的电脑。

1. 接受邀请；安装 Git、Python 3（建议与 CI 一致用 3.12），编辑器用 VS Code 或本人习惯的工具。Mac/Linux 终端；Windows 推荐 WSL 或 Git Bash，详见第 3–5 节。
2. `git --version`、`python3 --version` 验证；设置自己的提交署名：`git config --global user.name "本人姓名"`、`git config --global user.email "本人 GitHub 已验证邮箱或 noreply 邮箱"`。不要用负责人的账号身份。
3. `git clone 仓库地址` → `cd banking-ai-agent` → `git switch develop` → `git pull --ff-only origin develop`。第一次没有本地 develop 时 Git 会跟踪同名远端；失败则 `git switch --track origin/develop`。
4. 运行 `python3 -m unittest discover -s tests -v`。默认零第三方 Python 运行时依赖，不需要 pip install 模型 SDK；浏览器 E2E 未配置依赖时可能 skip，须记录。Windows 原生环境可用 `py -3` 代替 python3，启动脚本建议在 WSL 执行。
5. `BANK_AGENT_OFFLINE=1 ./run.sh` 启动 → 浏览器访问 `http://127.0.0.1:8090` → “查余额”能返回工具结果，“转一笔钱”必须待确认。另开终端 `curl --fail http://127.0.0.1:8090/readyz` 查看就绪；占用端口先核实已有进程，不乱杀服务。
6. 验证完成后在团队群回复：系统/Python 版本、分支、提交号（`git rev-parse --short HEAD`）、测试摘要、启动截图（不含 Key/真实信息）。在线语音需要负责人批准的个人配置，依本文末尾关联的模型说明，不影响离线开发。

### F. 四域所有权与每日 PR 操作

| 成员 | 独占源目录 | 对应测试 | 支线前缀 |
|---|---|---|---|
| 长辈体验 | `app/features/elderly/` | `tests/test_elderly.py` | `feature/elderly` |
| 反诈守护 | `app/features/antifraud/` | `tests/test_antifraud.py` | `feature/antifraud` |
| 家庭协作 | `app/features/family/` | `tests/test_family.py` | `feature/family` |
| 财务健康 | `app/features/finance_health/` | `tests/test_finance_health.py` | `feature/finance_health` |
| 负责人（security_ext 职责域） | 共享核心、权限、账务、审计、网关和集成 | 共享安全与回归测试 | 按任务创建；AI 默认 `codex/` |

成员先读自己域 README 与 `app/tool_contract.py`。新增工具在域内 register；金额整数分、权限由共享策略决定。成员不得改 `banking_agent.py`、共享认证/审计、其他域、公共前端或 CI；确有需要先向负责人提出。`config/features.json` 仅改本域独立 key，并在 PR 明示，不能重排/覆盖别人的 key。

以反诈成员为例（文件名换成自己的实际文件）：

```bash
git switch develop
git pull --ff-only origin develop
git switch -c feature/antifraud-new-rule
# 先写失败测试，再在自己域实现
python3 -m unittest discover -s tests -v
git diff --check
git diff --name-only
git add app/features/antifraud/example.py tests/test_antifraud.py
git diff --cached
git commit -m "feat(antifraud): 增加反诈提醒规则"
git push -u origin feature/antifraud-new-rule
```

GitHub **Pull requests → New pull request**：base 选 `develop`、compare 选自己的 feature；写“范围/需求/红绿测试证据/无副作用或安全边界/截图”；Reviewers 选负责人；检查 **Files changed** 只有本域和声明的本域配置。CI 绿不代表自动批准。负责人确认→合并 develop→集成回归→发布 PR（base main，compare develop）→另一成员审查→负责人合并。合并后本人同步 develop，再开新的域支线；不要在旧支线上反复堆积。

**待负责人完成的外部操作**：批准残留文件交付、创建仓库、接受邀请、绑定远端、push、保护规则及真实 CODEOWNERS、团队成员的计算机安装与麦克风授权。在线语音的 Key 申请/充值/安全输入见《大模型接入说明》。不要把“已编写操作文档”等同于这些账号侧动作已完成。

## 1 指南用途

本指南面向项目负责人和团队成员，说明如何创建 GitHub 仓库、首次上传银行 AI 智能体项目、在自己的电脑上安装 Git 与代码管理器、创建功能支线、同步主线、提交代码、推送远端、发起 Pull Request、进行代码审查、运行 CI、解决冲突和保护密钥。项目当前未绑定具体 GitHub 远端仓库，因此本指南给出可直接执行的通用流程；真正执行 push 前，需要由仓库管理员创建仓库并把远端地址替换为团队自己的地址。

## 2 推荐团队协作方式

本项目采用五人团队约定的 `feature/<域名> → develop → main`：`main` 只保留负责人验收通过的版本，`develop` 是日常集成分支；每项需求从 `develop` 创建自己的域支线，完成后 PR 到 `develop`，负责人再通过发布 PR 合入 `main`。不要直接在 `main` 上开发，不要把 API Key、真实客户数据、SQLite 运行库或个人隐私文件提交到远端。

建议角色如下：

| 角色 | 主要职责 | GitHub 权限建议 |
|---|---|---|
| 仓库管理员 | 创建仓库、邀请成员、保护 main、维护 Secrets | Admin |
| Agent 负责人 | 意图解析、计划、工具网关、模型适配 | Maintain 或 Write |
| 前端负责人 | 适老化页面、交互、可访问性、演示流程 | Write |
| 安全负责人 | 威胁模型、授权、审计、攻击用例、依赖检查 | Write |
| 文档与答辩负责人 | 方案、演示脚本、答辩材料、发布说明 | Write |

## 3 创建 GitHub 账号和团队仓库

1. 在 GitHub 官网注册账号并完成邮箱验证，建议使用长期有效的团队邮箱或学校邮箱。
2. 建议创建一个组织，例如 `banking-agent-team`，再在组织内创建仓库；如果比赛时间紧，也可以先由队长创建个人仓库。
3. 新建仓库时选择 Private 或 Public。比赛提交前按赛题要求决定是否公开；涉及真实银行数据时必须保持 Private。
4. 仓库名建议使用 `banking-ai-agent`。不要勾选自动生成 README、.gitignore 或 License，避免与本地项目首次提交产生冲突；本项目已经包含这些文件。
5. 在仓库 Settings、Collaborators and teams 中邀请成员，按最小权限分配 Write 或 Maintain。
6. 在 Settings、Branches 中为 `main` 添加保护规则：要求 Pull Request、至少一名审查人、CI 通过、禁止强制推送、禁止直接删除主线。

远端仓库地址通常形如：

```text
git@github.com:组织名/banking-ai-agent.git
https://github.com/组织名/banking-ai-agent.git
```

SSH 更适合长期团队开发；HTTPS 适合临时环境，但通常需要 Personal Access Token，不能把账号密码写进命令或脚本。

## 4 安装 Git 和代码管理器

### 4.1 macOS

安装 Xcode Command Line Tools：

```bash
xcode-select --install
```

也可以从 Git 官网安装 Git。安装完成后检查：

```bash
git --version
```

推荐安装一个代码管理器：

- GitHub Desktop：适合初学者，可视化查看改动、切换分支和创建 Pull Request。
- VS Code 或 Cursor：适合查看代码、运行终端、安装 Python 和 Markdown 插件。
- PyCharm：适合 Python 项目和测试调试。

### 4.2 Windows

安装 Git for Windows，并在安装选项中保留 Git Bash。安装 GitHub Desktop，再安装 VS Code 或 PyCharm。打开 PowerShell 或 Git Bash 检查：

```bash
git --version
```

### 4.3 Linux

以 Debian 或 Ubuntu 为例：

```bash
sudo apt update
sudo apt install -y git
sudo apt install -y python3 python3-venv
```

然后安装 VS Code 或其他团队统一的代码编辑器。

## 5 配置身份与登录

每台电脑第一次使用 Git，都要配置提交署名。邮箱应与 GitHub 账号中已验证的邮箱一致：

```bash
git config --global user.name "成员本人姓名"
git config --global user.email "成员本人邮箱"
git config --global init.defaultBranch main
git config --global pull.rebase true
```

检查配置：

```bash
git config --global --list
git config user.name
git config user.email
```

### 5.1 使用 SSH 登录 GitHub

生成密钥时不要覆盖已有密钥：

```bash
ssh-keygen -t ed25519 -C "成员本人邮箱"
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
pbcopy < ~/.ssh/id_ed25519.pub
```

在 GitHub 的 Settings、SSH and GPG keys、New SSH key 中粘贴公钥。验证连接：

```bash
ssh -T git@github.com
```

看到 GitHub 的成功认证提示后，再使用 `git@github.com:组织名/仓库名.git` 形式的远端地址。

### 5.2 使用 HTTPS 登录

使用 HTTPS 时，GitHub 不再接受普通账号密码作为 Git 密码。创建 Fine-grained Personal Access Token，只授予目标仓库的 Contents、Pull requests 和 Metadata 所需权限；令牌只保存到系统凭据管理器中，不能写入 README、脚本或截图。

## 6 队长首次上传本地项目

本项目目录为 `银行ai智能体app`。上传前先检查：

```bash
cd "/Users/c02702/Desktop/银行ai智能体app"
git status
git remote -v
```

如果目录还没有 Git 仓库，执行：

```bash
git init
git branch -M main
# 仅用于新空目录；当前仓库已经初始化，不要重复执行这一段
git add README.md
# 其余文件逐个审核后按路径添加，不要全量暂存
git status
git commit -m "chore: initial banking ai agent project"
git remote add origin git@github.com:组织名/banking-ai-agent.git
git push -u origin main
```

如果本地已经有 Git 仓库，先完成下方第 0 节的交付文件核对和干净克隆验证；确认 `main` 指向批准的提交后，才由负责人添加远端并推送（地址须替换）：

```bash
git remote add origin git@github.com:组织名/banking-ai-agent.git
git push -u origin main
```

如果远端已经自动生成 README，先不要强行覆盖；可先备份本地改动，再执行：

```bash
git fetch origin
git pull --rebase origin main
git push -u origin main
```

上传后在 GitHub 页面确认 README、app、tests、docs 和 `.github/workflows/ci.yml` 已存在。

## 7 团队成员第一次加入项目

每位成员在自己的电脑上选择工作目录并克隆项目：

```bash
cd ~/Projects
git clone git@github.com:组织名/banking-ai-agent.git
cd banking-ai-agent
git remote -v
```

首次运行前，复制环境变量模板，不要复制真实密钥：

```bash
cp .env.example .env
```

复制模板不代表配置生效：`run.sh` 不自动加载 `.env`。离线演示不需要填写 Key；在线验证请按《大模型接入说明》在启动终端导出变量。当前 CI 是离线测试，不需要模型 Secrets；不要为“让 CI 通过”上传真实密钥。随后按 README 启动 Web Demo，并在另一个终端运行测试：

```bash
./run.sh
python3 -m unittest discover -s tests -v
```

## 8 每项需求创建支线

开始新需求前先同步主线：

```bash
git switch develop
git pull --ff-only origin develop
git switch -c feature/antifraud
```

分支命名建议：

| 类型 | 示例 | 用途 |
|---|---|---|
| feature | `feature/antifraud` | 新能力 |
| fix | `fix/idempotency-replay` | 缺陷修复 |
| security | `security/toctou-check` | 安全加固 |
| docs | `docs/github-guide` | 文档更新 |
| chore | `chore/ci-python-version` | 工程维护 |

一条支线只解决一个主题，不要把格式化全项目、无关功能和安全修复混在同一个 Pull Request 中。

## 9 修改、检查、提交和推送

修改代码后先看差异，再运行测试：

```bash
git status
git diff
python3 -m unittest discover -s tests -v
git diff --check
```

确认没有密钥和本地数据后提交：

```bash
git add app/features/antifraud/example.py tests/test_antifraud.py
# 示例路径须换成本人实际修改文件；逐个审核，不使用 git add . 或 git add -A
git commit -m "feat: add elder-friendly fraud guard flow"
git push -u origin feature/antifraud
```

提交信息建议使用清晰的前缀：

- `feat:` 新功能
- `fix:` 缺陷修复
- `security:` 安全控制
- `docs:` 文档
- `test:` 测试
- `chore:` 工程维护

一个提交应当是可理解、可回滚的最小变更，不要使用“修改一下”“最终版”“test”这类无法说明内容的提交信息。

## 10 Pull Request 和代码审查

推送支线后，在 GitHub 中点击 Compare & pull request，目标分支选择 `develop`（只有负责人发布 PR 才选择 `main`）。Pull Request 描述至少写清：

1. 解决了什么用户问题。
2. 修改了哪些文件和接口。
3. 如何本地验证，列出实际运行的测试命令。
4. 是否涉及数据库、权限、审计、模型调用、敏感数据。
5. 是否有已知限制、后续工作和演示步骤。

审查人重点检查：

- Agent 是否仍然通过工具白名单执行，模型是否越权。
- 金额是否严格使用人民币分整数，是否拒绝负数、零、浮点和科学计数法。
- 黄色确认是否绑定任务、步骤、实体摘要并且只能消费一次。
- 余额、额度和风险是否在副作用前重新校验。
- 重试、双击和超时是否有幂等保护。
- 前端是否用自然语言解释，不把内部 JSON 当成普通用户界面。
- 测试是否覆盖正常路径、边界条件和攻击用例。

只有 CI 通过、审查人批准、冲突解决后，才允许合并。合并完成后删除远端支线，成员再同步主线。

## 11 合并后的日常同步

```bash
git switch develop
git pull --ff-only origin develop
git branch --merged
git fetch --prune
```

如果需要继续开发旧支线，先把主线变更变基到支线：

```bash
git switch feature/antifraud
git fetch origin
git rebase origin/develop
# 解决冲突后
 git add 冲突文件
git rebase --continue
git push --force-with-lease
```

只允许使用 `--force-with-lease` 更新自己尚未合并的支线，禁止对 `main`、`develop` 强制推送。

## 12 冲突解决方法

发生冲突时先查看状态：

```bash
git status
git diff --name-only --diff-filter=U
```

打开冲突文件，处理 `<<<<<<<`、`=======`、`>>>>>>>` 标记，保留正确逻辑，然后：

```bash
git add 已解决的文件
# 如果是在 merge
git commit
# 如果是在 rebase
git rebase --continue
```

如果发现方向错误，可以安全退出当前 rebase：

```bash
git rebase --abort
```

冲突解决后必须重新运行完整测试，尤其是安全、权限、金额和审计相关测试。

## 13 密钥、数据和提交前检查

`.gitignore` 应至少忽略：

```text
.env
.env.*
!.env.example
*.sqlite3
__pycache__/
*.pyc
.DS_Store
*.log
```

严禁提交：

- 大模型 API Key、银行接口密钥、JWT 密钥、数据库密码。
- 真实客户姓名、身份证号、银行卡号、手机号、交易流水。
- 本地 SQLite 数据库、导出的审计日志和真实截图。
- 含有口令的 Postman、浏览器开发者工具和终端截图。

如果密钥误提交，不能只删除当前文件；应立即撤销并重新生成密钥，再按仓库管理员流程清理 Git 历史。不要把“删掉文件”当成密钥已经安全。

## 14 GitHub Actions 和质量门禁

项目已提供 `.github/workflows/ci.yml`。建议 CI 至少执行：

```text
安装受支持的 Python 版本
运行 unittest
检查 Python 语法
检查前端 JavaScript 语法
检查 git diff --check
```

银行 Agent 的合并门禁还应逐步增加：

- 依赖漏洞扫描和许可证扫描。
- Secret scanning 和 push protection。
- 静态代码分析、依赖锁定和 SBOM。
- 单元测试、集成测试、端到端演示测试。
- 业务逻辑攻击回归：负金额、重复确认、旧 token、TOCTOU、冷静期绕过。

## 15 团队分工和发布节奏

建议按以下节奏管理：

1. 每个工作日更新 Issue、分支和 Pull Request 状态。
2. 需求通过 Issue 描述，写清验收条件和安全边界。
3. 每个功能至少配一条正常测试和一条异常或攻击测试。
4. 每周从 `main` 打一个演示标签，例如 `demo-2026-09-18`。
5. 答辩演示只使用固定的 MockBankCore 和脱敏数据，不接真实账户。
6. 发布前由安全负责人检查变更、配置、日志和文档是否一致。

## 16 当前项目的 GitHub 上传边界

当前项目已经完成本地 Git 结构、README、测试、CI、部署文件和方案文档，但没有自动上传到任何 GitHub 仓库，因为用户尚未提供仓库地址和授权。执行真实上传还需要团队管理员完成以下动作：创建远端仓库、邀请成员、配置 Branch Protection、选择 SSH 或 HTTPS、设置 CI Secrets，并确认项目中的 `.env` 和本地数据库未被提交。

当前项目仍是竞赛演示和工程基线，不是已经取得银行商用资质的生产系统。接入真实银行核心、真实 IM 或短信通知前，应另行完成实名身份、强认证、风控反洗钱、密钥管理、审计留痕、等保评估、渗透测试、灾备和运营审批。

## 17 常用命令速查

```bash
# 查看状态和提交记录
git status
git log --oneline --decorate -10

# 更新主线
git switch develop
git pull --ff-only origin develop

# 创建支线
git switch -c feature/your-topic

# 提交和推送
git add path/to/file
git commit -m "type: short description"
git push -u origin feature/your-topic

# 查看远端和清理远端删除的分支
git remote -v
git fetch --prune

# 撤销未提交的单个文件改动，执行前先确认
git restore path/to/file
```

## 18 完成标准

一个功能只有同时满足“代码已提交、测试通过、Pull Request 已审查、CI 通过、文档同步、没有泄露密钥、可以由另一位成员从干净环境复现”时，才算真正完成。对于转账、反诈、家庭授权和其他资金影响操作，还必须有可回放审计和失败恢复说明。

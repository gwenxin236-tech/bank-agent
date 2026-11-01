# **P03：RAG知识库、引用与验收**

日期：2026-10-23。复用现有主链与P02模型客户端，标准库实现，无新增运行依赖；本批不做Git提交或推送。基础批与扩展批分别保存证据，当前扩展状态见第7节。

## **1. 当前交付能力**

| 能力 | 本批实现 | 代码依据 |
|---|---|---|
| 知识导入 | 固定本地JSON清单，严格模式、体积/字段限制、审核标记、版本、到期日、公开/操作人白名单 | `app/knowledge_service.py:247`，`config/knowledge.json:1` |
| 切分与索引 | 最长320字符片段；记录展开后文本偏移、内容hash；中文二元词及英文词倒排索引，关键词加权 | `app/knowledge_service.py:145`、`:232`、`:399` |
| 权限过滤 | 可信调用方提供actor；白名单与到期过滤先于排序/top-k；无跨用户答案缓存 | `app/knowledge_service.py:399` |
| 原文与引用 | 标注[K1]/[K2]、资料名、文件行号、版本、源文件及片段SHA256；动态规则附当前配置值 | `KnowledgeChunk.citation`、`KnowledgeService.answer` |
| 无证据拒答 | 无命中、无权限、过期不臆答；损坏/缺失知识库只使知识问答不可用 | `KnowledgeService.from_path/search/answer` |
| 抽取式RAG | 可选真实模型从已授权DATA中选完整原文；输出ID与原文逐字匹配后才采用 | `app/knowledge_service.py` 的 SYSTEM_PROMPT、answer |
| 主链 | knowledge_query → PolicyEngine绿色只读裁决 → knowledge_lookup → 原DAG/审计 → 回复 | `app/banking_agent.py` 的 IntentParser、PolicyEngine、Planner、AgentRuntime |
| 展示 | 沿用Web对话与结果卡；适老模式保留引用全文，避免通俗词替换改变原文 | `app/banking_agent.py` 的 present_plain_response；浏览器验收记录 |

当前支持**本地模型离线向量检索＋可选在线向量混合检索＋受约束原文抽取的RAG**。本地模型与依赖必须完整准备或使用包含它们的离线交付包；没有本地模型时基础版仍用关键词检索。embedding与聊天引用选择分别配置；答案仍由服务端从已核对资料组装，不接纳无精确出处的模型解释。第8节说明可迁移离线版本，前面的基础/在线扩展测试数字均是历史快照。

## **2. 怎样使用**

### **2.1 默认离线知识问答**

- 在已启动的网页输入`知识问答：什么是冷静期`或`银行知识：确认卡是什么`。必须有中文或英文冒号；这个明确入口避免将普通转账需求误切到解释通道。
- 可演示问题：`知识问答：三色权限是什么`、`知识问答：小额阈值`、`知识问答：未知结果怎么办`、`知识问答：订阅取消有什么含义`。
- `知识问答：火星航班`应拒答。`查余额`和`给妈妈转100元`仍走原业务链；知识模式中的账户事实/执行请求不能凭资料办理。
- 已运行的旧服务需要重启。基础批未更新Docker；扩展批会尝试构建和容器验收，具体通过状态与镜像ID仅以第7节及实际报告为准。

### **2.2 启用模型选择引用**

- 先按[P02说明](P02模型接入与验收.md)配置用户环境变量。在已继承模型配置的PowerShell中执行下列命令，再打开本地网页。Python不自动加载`.env`。
- `RAG_USE_MODEL=1`显式启用外部模型；未启用时，知识问答不发模型请求。离线开关或`AgentRuntime(offline=True)`会禁用外发。共用`self.parser.llm.client`，与意图解析共享P02的实例内频率/并发限制。

```powershell
# 先进入已解压的项目根目录，再执行后续命令
$env:BANK_AGENT_OFFLINE='0'
$env:RAG_USE_MODEL='1'
$env:VOICE_ENABLED='0'
python app/banking_agent.py
```

- 仅在资料允许外发时启用在线模式。发送内容为当前知识问题、最多三个已授权片段的文本和ID，以及固定引用选择指令；不附余额、交易结果、会话历史、读者白名单或其他主体的资料。`readers`是本地阅读控制，不是外发同意凭据；私有资料外发仍需另行获得允许。
- 本次只明确获准外发“什么是冷静期”及指定的24小时演示冷静期片段和引用选择指令，并按此范围验收；该授权不扩展到其他资料。未针对其他资料取得真实API外发验收证据。

### **2.3 接口返回**

- Web仍走带登录演示会话的`POST /api/chat`，没有新建匿名知识接口，也不从HTTP正文、模型或渠道metadata接收ACL、角色、资料路径。引用在`result.results[tool=knowledge_lookup].data.citations`，答案在同层`answer`及`result_view.user_message`。
- 状态包括`ANSWERED / NO_EVIDENCE / OUT_OF_SCOPE / INVALID_QUERY / UNAVAILABLE`。工具读取完成的外层`SUCCESS`不等于已有可回答证据，更不表示发生银行交易；应查看知识结果的内层status。
- 模型证据含attempted/accepted/reason及P02安全耗时/用量；`mode=model_extractive`才表示本次通过精确原文核对后采用模型选择。模型失败或伪造引用会使用`local_extractive`，不会伪装为模型验收成功。

## **3. 资料清单与维护**

### **3.1 当前资料范围**

- `config/knowledge.json`现有11张公开卡：原9张项目解释/路径卡，加2张中国银行官网产品资料摘要。前9张基于代码行为，后2张标注银行出处、采集日及复核期限，不能混称为本项目的产品。
- 规则解释依据包括`PolicyEngine`、`MockBankCore.check_transfer_constraints`、`present_result`、订阅三层数据及`handoff_queue.py`；不是银行官网产品条款或监管解释。
- 原L3草案的20–30张卡属于内部规划；不为凑数编造产品。扩展批已导入“整存整取”“定期一本通”公开摘要，不固定写入利率、不承诺收益，也不代表已经接通银行接口。

### **3.2 配置字段**

| 字段 | 要求 |
|---|---|
| schema/version/documents | 清单固定schema=1、版本字符串、最多100条资料；总文件不超过512KiB |
| id/title/kind/version | 唯一稳定ID、标题、rule_explain/process_path/product_fact分类、资料版本；分类存在不代表已填充正式产品资料 |
| reviewed | 只有true才纳入；只是维护者的审核声明，不是自动安全认证或数字签名 |
| access/readers | public且readers=[]；或restricted且readers为明确操作人ID白名单。不能从用户文本获取 |
| owner_id/family_readable | 本人私有资料的主体及显式家庭共享标记；共享须restricted、owner在readers内，默认false。资金代办授权不会自动让所有私有资料可读 |
| embedding_allowed | 仅public资料且明确标记true才可送向量模型；还需运行配置同意。私有/家庭资料永不送embedding，查询不持久缓存；该外发标记不限制第8节纯本地计算 |
| publisher/source_url/retrieved_at/source_excerpt | 官网来源、采集日期与短核对摘录；当前限定中国银行官方HTTPS页面，保留摘录hash，不冒充整页存档hash或银行生效日期 |
| valid_until | ISO日期；每次检索检查UTC日期，过期内容不再召回；维护者应按实际复核周期更新 |
| patterns/text | 检索短语和原文；原文最多12000字符，不放密钥、客户记录、账号或未审核指令 |

- 动态阈值仅接受`{{policy.small_transfer_limit_cents}}`、`{{policy.daily_transfer_limit_cents}}`、`{{policy.cooling_off_hours}}`，从注入的同一服务端配置快照填入；金额用整数分转换显示，资料不另存一份固定金额。引用的policy_evidence记录实际值，不能读取OTP等秘密配置。
- 片段的offset_start/end指展开后文本的字符偏移；line_start/end指JSON中原文模板字段所在行，source_sha256对应完整源文件，content_sha256对应展开后的片段。模板数值与文件字面量不同，应结合policy_evidence核对。
- 当前固定长度切分可能切断长句，属于检索质量边界；默认9张短卡均在单片段范围。正式长条款导入前应增加按章节/句子切分和人工质量验收。

### **3.3 修改、撤销与重新加载**

- 维护者更新内容/白名单/审核状态/期限时，同时更新资料版本，并复核出处。知识库不接收浏览器上传、任意URL抓取或目录递归扫描。
- 每次回答前及模型调用后核对源文件hash。内容或ACL发生变更、文件删除或损坏时，旧索引拒绝继续回答；审核后重启服务重新加载。无热更新承诺。
- 新请求不能继续用旧索引；历史审计/已返回引用仍属于当时的操作记录，不因索引失效自动删除。生产留存、访问撤销和合规流程需在P09等阶段完善。

## **4. 安全边界**

### **4.1 资料注入防护**

- 入库时对标题、检索短语和原文做规则筛查；未审核、明显指令投毒及敏感内容不进入可检索索引。已授权片段放在用户消息的DATA字段，不拼进系统消息。
- 模型只能返回片段ID与该片段完整原文；伪造ID、裁切/改写原文、添加其他字段、重复引用和空答案都不作为模型成功结果采用。模型不能提供新的银行事实或权限字段。
- 筛查是启发式规则，不能证明所有自然语言投毒均可识别。主要执行隔离仍来自只读工具绑定、服务端PolicyEngine、精确原文核对与无副作用能力；维护者仍须审核来源。

### **4.2 身份与业务事实**

- ACL过滤使用受信入口提供的实际操作人。现有HTTP演示身份仍不是生产IAM；伪造user_id的请求被原登录链拒绝。扩展批允许家庭成员本人读取公开/明确本人可读资料；跨主体时须有效守护关系与显式family_readable，检索前后重查撤销/到期和主体绑定。其余银行业务白名单、金额与认证条件保持原样。
- `knowledge_query`和`knowledge_lookup`双向绑定；知识计划不能改调余额或资金工具，其他意图也不能借用知识工具。绿色只读分支在PolicyEngine内新增，未由模型或资料决定；原policy配置中的业务分级集合保持原样。
- 资料只解释，余额和交易终态只能来自银行工具；模型故障不重试资金操作。敏感知识问题先清空问题实体后生成计划，避免已识别的密钥等进入计划与审计；该规则不是完整DLP认证。
- 原文引用在适老模式也保留逐字内容，周边界面仍可做通俗说明；页面以文本展示，未新增可执行HTML或任意链接。

## **5. 基础批测试与实测记录（历史）**

| 验收 | 当前结果 | 证据 |
|---|---|---|
| RAG专项 | 22项通过 | `tests/test_rag.py`；`data/verification/p03-20261023/unit-tests-final.log` |
| 主链/HTTP | 策略、DAG、审计、身份拒绝、原转账/余额入口、工具意图绑定、资料故障、敏感问题不落盘 | `RagMainChainTests` |
| 检索与模型替身 | ACL/到期/审核、损坏/重复清单、源文件变更、引用真实性、无命中、模型超时及投毒 | `KnowledgeTests` |
| 默认知识卡 | 9张卡各以首个检索短语验证，均命中并引用预期资料；不是广泛同义问法质量集 | `data/verification/p03-20261023/knowledge-smoke.json` |
| 真实模型RAG | PASS；model_extractive、HTTP 200；214输入+79输出=293 tokens，模型耗时3761ms，审计有效、余额未变 | `data/verification/p03-20261023/live-rag.json` |
| 浏览器专项 | PASS；适老模式原文逐字、来源行号可见、无命中拒答、无确认按钮、0页面错误 | `data/verification/p03-20261023/browser-rag.json`及`browser-rag.png` |
| 最终完整回归 | PASS，600项、0跳过、0 ResourceWarning，日常数据库哈希未变 | `data/verification/t03-20261023-182721-127645/report.json`；公开快照`docs/验收摘要.json` |

- 测试先行的失败记录保留：服务不存在时导入失败、源文件变更未拦截的2项失败、适老替换破坏原文的1项失败；修复后专项全绿。旧回归数字不能替代最终快照。
- 本次仅一次经明确授权的真实模型知识请求，不是银行/生产认证、全部知识准确率或性能压测。单次耗时不代表P95/P99。Docker镜像、离线发行与目标机验证仍归P11/T14。
- 用户本轮已确认此前聊天中出现的旧密钥已撤销；这是用户确认，未通过平台管理接口独立核验。P02文档中的未确认状态属于该批历史记录。

## **6. 后续衔接**

- P04接OCR并保留字段人工核对；P05处理音频hash；P06动态DAG仍受原工具/权限/预算约束。
- 后续扩大银行资料覆盖、领域阅读契约和自然问法质量集时分别验收。基础批的向量/产品/家庭阅读缺口已进入本次扩展；自由改写回答仍需另行设计证据核验，不因向量检索完成而放宽原文约束。

## **7. P03扩展：向量、正式资料、家庭阅读与镜像**

### **7.1 向量语义检索配置**

- 复用P02的密钥和受限HTTP helper，新增标准库`EmbeddingClient`调用`/v1/embeddings`。默认模型为`BAAI/bge-m3`，是否可用以本账号实际验收为准。[SiliconFlow官方embedding协议](https://docs.siliconflow.cn/docs/api/embeddings-post)。
- 仅在明确允许公开资料和问题外发后启用以下配置；关闭聊天引用模型也可单独使用向量检索。当前终端应先继承P02的LLM环境变量，不将密钥复制进命令或文件。

```powershell
$env:BANK_AGENT_OFFLINE='0'
$env:RAG_USE_EMBEDDINGS='1'
$env:RAG_EMBEDDING_CONSENT='public'
$env:RAG_EMBEDDING_MODEL='BAAI/bge-m3'
$env:RAG_USE_MODEL='0'
python app/banking_agent.py
```

- 拒绝私密/空/过长输入，最多16条/批、总16000字符；输出最多2MiB。校验模型ID、条数、索引唯一性、统一维数、有限数值和非零范数，拒绝NaN/布尔/极端值。每实例滚动60秒最多20次、并发不超过P02配置，无自动重试；限额不是账号消费封顶。
- 标准库执行归一化和精确余弦扫描，无向量数据库依赖。仅对已授权、未过期、公开且允许外发的片段建索引，最多64片段；按模型/片段/源文件hash缓存向量于内存。重启后首次使用重新建立，运行时查询向量不写磁盘；验收记录仅对本次获准的固定公开样本额外保存向量，用于无新增外发的本地重放。
- 余弦门槛0.50，与原关键词分合并排序；默认答案仅显示最佳片段，其他候选只交给显式启用的受约束模型择取，减少无关的次要引用；失败/离线/超限回退关键词，检索证据明确标注local_lexical或hybrid_vector。该门槛只是本版参数，不能承诺所有无关问题均可识别。质量结论仅限实际记录的测试集。
- embedding只对经明确同意的公开数据验收；私有资料读取成功也不授予外发权，家庭代读或当前可见资料包含私有内容时，整个问答均只用本地检索，也不调用聊天引用模型。默认功能不向公网发送任何资料。

### **7.2 正式公开资料来源**

| 资料 | 来源及复核边界 |
|---|---|
| 中国银行整存整取 | [官方产品页面](https://www.boc.cn/pbservice/pb1/200806/t20080625_699.html)；采集2026-10-23，仅概括存入/支取及到期转存说明，不固定写入利率 |
| 中国银行定期一本通 | [官方产品页面](https://www.boc.cn/pbservice/pb1/200902/t20090219_588012.html)；采集2026-10-23，仅概括集中管理多笔定期存款，不表示本项目可办理开户 |

- 两卡为人工核对后的摘要，附官网链接与短摘录hash。内部复核截止2026-11-02是维护规则，不是银行承诺的产品有效期；页面URL的历史日期也不当作当前条款生效日。到期后需重查官网再更新。
- 运行时不抓取官网、不接受任意URL；官网可能更新，用户实际办理仍需核对银行当时条款。引用同时保留本地摘要行号、版本和源文件hash。

### **7.3 家庭阅读契约**

| 情况 | 规则 |
|---|---|
| 本人/公开资料 | 已认证家庭成员可走知识入口；本人私有资料仍按readers核验 |
| 代读其他主体 | 原FamilyAccess绑定真实演示操作者/设备和subject，PolicyEngine检查守护关系的双方、时间、撤销和delegate/assist模式 |
| 私有资料共享 | 必须owner_id等于本次subject、family_readable=true，且资料本身为受限资料；共享标记默认为false |
| 撤销/到期/并发变更 | 读取前后重新核验；不再有效时返回ACCESS_DENIED且不输出原文，不自动给其他家庭主体的资料 |
| 外发 | 家庭/私有资料不进云端embedding或外部聊天模型；第8节本地模型可处理经授权资料。资金权限和强认证不因知识读取而变化 |

- 标记仍是本地受信维护配置，不冒充真实所有人线上授权或生产IAM。成员Agent ABI未变，领域Agent仍不能自行更改actor/subject或拼接家庭Context获取阅读权。

### **7.4 本批验收状态**

| 项目 | 当前证据 |
|---|---|
| 专项自动化 | 37项通过，含新增向量协议/缓存/降级、正式出处和家庭主体/撤销测试；`data/verification/p03-extension-20261023/unit-tests-final.log` |
| 实际embedding质量 | PASS；获准公开范围内9次真实BAAI/bge-m3请求，1024维；7个业务问法目标top1均正确、1个无关问题拒答。最终原文选择另以真实向量本地重放8例通过，记录live-embedding.json及final-vector-replay.json |
| 最终完整回归 | PASS，615项、0跳过、0 ResourceWarning，日常数据库哈希未变；data/verification/t03-20261023-211419-816567/report.json |
| Docker | PASS；引擎恢复后最终代码重新构建，28项隔离容器检查通过。镜像bank-agent-qa:t04-20261023-211431-988474，ID前缀c10afa52bee1；报告data/verification/t04-20261023-211431-988474/report.json |

- 基础批第5节是历史记录，当前扩展最终快照以本节和`docs/验收摘要.json`为准。本批真实embedding及隔离容器另有实际通过记录，不把替身结果作为其替代。离线包导出与第二台电脑复验仍属P11/T14，本批镜像验收不代替这些交付。
- 本服务是平台能力，不改变成员DomainContext/Finding/Proposal ABI，也不允许成员将知识引用纳入银行事实快照绕过Supervisor相等核验。


## **8. 可迁移的离线向量语义检索（2026-10-23）**

### **8.1 模型与运行边界**

- **本地推理**：原模型为`BAAI/bge-small-zh-v1.5`，使用Xenova转换的ONNX INT8权重；固定版本`75c43b069aac4d136ba6bc1122f995fedcfd2781`，512维、最大512 token、CLS池化后L2归一化，CPU运行。查询添加原模型检索指令；资料不加查询指令。按真实token数检查上限，超长输入拒绝并标记`local_input_too_long`，不静默截断。长文本切片优先在句号、分号和换行处收束，上限320字符、重叠32字符；引用仍绑定原始起止偏移。模型缓存身份包含清单哈希，换版本不会复用旧文档向量。不要求GPU、API密钥或付费额度。
- **真实新问题**：文档向量和每次新问题的查询向量都在本机计算。索引仅在进程内存中缓存；重启、清空索引或换电脑后，使用随包模型和资料重新建立，不依赖以前问过的问题或云端预计算向量。
- **授权范围**：本地模型可对当前操作人实际可读的公开/私有资料及明确共享的家庭资料做向量检索。先按主体、有效期和共享标记筛选，再计算与排名；回答发出前复查授权。私有向量不写磁盘，不因共享索引缓存扩大可见范围。云端embedding仍禁止家庭/私有上下文外发。
- **相关度边界**：本地BGE使用0.50余弦阈值，与原关键词/完整pattern匹配结合，并对审核pattern中的稀有词加IDF权重。另提供`RAG_FUSION=rrf`的双路名次融合（k=60）供配对评测；当前保留weighted默认，不能将调研的推荐直接等同于本库的最优结论。它是当前小知识库的精确余弦扫描，本地最多2048个可见片段（云端仍为64）；超过上限、模型损坏或依赖缺失时明确降为`local_lexical`，不得将降级冒充语义检索。此处为初始批次边界；新增规模与负载实测见第9节，不能由本地结果推断生产准确率。
- **完整性**：模型、分词器、原始说明、MIT许可证、全部wheel和版本锁均有SHA256清单。禁止隐式模型下载、远程代码或自定义ONNX算子；推理只读本地文件。校验用于发现损坏，不是发布者数字签名。
- **解释边界**：离线向量检索不等于离线聊天大模型。回答继续抽取资料原文并附出处；银行、IM、生产认证等其他能力的边界不因本批改变。

### **8.2 准备与选择后端**

- **维护者首次联网准备**：只下载公开模型、许可证和依赖，不发送知识资料或用户问题。`models/rag/`和`.qa-tools/local-rag/`是交付资产，不进入源码Git提交。模型来源和完整哈希在`models/rag/manifest.json`；两平台依赖各有`requirements.lock`及`manifest.json`。

```powershell
python scripts/prepare_local_rag.py --download --windows
python scripts/prepare_local_rag.py --verify --windows
```

- **本地后端**：`RAG_EMBEDDING_BACKEND=local`强制本地，`auto`默认优先发现项目`models/rag`，`disabled`强制关键词，`siliconflow`选择既有在线后端。离线包明确设置`local`、`BANK_AGENT_OFFLINE=1`和`RAG_USE_MODEL=0`；离线开关只禁止外部服务，不禁用本地模型。在线模式原有公开外发同意开关继续有效。
- **当前电脑原生Python可选路径**：预备的Windows wheel只适配x64 CPython3.13。通过下面命令安装到项目私有目录，不改全局Anaconda环境；Python版本不同应使用Docker交付包。原生ONNX的Windows VC++运行库仍是系统前提，不宣称这些wheel适配所有系统。

```powershell
python scripts/prepare_local_rag.py --verify --windows --install-windows
$env:PYTHONPATH="$PWD\.qa-tools\local-rag\windows-site"
$env:RAG_EMBEDDING_BACKEND='local'
$env:BANK_AGENT_OFFLINE='1'
python app/banking_agent.py
```

### **8.3 另一台电脑完全断网启动**

- **推荐交付形态**：完整Docker镜像包，包含代码、Python、CPU依赖、模型、分词器和知识来源。目标是Linux/amd64容器；另一台Windows/Linux电脑须已安装并能运行兼容Docker及Compose。Docker安装器本身不在包内，ARM/Mac或其他平台没有本批实机验收证据。
- **维护者构建**：联网准备资产后，构建阶段也以`--network none`安装本地哈希锁定wheel，禁止镜像拉取。该命令生成完整目录，包括`images.tar`、`compose.json`、`START_HERE.md`、验证脚本和SHA256清单。`--local-rag`缺模型或依赖必须失败，不能产出冒充语义版的关键词包。

```powershell
python scripts/offline_release.py build .qa-tools/offline-releases/local-rag-demo --local-rag
python scripts/offline_release.py verify .qa-tools/offline-releases/local-rag-demo
python scripts/offline_qa.py .qa-tools/offline-releases/local-rag-demo
```

- **目标电脑操作**：复制生成的整个交付目录；核对`SHA256SUMS`中每一个文件后执行以下命令。未安装Python也能运行；无需pip、模型仓库、Docker仓库或LLM密钥。`images.tar`已包含所需应用镜像和沙箱基础镜像；若目标已有不同版本的同名基础镜像，先按随包说明保留原标签或使用独立引擎。

```powershell
docker load --input images.tar
docker compose -p bank-agent-offline -f compose.json up -d --no-build --pull never
```

- **使用**：访问`http://127.0.0.1:8090`，输入`知识问答：把钱整笔放着到期再拿是哪种储蓄`。为兼容本机Docker29内部网络端口不映射的问题，Compose用同一个镜像运行Agent和轻量Web转发两个容器：Agent仅接内部网络、不发布端口；转发容器通过前端桥接网向127.0.0.1发布端口，目标固定为`banking-agent:8090`，不支持任意目的地或CONNECT。转发层没有模型实例、密钥或数据卷。Agent仍无外网路由；不修改Docker全局配置。两容器使用只读镜像，Agent使用独立持久卷；重建容器后业务演示数据保留，内存向量索引在本机重新建立。知识来源仍受审核有效期限制，过期资料需维护者更新后重新交付，不能为演示无限延长或绕过到期检查。
- **GitHub分发**：仅下载源码ZIP不能保证离线语义检索。应额外分发本节的完整镜像包（后续可作为GitHub Release附件），或在有网时准备全部资产。当前没有创建Release、上传文件或推送Git。

### **8.4 本批验收记录**

| 验收项 | 实际结果与证据 |
| --- | --- |
| 启用本地模型的全量回归 | **636通过、0跳过、0 ResourceWarning**，浏览器及恢复演练启用，日常数据库哈希未变；`data/verification/p03-offline-20261023/full-suite-final.json` |
| Docker离线交付与主链 | **45项检查PASS**，引擎29.9.2；`data/verification/t14-20261023-233815-202679/report.json`，含8个HTTP语义/拒答样例、审计、转账确认、接管和重建持久化 |
| 无外网的真实推理 | `--network none`加禁止socket.connect探针：512维归一化、新问题、空索引/重建、家庭可见性、跨用户缓存和撤权9项PASS；不是替身模型 |
| 完整资产与代码一致性 | 包内185个源文件/模型/依赖资产与当前工作副本逐一SHA256一致，应用镜像`sha256:ca3ad4a2bbc1c3d39fc2f47ec1e9dd6d2359b7e2fa83d5ec044d16b1ceb22e84` |
| 模拟搬家 | 将完整包复制到含中文和空格的新目录，逐文件校验、镜像导入、无宿主源码/模型挂载后运行通过；`migration-copy.json`。**尚无物理第二台电脑断网验收证据** |
| 研究建议配对评测 | 54题：weighted首条43/44、top3为44/44，10个未覆盖题均拒答；RRF首条42/44，差值bootstrap95%区间[-0.0682,0]。单人构造的同源回归，不宣称泛化或显著优胜 |

- **交付目录**：`.qa-tools/offline-releases/p03-local-rag-20261023-final/`，约314MiB，复制全部8个文件。入口`START_HERE.md`；完整汇总见`data/verification/p03-offline-20261023/report.json`。
- 本批只修改批准的14份源码/测试/配置/文档，另生成模型、依赖与QA交付资产；未改团队领域契约、五个删除文件、日常数据库、Git HEAD或远端。`docs/验收摘要.json`仍是此前批次的历史摘要，本次以本节和新报告为准。
- 模型/镜像不是生产银行、真实IM或生产认证的完成证明；本节记录的是2026-10-23初始批次；独立重排及本地压力测试的后续进展见第9节，全套跨文档冲突治理仍无完成证据。


### **8.5 两份调研的采纳与取舍**

| 调研建议与证据 | 本批处理 | 原因与验证 |
| --- | --- | --- |
| R2 §3.1，R3 §10：混合召回及RRF | 保留词面＋本地向量；新增可选RRF(k=60)，与weighted配对对比 | 避免将另一项目的默认值直接当本库最优；54题同源回归中weighted首条43/44，RRF42/44，差异置信区间包含0，不宣称显著优胜 |
| R3 §1.2：结构感知、重叠与原始定位 | 句末/换行优先，最长320字符、重叠32字符，保留偏移、行号和哈希 | 针对当前JSON知识卡；还不是通用PDF/DOCX解析、层级章节父子块或OCR |
| R3 §3.9：模型版本隔离、归一化 | 查询和文档均L2归一化；缓存身份含模型清单哈希，换模型重建文档向量 | 单元测试验证清单版本改变后身份变化；不混用M3与本地small向量 |
| R2 §4.1、R3 §11：截断风险 | 真实token长度检查；超长拒绝，不静默丢弃后文 | 512 token的模型上限与字符切片上限分别约束；过长句段不会被悄悄当完整材料 |
| R3 §14.5-14.6：扩大评测、配对比较与失败留存 | 冻结54题（44条定位/口语问法＋10条未覆盖题），输出Hit@1/3、MRR@3、NDCG@3、拒答率和1000次配对bootstrap | 题集内置于`scripts/offline_qa.py`，结果带题集和资料SHA256。单人按现有来源编写，缺少双人标注、真实用户题和跨文档/冲突题，不能称独立泛化验收 |
| R2 §3.2/§4.4-4.5：先过滤、精确引用与拒答 | 保留并扩展本地语义分支：先ACL/到期过滤，返回前撤权复查，原文精确引用，无依据拒答 | 家庭隔离、未知结果与银行操作权限不由检索模型裁定 |
| R2 §4.1、R3 §11：独立cross-encoder重排 | 初始批次未实现；2026-10-24已增量接入，见第9节 | 保留历史取舍；新增模型已获批准，按相同题集比较，保留失败样例 |
| R2 的HyDE/查询改写/GraphRAG/多跳、R3 的MySQL/Qdrant/知识治理 | 未纳入本批 | 前者需额外本地LLM/图与治理需求；后者是参考项目栈。当前11张卡和既有标准库主链可用精确扫描，不进行框架/数据库迁移 |

- 研究正文中“14人团队”“禁止SQLite”“FastAPI/MySQL”等属于其原项目，文件首页第3行也明确为历史研究记录，不替代本项目五人分工、既有技术边界和用户批准范围。
- 54题中仍保留一项首条标签不匹配：问本地客服能否把未知结果改成功，weighted首条返回`unknown_result`，预期标签是`human_handoff`；相关限制没有被改写为成功。保留该失败供后续人工仲裁/难负例评测，没有为这道题增加硬编码规则。


## **9. 独立重排、扩展评测与本地压测（2026-10-24）**

### **9.1 实现与运行边界**

- **独立重排**：`app/local_reranker.py`使用`BAAI/bge-reranker-base`的ONNX INT8 cross-encoder，转换仓库固定到`280bcc27a84e0b898c251e06fddb25171bd9b101`。模型、分词器、说明及MIT许可证均由`models/reranker/manifest.json`校验。设置`RAG_USE_RERANKER=1`；模型位置由`RAG_RERANKER_DIR`指定。没有模型时不联网补下载。
- **增量接入**：`KnowledgeService.search`先执行已有ACL、有效期、混合召回，再对最多8个候选做重排。输入总长超过512 token拒绝，不静默截断；分批大小2，CPU两线程。分数小于0.03的候选不进入答复；最高分低于0.20时保留融合排序，避免在低分区放大重排噪声。阈值基于公开回归校准，分数不是正确概率，不能宣称独立泛化。模型忙、损坏、输出无效时保留融合结果，并在`retrieval.rerank`记录未应用原因。单实例重排并发上限1，降级请求不能计作完整重排吞吐。
- **防幻觉与权限**：知识仍只生成有出处的原文摘录。请求的金融事实类别不在候选材料中、或请求实时利率等无实时来源信息时拒答。ACL先于向量及重排，答复前再次核验；模型不决定三色权限，不生成资金事实。没有引入向量数据库、Agent ABI或副作用执行旁路。

### **9.2 冻结题集与真人评测**

- **120题回归**：`eval/rag_eval_v2.json`包含原54题及新增66题；来源与题集均绑定SHA256。新增组已经在开发过程中暴露，标签虽沿用holdout，**不能再称独立未见测试集**。全部为合成、按来源构造的问题，不能冒充真人问法。
- **实际结果**：`data/verification/p03p04-20261024/evaluation-final.json`中，lexical为110/120、hybrid为113/120、独立重排为117/120。重排正例Hit@1=85/88（96.59%）、Hit@3=87/88（98.86%），32个负例全部按规则拒答。3个失败保留为`hold-070/086/087`，涉及目标来源未召回或首位来源标签不同；不为单题加问法硬编码。
- **真人评测工具**：`scripts/rag_evaluation.py`可导入`eval/rag_user_study_template.json`。要求匿名参与者、明确同意、两位不同标注人和来源标签；过滤隐私和伪造样本标记。模板目前为空，状态**PENDING**；工具验证格式不能独立证明参与者身份。后续由五人各提供5—10个脱敏自然问法，再人工核对回答。
- **复现**：使用已安装私有依赖的Python运行`python scripts/rag_evaluation.py --output data/verification/new-eval.json --user-study eval/rag_user_study_template.json`。默认比较关键词、混合、重排三种模式，失败、耗时、Hit@1/3、MRR@3、NDCG@3及拒答率全部保留。

### **9.3 本地负载与结论范围**

- **阈值先记录**：`data/verification/p03p04-20261024/protocol.json`在实测前固定目标：单用户P95≤10秒、有效重排率≥95%、错误0、重复扣款0。它们是本机演示工程目标，**不是赛题L1规定，也不是生产SLO**。
- **测试范围**：`scripts/rag_load_test.py`对本地CPU模型进行1/2/4/8并发阶梯（各20秒）、600秒持续负载、冷启动、索引清空重建；规模实际为64/300/1100片段。P50/P95/P99、吞吐、错误、超时、忙降级、有效重排比例、CPU和进程内存均保留。只允许本机回环连接。
- **主链正确性**：临时Mock数据库中10笔各1分交易，每笔4次并发确认，核对总扣款10分、重复扣款0与审计完整性；另走实际HTTP知识问答，检查限流/错误与余额未变。最终报告位置`data/verification/p03p04-20261024/load-final.json`；`load-report.json`保留排序修正前记录。
- **未测范围**：真实银行、模型付费API、第三方IM/IAM的负载测试与物理第二台电脑均未测。高并发时融合降级的高吞吐不能冒充神经重排容量；容量不足时应限制演示并发，而非取消权限与审计。

| 首次实测（排序修正前；最终结果见第9.5节） | 结果 |
| --- | --- |
| 单用户20秒 | 162次，P50/P95/P99约68/329/358ms，完整重排8.03次/秒，0错误、0降级 |
| 单用户持续600秒 | 4442次，P50/P95/P99约88/353/473ms，完整重排7.40次/秒，0错误/超时/降级，进程内存约1.04GiB |
| 并发2/4/8 | 忙降级比例约94.83%/96.48%/96.80%；完整重排容量仍约6—8次/秒，不能宣传为全部完整重排的121—203次/秒 |
| 1100片段规模 | 首次索引约11.36秒、清空重建约10.52秒；没有宣称大库冷启动也满足单用户热请求P95目标 |
| Mock与HTTP | 40次确认只产生10分扣款，重复扣款0；24次HTTP问答均200，余额未变、审计有效；本地预设门槛通过 |

### **9.4 P03/P04联合离线发行**

- **构建**：`python scripts/offline_release.py build .qa-tools/offline-releases/p03p04-20261024-final-r5 --local-rag --local-reranker --local-ocr`。三个功能在manifest中分别声明；全部依赖从本地哈希锁定wheel安装，构建不拉镜像、不访问网络。输出目录必须不存在，不能覆盖已发行目录。
- **验收**：`python scripts/offline_qa.py <完整发行目录> --ocr-image data/verification/p03p04-20261024/ocr-synthetic.png`。测试图片仅包含虚构演示文字；不传真实票据、账户或个人信息。验收会核对包内源码/资产、network-none真实推理、HTTP主链、审计和持久化。
- **最新记录**：本批全量回归、负载与最终镜像的汇总以`data/verification/p03p04-20261024/report.json`及`docs/验收摘要.json`为准；旧节测试数字仅是历史快照，不可替代当前验收。


### **9.5 迁移发现回归与最终修正**

- **真实发现**：首次联合镜像迁移在旧HTTP样例“想停下来核实骗局时的保护措施”失败，新重排器给正确资料0.034分，原0.05门槛误拒；另一旧确认卡问法的低分区排序也偏离原检索。失败记录保留在`data/verification/t14-20261024-092247-913092/report.json`，该镜像批次不能当最终通过。
- **统一处理**：用120题及旧验收问法查看真实分数，统一拒答门槛改为0.03；最高分小于0.20时仅执行重排相关度筛选并保留原融合顺序。没有按问题字符串分支，没有改测试预期；证据`threshold-diagnostic.json`和新增单元回归。`retrieval.rerank.ordering`区分`cross_encoder/fusion_low_score`，有效推理不等于每次改变顺序。
- **结果边界**：最终合成回归117/120，仍有3题目标来源首位不符/拒答；不把调参后的题集称未见集。最终完整测试、重新运行的600秒负载及r5镜像迁移结果见本批汇总`data/verification/p03p04-20261024/report.json`。

### **9.6 最终验收快照**

| 最终验收 | 当前记录 |
| --- | --- |
| 完整回归 | 668通过，0跳过、0 ResourceWarning，日常数据库不变；`full-suite.json` |
| 持续负载 | 600.22秒、3886次；P50/P95/P99=89.5/484.6/513.0ms；有效本地cross-encoder评分率100.0%；错误/超时/降级均0，完整记录`load-final.json` |
| 镜像与迁移 | 53项检查通过，246项源码/资产与当前工作副本SHA256一致；`data/verification/t14-20261024-094457-003033/report.json` |
| 最终离线发行 | `.qa-tools/offline-releases/p03p04-20261024-final-r5/`，完整8文件、images.tar约1.13GiB；含中文空格目录复验，未使用宿主源码或模型挂载 |
| 保护边界 | 批准29份文件；Git HEAD、领域Agent ABI、日常数据库及五个删除文件均保持原状；未commit/push |

- 最终记录位于`data/verification/p03p04-20261024/`；真实用户问法、真实OCR样本、物理第二台电脑、真实外部服务及其压测仍未验收。


## **10. 段落修订与同题回归（2026-10-31，当前口径）**

### **10.1 增量实现与规则依据**

- **保留原文与来源**：仅为金额阈值、人工接管、知识边界三张项目卡增加解释段；不改中国银行产品摘录。知识说明不能改变额度，依据仍为服务端PolicyEngine；结单不能改变交易，依据为handoff_queue/live_support只处理工单消息；引用不增加模型权限，依据为KnowledgeService只返回有出处的原文。
- **按段落分块**：app/knowledge_service.py的_chunk_spans按显式空行划分段落，保留Unicode/CRLF精确偏移；段内沿用最大320字符、32字符重叠和句末边界。文档级审核、注入隔离、有效期及ACL先处理，所有新片段继承原权限；没有按评测问法特判、修改阈值或增加依赖。
- **版本可复现**：eval/rag_eval_v2.json保持不动，旧资料逐字冻结在eval/knowledge_p03_v2.json。eval/rag_eval_v3.json绑定新资料SHA，120个问题、ID、分组和标签与v2完全一致；scripts/rag_evaluation.py新增--source，资料不匹配在初始化模型前拒绝。默认题集切换v3，不允许悄悄更新旧分数。

### **10.2 实测结果与局限**

| 模式 | P03旧资料 | 本次资料 | 新增退步 | 仍未通过 |
| --- | --- | --- | --- | --- |
| 关键词 | 110/120 | 113/120 | 0 | 7题；详见完整报告 |
| 本地向量混合 | 113/120 | 116/120 | 0 | cal-031、hold-060、hold-069、hold-102 |
| 本地向量＋独立重排 | 117/120 | 120/120 | 0 | 本合成题集无失败 |

- **指标**：重排88个正例Hit@1/3、MRR/NDCG@3均为1，32个负例全部按规则拒答；混合模式仍有一个无覆盖问题未拒答（hold-102），因此重排忙/缺失后的降级质量仍有边界，不能用120/120覆盖该事实。关键词/混合的错误没有改标签消除。
- **真实运行**：本机CPU实际生成查询向量并调用独立重排；评测期间socket.connect被禁止，无外发资料、无付费调用。旧资料在同机重现117/120及原三个失败。候选改写造成退步的记录全部保留在data/verification/finalize-20261031/rag-analysis/，选中r6，其余不作为成品。
- **验收记录**：新题集三模式为data/verification/finalize-20261031/batch03/evaluation-final.json；旧资料复现为evaluation-v2-reproduced.json；五项新增测试先失败后通过，覆盖段落/长文本/偏移/权限/版本和冻结标签。完整套件最终状态见docs/assets/RAG修订验收.json，不将缺浏览器配置的带跳过运行当通过。
- **真人与压测**：真人模板依旧为空，PENDING；本成绩是开发中暴露的合成回归，非独立未见集、真实用户准确率或生产质量证明。P10负载成绩保留原冻结版本；本次未将旧压测改名为新版本压测。

### **10.3 复现命令**

```bash
python scripts/rag_evaluation.py --dataset eval/rag_eval_v3.json --output data/verification/rag-v3-new.json
python scripts/rag_evaluation.py --dataset eval/rag_eval_v2.json --source eval/knowledge_p03_v2.json --modes reranked --output data/verification/rag-v2-reproduced-new.json
```

- 需使用已批准且已安装本地CPU可选依赖和模型的环境；命令不会联网补下载。最终离线包须在本次修订后重建，旧P11包仍对应旧资料，不能直接当作v3成绩的运行包。

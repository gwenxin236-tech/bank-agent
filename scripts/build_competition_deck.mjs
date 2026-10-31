// Local authoring only. Reuses the supplied Artifact Tool and current verified recording.
import fs from 'node:fs/promises';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
const root=path.resolve(path.dirname(fileURLToPath(import.meta.url)),'..');
const skill=process.env.T16_PRESENTATION_SKILL,modules=process.env.RUNTIME_NODE_MODULES,python=process.env.RUNTIME_PYTHON;
if(!skill||!modules||!python)throw Error('Use the bundled presentation skill, Node modules and Python');
const require=createRequire(path.join(modules,'package.json'));
const {Presentation,PresentationFile,FileBlob}=await import(pathToFileURL(require.resolve('@oai/artifact-tool')).href);
const {finalizePresentation}=await import(pathToFileURL(path.join(skill,'container_tools/artifact_tool_utils.mjs')).href);
const record=path.resolve(process.env.T16_RECORDS_DIR||path.join(root,'data/verification/finalize-20261031/batch02/recording-final'));
if(!record.startsWith(path.join(root,'data','verification')+path.sep))throw Error('Owned record directory required');
const work=path.join(record,'deck-'+(process.env.DECK_REVISION||'r1'));await fs.mkdir(work,{recursive:true});
const recording=JSON.parse(await fs.readFile(path.join(record,'recording/report.json'),'utf8'));
if(recording.status!=='PASS'||!recording.video||recording.video.duration_seconds>300)throw Error('Current real recording must pass first');
const evidence=JSON.parse(await fs.readFile(path.join(root,'docs/assets/最终交付验收.json'),'utf8'));
if(evidence.regression.current_full_windows?.tests!==780||!['PASS','PENDING_DOCKER_ENGINE'].includes(evidence.offline.status))throw Error('Verified full evidence and explicit offline status required');
const runtimePassed=evidence.offline.status==='PASS';
if(runtimePassed&&evidence.regression.current_core_linux?.status!=='PASS')throw Error('Current core evidence must pass before claiming runtime passed');
const reference=process.env.DECK_REFERENCE_FILE||null;
const referenceSha='286472fec9d87a3358493ce97daf3dc825cfdaa44e1ab4d43cbff9fbedc29a43';
const FONT='Microsoft YaHei';const {GlobalFonts}=require('@napi-rs/canvas');if(!GlobalFonts.families.some(f=>f.family===FONT))throw Error('Required Chinese font missing');
const p=Presentation.create({slideSize:{width:1280,height:720}});
const C={blue:'#1E4BAE',ink:'#101820',muted:'#4E6075',pale:'#ECF2FB',line:'#C9D5E6',green:'#146B27',amber:'#9B630B',red:'#A22B32',white:'#FFFFFF'};
let s;const timings=[15,25,30,30,25,30,30,25,30,20,10,20];
function rect(x,y,w,h,fill=C.white,line='none'){return s.shapes.add({geometry:'rect',position:{left:x,top:y,width:w,height:h},fill,line:{fill:line,width:line==='none'?0:1}});}
function text(value,x,y,w,h,size=25,color=C.ink,bold=false){const q=rect(x,y,w,h,'none');q.text=value;q.text.style={typeface:FONT,fontSize:size,color,bold,insets:0,verticalAlignment:'top',wrap:'square',autoFit:'none'};return q;}
function line(x,y,w,color=C.line){rect(x,y,w,1,color);}
function arrow(x,y,w=50){text('→',x,y,w,42,32,C.blue,true);}
function node(title,body,x,y,w=245,h=100){rect(x,y,w,h,C.pale,C.line);text(title,x+15,y+13,w-30,34,25,C.ink,true);text(body,x+15,y+51,w-30,h-55,18,C.muted);}
function slide(title,section,notes,sources){s=p.slides.add();s.background.fill=C.white;rect(0,0,1280,44,C.blue);text('澜银行 AI 智能体',44,10,600,29,18,C.white,true);text(section,850,10,380,29,18,C.white);text(title,58,75,1160,65,38,C.ink,true);line(58,149,1164);text('竞赛原型 · 本地/虚构资金 · 2026-10-31',58,685,950,23,13,C.muted);text(String(p.slides.items.length).padStart(2,'0'),1178,685,50,23,14,C.muted);
 s.speakerNotes.text=`口述预算${timings[p.slides.items.length-1]}秒，尚需真人排练。\n${notes}\n证据：\n${sources.join('\n')}\n默认Mock与可选Fineract/身份模式分开，不把本地或自动验收称为生产/真人认证。`;return s;}
async function image(name,x,y,w,h,alt){const data=await fs.readFile(path.join(record,'recording',name));const iw=data.readUInt32BE(16),ih=data.readUInt32BE(20);const scale=Math.min(w/iw,h/ih);s.images.add({blob:new Uint8Array(data),contentType:'image/png',alt,position:{left:x+(w-iw*scale)/2,top:y+(h-ih*scale)/2,width:iw*scale,height:ih*scale}});}
function small(value,y=630,color=C.muted){text(value,58,y,1164,40,20,color);}
slide('澜银行 AI 智能体','银行业务 · 安全Agent','介绍定位和已验证范围；不套用参考作品团队、机构标识和指标。',['docs/技术文档.md:1','docs/assets/P11发行验收.json']);
text('自然语言办理银行业务',64,235,1120,90,62,C.ink,true);text('六域协作，平台统一授权与执行',69,353,1120,57,34,C.blue,true);
line(69,451,1100,C.blue);text('本地知识检索与独立重排 · OCR核对草稿 · 受控动态DAG',69,490,1100,50,27);text('774项自动回归  /  三种同版离线运行配置',69,560,1100,45,27,C.green,true);
slide('自然语言请求需要四道可信边界','01 需求与业务边界','依据原始赛题，不称独立用户调研或效率提升。',['人工智能赛道原始要求.docx P11-P23','app/banking_agent.py']);
const needs=[['要素不完整','“给妈妈转账”先补齐金额和可信账户'],['业务有副作用','理解意图不等于授权，用户核对原计划'],['结果会不确定','超时查原请求，不能重新转一次或补写成功'],['任务可能中断','持久化节点状态，回退只执行已声明补偿']];
needs.forEach(([a,b],i)=>{const y=180+i*108;text(String(i+1).padStart(2,'0'),65,y,80,55,40,C.blue,true);text(a,170,y,1020,38,29,C.ink,true);text(b,170,y+43,1020,45,24,C.muted);if(i<3)line(170,y+95,1030);});
slide('六域只读分析，平台统一执行','02 整体方案','六域为项目目标，现有基础实现不代表成员增强版已交付；来源冲突不投票授权。',['app/domain_agent_registry.py','app/supervisor.py','app/domain_agent_contract.py']);
node('输入与理解','文字 / OCR草稿 / 可选语音',58,190,350,95);node('可信业务计划','规则 / 受限模型，实体由平台核验',465,190,350,95);node('安全执行链','Policy → 确认/认证 → DAG → 工具',872,190,350,95);arrow(417,215);arrow(824,215);
const domains=['支付与交易','反诈风控','财务健康','家庭授权','订阅治理','卡片管理'];domains.forEach((a,i)=>{const x=58+(i%3)*407,y=335+Math.floor(i/3)*94;rect(x,y,350,72,C.pale);text(a,x+18,y+20,315,38,26,C.ink,true);});
text('Supervisor：上下文绑定 → 平台快照比对 → 冲突保留 → 原执行链',58,553,1164,48,29,C.blue,true);small('领域Finding/Proposal只能提供事实与建议，不能决定权限或直接调用资金工具');
slide('本地RAG：先核验阅读权，再给出引用','03 关键机制','512维BGE向量与独立cross-encoder；120题是合成集，修订后120/120，混合降级仍4失败，不称独立未见或真人效果。',['app/knowledge_service.py','app/local_embeddings.py','app/local_reranker.py','docs/assets/RAG修订验收.json']);
node('权限过滤','owner / readers / 显式家庭共享',58,185,350,92);node('本机混合召回','词项 + 512维向量，可禁网重建',465,185,350,92);node('独立重排与引用','有限候选/低分拒答，答复前复核',872,185,350,92);arrow(417,207);arrow(824,207);
await image('11-rag.png',670,300,548,320,'当前本机知识问答及引用界面');
[['关键词',113],['混合检索',116],['独立重排',120]].forEach(([label,n],i)=>{const y=342+i*86;text(label,60,y,170,34,25,C.ink,true);rect(230,y+5,330*n/120,21,i===2?C.green:C.blue);text(`${n}/120`,568,y,112,34,23,C.ink,true);});
small('合成题集保持不变，三模式均无新增退步；混合仍4失败，真人待测');
slide('OCR与语音先核对，只进入普通草稿','03 关键机制','图片/音频数据不直接授权交易；固定短句真实麦克风/ASR为此前单独验收，不称本片真实录音。',['app/ocr_service.py','app/speech_adapter.py','docs/P05音频证据与生命周期.md']);
await image('12-ocr.png',58,174,590,440,'本地OCR位置、置信度和人工核对界面');
text('OCR：本机识别 → 字段核对 → 草稿',688,190,525,82,28,C.ink,true);text('位置与置信度可见\n低置信度/冲突须确认\n金额在平台采用整数分',688,300,525,150,25,C.muted);
text('语音：试听与同意 → 转写核对',688,465,525,45,27,C.ink,true);text('区分上传/PCM/供应商字节hash\n供应商逐条删除回执尚未取得',688,526,525,92,24,C.muted);small('识别与核对都不办理业务；最终仍须原权限、确认、认证及审计');
slide('动态拓扑可变，业务权限保持平台控制','03 关键机制','原步骤全部保留，额外只读工具有白名单；采纳撤旧卡，新确认摘要绑定图。当前协作式单线程，无任意跨业务/生产并行承诺。',['app/dag_planner.py','app/banking_agent.py dynamic_dag','tests/test_dynamic_dag.py','docs/assets/DAG真实模型验收.json']);
node('原可信计划','必须业务步骤 / 可信实体',58,185,350,93);node('结构提案','本地或可选模型，无金额/授权',465,185,350,93);node('监督审核','无环 / 白名单 / 节点预算',872,185,350,93);arrow(417,209);arrow(824,209);
await image('13-dag.png',58,299,695,315,'真实预览与采纳动态任务图');text('采纳 ≠ 执行',800,338,410,58,35,C.ink,true);text('撤销旧确认与旧认证\n持久化图摘要\n新卡重新确认\n原DagExecutor执行',800,425,410,160,26,C.muted);small('真实模型2次：首次结构拒绝、第二次贯通；任意跨业务/生产并行仍未完成');
slide('三色权限、事实回执与审计贯通','03 安全与认证','默认红色为演示OTP；可选密码＋TOTP已自动验，尚非生产身份/硬件认证。只有工具证实终态才报成功。',['config/policy.json','app/identity_service.py','app/banking_agent.py PolicyEngine/AuditStore']);
const colors=[C.green,C.amber,C.red];const levels=[['绿色','余额/流水等允许的纯查询','可自动执行'],['黄色','小额转账、订阅查询/取消','先明确确认原计划'],['红色','越过日累计阈值、挂失等','强认证后仍须再次确认']];
levels.forEach(([a,b,c],i)=>{const x=58+i*407;rect(x,183,350,8,colors[i]);text(a,x,218,350,43,31,C.ink,true);text(b,x,287,350,95,25,C.muted);text(c,x,400,350,70,26,C.ink,true);});
line(58,502,1164);text('模型不能授权  /  UNKNOWN不补写成功  /  副作用不自动重试',58,534,1164,51,28,C.blue,true);small('trace哈希链可核验，工单结单不改交易；工程自评不等于生产安全认证');
slide('开源核心与自建坐席分别验证','03 接入与协作','Fineract为开源本地虚构账户；自建坐席自动两上下文不是真人验收。持牌银行和官方IM待外部环境。',['app/fineract_transport.py','app/live_support.py','docs/assets/P11发行验收.json']);
text('Fineract · P11已验25项',58,183,570,45,30,C.ink,true);text('实际扣款与入账\n原请求查询与对账\n原生UNDO与回调重查\n重启不重扣、不重新种子初始化',58,265,550,230,26,C.muted);
await image('14-support-operator.png',660,182,558,360,'本机自建坐席实际消息收发工作台');text('自建消息收发 / 原队列与角色隔离',660,565,558,50,27,C.ink,true);small('不是持牌银行或官方客服；真人双端此前由用户暂缓，自动验收不替代真人');
slide('回归与冻结压测分别给出证据','04 验证结果','780是当前全套回归，非功能完成率。P10数据属于冻结4CPU/4GiB本地镜像与闭环负载；P11未把旧数据改名重新压测。',['data/verification/finalize-20261031/batch05/full-final.json','docs/assets/P10性能压测验收.json','docs/assets/最终交付验收.json']);
const values=[['验证组','实测结果','解释边界'],['本轮完整回归','780项通过','0跳过 / 0 ResourceWarning'],[runtimePassed?'本轮三配置迁移':'P11历史三配置迁移',runtimePassed?'53 / 32 / 25项通过':'53 / 32 / 25项通过',runtimePassed?'本轮同版复制验收':'旧版成绩；新镜像待验'],['P10持续混合负载','600次 / 302.19秒','4CPU/4GiB，闭环Mock'],['P10延迟 / 资金正确性','P95 3198ms / 重复扣款0','非真实银行容量或生产SLO']];
const table=s.tables.add({rows:5,columns:3,left:58,top:183,width:1164,height:340,columnWidths:[360,365,439],values});
table.borders.assign({style:'solid',fill:C.line,width:1});for(let r=0;r<5;r++)for(let c=0;c<3;c++){const cell=table.getCell(r,c);cell.fill=r===0?C.pale:C.white;cell.text.style={typeface:FONT,fontSize:23,bold:r===0,color:C.ink,insets:12,verticalAlignment:'top'};}
text('P11云模型6次；本轮模型DAG另2次',58,560,1164,44,29,C.ink,true);small('P11为1721tokens；本轮989tokens，首次图非法被拒；均非完整云端压测');
slide('完整离线交付：复制、校验、导入、运行','04 交付与边界','本地模型与依赖在镜像内；接收方需已有兼容Docker Linux/amd64引擎。源码ZIP和运行包分开，校验不是签名。物理第二台仍待验。',['scripts/source_release.py','scripts/offline_release.py','docs/assets/最终交付验收.json']);
['复制完整包','核对SHA清单','导入镜像','离线启动'].forEach((a,i)=>{node(a,i===0?'源码与运行包分别交接':i===1?'完整性校验，非签名':i===2?'模型、依赖随镜像导入':'不构建、不拉取，仅本机',58+i*302,195,255,105);if(i<3)arrow(321+i*302,222,36);});
const modes=[['默认离线','Mock + 本地RAG/重排/OCR'],['自建身份','密码＋TOTP，无预置种子'],['独立开源核心','Fineract + PostgreSQL，虚构资金']];modes.forEach(([a,b],i)=>{const y=351+i*77;text(a,63,y,260,40,27,C.ink,true);text(b,360,y,850,42,26,C.muted);line(58,y+58,1164);});small(runtimePassed?'目标机需已有兼容Docker；物理异机和托管CI仍待验':'新镜像/三配置已导出并校验，Docker Secrets Engine故障，容器复验待完成');
slide('真实界面演示','04 演示','在此页另播当前MP4。视频为自动浏览器的真实本机界面与模型，不伪装真人；播放不计入本页10秒口述预算。',['scripts/record_competition_demo.mjs',path.relative(root,path.join(record,'recording/report.json'))]);
await image('03-yellow.png',58,174,740,450,'本次实录的原计划确认卡');text('按顺序看五件事',840,192,360,45,30,C.ink,true);text('先补齐再确认\n认证不等于扣款\nRAG有引用\nOCR只填草稿\n自建坐席不改账',840,274,360,255,28,C.muted);small(`本次视频 ${recording.video.duration_seconds.toFixed(2)}秒；外部API调用0，临时Mock数据库`);
slide('当前成果与后续边界','04 总结','不把未完成项改为通过：人工作业/外部环境/任意跨业务DAG和生产认证分别排期。当前交付以最终发行索引和收件记录为准。',['docs/验收摘要.json known_unverified','docs/五人团队Agent接入契约.md']);
const conclusions=[['已经验收','六域与平台权限链\n本地RAG/重排/OCR\n动态拓扑、自建身份/消息\nP11历史三模式迁移'],['仍待真人','自建双端人工操作\n独立TOTP与真实读屏\n真实问法/图片/语音\n物理第二台与实际汇报计时'],['仍待外部与扩展','持牌银行与官方IM\n生产认证/硬件证明\n任意跨业务/生产并行\n完整云端与生产容量']];
conclusions.forEach(([a,b],i)=>{const x=58+i*407;text(a,x,205,350,48,31,C.ink,true);rect(x,266,350,3,i===0?C.green:C.blue);text(b,x,300,350,240,26,C.muted);});small('让请求更易表达，让权限、过程与事实都可核对',624,C.blue);
const candidate=path.join(work,'candidate.pptx');await(await PresentationFile.exportPptx(p)).save(candidate);
const final=path.join(record,'output-'+(process.env.DECK_REVISION||'r1'),'final.pptx');await fs.mkdir(path.dirname(final),{recursive:true});
await finalizePresentation({workspaceDir:root,candidatePath:candidate,finalPath:final,pythonExecutable:python,integrityValidatorPath:path.join(skill,'container_tools/inspect_presentation_package_integrity.py'),layoutValidatorPath:path.join(skill,'container_tools/inspect_presentation_layout_geometry.py'),layoutArgs:['--expected-slide-size-emu','12192000,6858000','--validate-bullet-geometry','--validate-heading-fit','--require-native-table-slide','9'],explicitTotalSlideCount:12,requiredNativeTableOwnerSlides:[9],requiredNativeChartOwnerSlides:[],fontPolicy:{basis:'design',families:[FONT]},verifyArtifactToolImport:true,receiptPath:path.join(work,'validation.json')});
const finalDeck=await PresentationFile.importPptx(await FileBlob.load(final));for(let i=0;i<finalDeck.slides.items.length;i++){const png=await finalDeck.export({slide:finalDeck.slides.items[i],format:'png',scale:1});await fs.writeFile(path.join(work,`slide-${String(i+1).padStart(2,'0')}.png`),new Uint8Array(await png.arrayBuffer()));}
const talk=timings.reduce((a,b)=>a+b,0),budget=talk+recording.video.duration_seconds;if(budget>600)throw Error('Planned talk plus measured video exceeds 10 minutes');
await fs.writeFile(path.join(work,'timing.json'),JSON.stringify({slides:timings,planned_talk_seconds:talk,actual_video_seconds:recording.video.duration_seconds,combined_planned_seconds:budget,human_rehearsal_measured:false},null,2));
await fs.writeFile(path.join(work,'reference.json'),JSON.stringify({source:reference?'explicit local reference file':'user-provided reference previously reviewed',sha256:reference?createHash('sha256').update(await fs.readFile(reference)).digest('hex'):referenceSha,usage:'visual inspiration: blue header/white technical pages, section organization, workflow/algorithm evidence; no source team/logo/metrics copied',reference_pages_rendered:24},null,2));
await fs.copyFile(final,path.join(root,'submission/澜银行AI智能体_答辩_v2.pptx'));console.log('DECK COMPLETE',final);

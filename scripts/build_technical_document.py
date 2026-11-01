"""Authoring only: render the technical Markdown using optional ReportLab.

Not imported by the bank application. Run with the supplied document runtime.
Source citations are resolved against actual files; no private/state payloads read.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
from pathlib import Path
import re
import keyword
import io
import token
import tokenize

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (BaseDocTemplate, CondPageBreak, Flowable, Frame, KeepTogether,
                               PageBreak, PageTemplate, Paragraph, Spacer,
                               Table, TableStyle, Image)
from reportlab.platypus.tableofcontents import TableOfContents
from technical_figures import TechnicalFigure
from source_release import material_evidence

ROOT = Path(__file__).resolve().parents[1]
INK = colors.black
GREEN = colors.HexColor('#146b27')
LIGHT = colors.HexColor('#eaf5ef')
LINE = colors.HexColor('#c9d8d2')

# Marker matching fails on missing/ambiguous references instead of inventing lines.
SOURCES = {
 'S01': ('人工智能赛道原始要求.docx', None, '正文P11-P23、P25-P29；权限表T2R2-R4'),
 'S02': ('config/policy.json', '"green":', '三色分组、金额阈值和认证配置'),
 'S03': ('app/banking_agent.py', 'class PolicyEngine:', '平台权限与工具前复核'),
 'S04': ('app/banking_agent.py', '    def confirm(self, plan_id:', '原计划确认与执行授权'),
 'S05': ('app/banking_agent.py', '    def verify(self, plan_id:', '绑定计划的演示强验证'),
 'S06': ('app/banking_agent.py', 'def present_result(', '工具事实与用户结果视图'),
 'S07': ('app/banking_agent.py', 'class InjectionGuard:', '规则注入检查'),
 'S08': ('app/banking_agent.py', 'class AuditStore:', '审计存储、哈希链与身份关联'),
 'S09': ('app/banking_agent.py', 'class CircuitBreaker:', '连续失败熔断'),
 'S10': ('app/task_dag.py', 'class TaskDag:', '任务图、校验和生日迁移'),
 'S11': ('app/task_dag.py', 'class DagExecutor:', '节点调度、中断与声明式补偿'),
 'S12': ('app/task_dag.py', 'class TaskDagStore:', '任务图持久化'),
 'S13': ('app/domain_agent_contract.py', 'class DomainContext(', '不可变领域契约、Finding与Proposal'),
 'S14': ('app/domain_agent_registry.py', 'def load_domain_agents(', '固定六域原子注册；配套agents/各模块'),
 'S15': ('app/supervisor.py', 'class Supervisor:', '固定路由、来源比对、冲突合并与模板核对'),
 'S16': ('app/conversation_context.py', 'class TransferDraftStore:', '有限转账草稿；docs/T06说明补充限制'),
 'S17': ('docs/T07隔离执行演示.md', '## 隔离与授权边界', '受限沙箱与历史8场景记录；实现app/sandbox_execution.py'),
 'S18': ('docs/T08本地第二渠道.md', '##', '本地终端与真实IM边界；实现app/local_channel.py'),
 'S19': ('docs/T09本地人工接管.md', 'T18第2批增加', '本地工单状态、权限及新轮求助'),
 'S20': ('app/banking_agent.py', '    def _birthday_entities(', '生日独立提前天数；卡片显示请求见IntentParser.parse'),
 'S21': ('app/family_access.py', 'class FamilyAccess:', '演示会话、家庭身份与账户主体隔离'),
 'S22': ('app/llm_adapter.py', 'class LLMConfig:', '可选模型受限输出与文本外发边界'),
 'S23': ('app/static/app.js', 'function renderCardDisplay()', '显示设置、隐藏/恢复、新轮求助与前端交互'),
 'S24': ('docs/E7B适老化验收.md', '## 5.', '真实读屏仍需人工走查；语音实现app/speech_adapter.py'),
 'S25': ('docs/assets/提交日源码验收.json', '"current_full_windows":', '提交日互补分组回归；默认容器与完整AI镜像分开'),
 'S26': ('docs/E6边界测试矩阵.md', '## T01-T25', '正常/异常测试映射；tests/含各专项实现'),
 'S27': ('docs/需求追踪矩阵.md', '## L2', '官方交付要求、分层与未验收项'),
 'S28': ('README.md', '#', '运行方式、功能演示与工程边界'),
 'S29': ('docs/安全自评报告.md', '## 3.', '已知风险；发布边界见docs/T17发布前检查.md'),
 'S30': ('docs/assets/P11发行验收.json', '"status": "PASS_P11_LOCAL_REMOTE_CI_AND_PHYSICAL_HOST_PENDING"', 'P11源码、镜像、离线迁移及真人/远端边界'),
 'S31': ('app/preflight.py', 'class PreflightDimension:', '六维预检的数据结构与三态定义'),
 'S32': ('app/preflight.py', 'confirmed_expenses_cents = sum', '预算约束的实际整数分计算'),
 'S33': ('app/risk_controls.py', 'def score_transaction_risk(', '风险特征计分与可信历史输入'),
 'S34': ('app/ledger_views.py', 'def _dedup_key(', '双口径账本、状态分组与显式关联去重'),
 'S35': ('app/transaction_agent.py', 'def context_digest(', 'Context规范化摘要与支付快照绑定'),
 'S36': ('app/transaction_agent.py', 'def propose(self, ctx, finding):', '支付领域候选步骤与分支优先级'),
 'S37': ('app/supervisor.py', 'def _claims(', '来源化claims和同类型值冲突比较'),
 'S38': ('app/banking_agent.py', 'def check(self, item: AuthCredential,', '认证凭据时效、使用和绑定核对顺序'),
 'S39': ('app/banking_agent.py', 'def _hash_body(', '规范化审计字段及前驱哈希'),
 'S40': ('app/task_dag.py', 'def topological_order(', '稳定Kahn拓扑序、重复与未知依赖拒绝'),
 'S41': ('app/task_dag.py', 'def _write(self, dag:', '图快照与审计事件同事务提交'),
 'S42': ('app/guardianship.py', 'def evaluate(', '家庭授权、额度和有效性复核'),
 'S43': ('app/handoff_queue.py', 'def transition(', '坐席绑定、合法状态转移和固定结论'),
 'S44': ('app/agents/antifraud_agent.py', 'class AntifraudAgent(', '反诈领域的事实解释与核对选项'),
 'S45': ('app/agents/finance_health_agent.py', 'class FinanceHealthAgent(', '财务领域的覆盖范围和双口径解释'),
 'S46': ('app/agents/family_agent.py', 'class FamilyAgent(', '家庭领域授权缺失与本人核验建议'),
 'S47': ('app/agents/subscription_agent.py', 'class SubscriptionAgent(', '订阅领域选择、可终止性与退款未知'),
 'S48': ('app/agents/card_agent.py', 'class CardAgent(', '卡片领域状态与挂失/解挂建议'),
 'S49': ('tests/test_supervisor_integration.py', 'class SupervisorIntegrationTests(', '多域主链异常、认证与恢复验证'),
 'S50': ('app/knowledge_service.py', 'class KnowledgeService:', '知识权限过滤、混合检索、引用和拒答'),
 'S51': ('app/local_embeddings.py', 'class LocalEmbeddingClient:', '本机ONNX向量编码及模型校验'),
 'S52': ('app/local_reranker.py', 'class LocalReranker:', '独立cross-encoder重排及忙降级'),
 'S53': ('app/ocr_service.py', 'class OCRService:', '本地OCR、字段核对与草稿边界'),
 'S54': ('app/speech_adapter.py', 'class VoiceEvidenceStore:', '音频摘要、证据关联和到期清理'),
 'S55': ('app/dag_planner.py', 'def review(', '受控动态拓扑、节点预算和强制确认门'),
 'S56': ('app/bank_service.py', 'class BankServiceAdapter:', '银行协议、原请求查询、回调和对账'),
 'S57': ('app/fineract_transport.py', 'class FineractTransport:', '固定开源核心、虚构账户及原生撤销'),
 'S58': ('app/live_support.py', 'class LiveSupport:', '自建用户/坐席消息、回执与角色隔离'),
 'S59': ('app/identity_service.py', 'class IdentityService:', '本地密码、TOTP、持久会话和撤销'),
 'S60': ('docs/assets/P10性能压测验收.json', '"status": "PASS_P10_LOCAL_SCOPE_EXTERNAL_LOAD_UNMEASURED"', '冻结本机压测、资源、资金正确性和阈值'),
 'S61': ('docs/P03RAG知识库与验收.md', '### **10.2', '修订同题113/116/120、降级与真人评测边界'),
 'S62': ('docs/P05音频证据与生命周期.md', '### **1.1', '录音同意、云ASR固定短句及供应商删除边界'),
 'S63': ('scripts/ci_verify.py', 'def acceptable(', 'CI零跳过/资源警告、隔离数据库和分组'),
 'S64': ('scripts/source_release.py', 'def build(', '当前工作区确定性源码快照与白名单'),
 'S65': ('docs/assets/RAG修订验收.json', '"status": "PASS_LOCAL_RAG_AND_FULL_REGRESSION"', '保留旧题标签与阈值的三模式修订实测'),
 'S66': ('docs/assets/DAG真实模型验收.json', '"status": "PASS_REAL_MODEL_DAG_BOUNDED"', '两次真实模型DAG，首次拒绝及第二次原图确认'),
 'S67': ('docs/最终推进清单.md', '# 最终推进清单', '最终执行顺序、团队交接及人工/外部待办'),
 'S68': ('app/service_tasks.py', 'class ServiceTasks:', '交易异议、资金准备与本地交接，不是银行案件或退款'),
 'S69': ('app/source_provenance.py', 'class SourceProvenance:', '资料来源、版本与ACL重验，仅参考不授权'),
 'S70': ('app/payment_tasks.py', 'def resume_payment(', '原任务重准备、版本检查和新确认'),
 'S71': ('app/action_effects.py', 'def build_transfer_contract(', '单笔效果范围和原确认绑定'),
 'S72': ('docs/原型评测与真人验收.md', '## 2026-10-31', '当前原型/平台负载与真实样本待验'),
 'S73': ('tests/test_d1_security.py', 'def test_state_never_preloads_yellow_subscription_data(', '状态刷新不预读黄色订阅，确认后只读查询'),
 'S74': ('scripts/finalize_competition_video.py', 'def finalize(', '本地旁白、H.264/AAC与faststart；无外发'),
}


def source_records():
    rows = []
    for ident, (name, marker, note) in SOURCES.items():
        path = ROOT / name
        if not path.is_file():
            raise ValueError('Missing source: ' + name)
        line = None
        if marker:
            matches = [i for i, text in enumerate(path.read_text(encoding='utf-8').splitlines(), 1)
                       if text.lstrip().startswith(marker.lstrip())]
            if not matches:
                raise ValueError('Missing source marker: ' + name + ' / ' + marker)
            if len(matches) != 1 and marker not in ('#', '##'):
                raise ValueError('Ambiguous source marker: ' + name)
            line = matches[0]
        rows.append({'id': ident, 'path': name, 'line': line, 'note': note,
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    return rows


def fonts(font_dir):
    pdfmetrics.registerFont(TTFont('Body', str(font_dir / 'simsun.ttc'), subfontIndex=0))
    pdfmetrics.registerFont(TTFont('Heading', str(font_dir / 'msyhbd.ttc'), subfontIndex=0))
    pdfmetrics.registerFont(TTFont('Code', str(font_dir / 'consola.ttf')))
    pdfmetrics.registerFontFamily('Body', normal='Body', bold='Heading', italic='Body', boldItalic='Heading')


class Diagram(Flowable):
    def __init__(self, kind, width):
        super().__init__()
        self.kind, self.width = kind, width
        self.height = 310 if kind == 'architecture' else 232

    def draw(self):
        c = self.canv
        w = self.width

        def box(x, y, width, height, title, sub=''):
            c.setFillColor(LIGHT); c.setStrokeColor(LINE)
            c.roundRect(x, y, width, height, 4, fill=1, stroke=1)
            c.setFillColor(INK); c.setFont('Heading', 10)
            c.drawCentredString(x+width/2, y+height/2+(4 if sub else -3), title)
            if sub:
                c.setFont('Body', 8.5); c.drawCentredString(x+width/2, y+height/2-10, sub)

        def arrow(x, y, xx, yy):
            from math import atan2, cos, sin
            c.setStrokeColor(GREEN); c.setLineWidth(.8); c.line(x,y,xx,yy)
            angle = atan2(yy-y,xx-x)
            c.line(xx,yy,xx-5*cos(angle-.5),yy-5*sin(angle-.5))
            c.line(xx,yy,xx-5*cos(angle+.5),yy-5*sin(angle+.5))

        if self.kind == 'architecture':
            mid = w/2
            box(0,258,144,43,'Web / local_demo','会话、身份与设备检查')
            box(174,258,w-174,43,'AgentRuntime','注入检查、规则/可选受限模型')
            arrow(144,279,174,279)
            box(0,181,208,49,'Planner / PolicyEngine','预检、分色与原计划')
            box(238,181,w-238,49,'固定 Supervisor / 六域','可信快照、来源比对与冲突保留')
            arrow(220,258,104,230); arrow(208,205,238,205)
            box(0,101,208,49,'原计划确认 / 演示强验证','绿色自动；黄色确认；红色再确认')
            box(238,101,w-238,49,'持久 DAG / 固定工具','执行前复核、幂等与状态查询')
            arrow(w-100,181,104,150); arrow(208,126,238,126)
            box(238,20,w-238,49,'MockBankCore / SQLite','工具事实、回执与恢复证据')
            box(0,20,208,49,'审计哈希链 / 本地接管','关联关键节点；工单不执行资金业务')
            arrow(w-105,101,w-105,69); arrow(238,44,208,44)
        else:
            box(w/2-96,173,192,44,'A 锁定预算','原黄色确认后执行')
            box(0,91,208,44,'delay-B：生日减鲜花提前天数','未指定默认2天')
            box(w-208,91,208,44,'delay-C：生日减蛋糕提前天数','未指定默认1天')
            arrow(w/2-40,173,104,135); arrow(w/2+40,173,w-104,135)
            box(0,21,208,44,'B 订购鲜花','只执行已授权的固定工具')
            box(w-208,21,208,44,'C 订购蛋糕','只执行已授权的固定工具')
            arrow(104,91,104,65); arrow(w-104,91,w-104,65)
            c.setFillColor(INK); c.setFont('Body',8.5)
            c.drawCentredString(w/2,3,'取消仅补偿已声明的预算锁；不承诺已受理商户订单可退')


class Cover(Flowable):
    def __init__(self, width, height):
        super().__init__(); self.width, self.height = width, height

    def draw(self):
        c, w, h = self.canv, self.width, self.height
        c.setFillColor(INK); c.setFont('Heading',10)
        c.drawString(0,h-5,'人工智能赛道')
        c.drawRightString(w,h-5,'竞赛离线原型 / 提交日集成封版 · 2026-11-01')
        c.setFillColor(INK); c.setFont('Heading',55)
        c.drawString(32,h*.59,'技术文档')
        c.setFillColor(INK); c.setFont('Heading',23)
        c.drawString(32,h*.53,'澜银行 AI 智能体')
        c.setFont('Heading',12); c.drawString(32,h*.48,'自然语言理解、多领域核对与受控银行任务执行')
        c.setFont('Heading',10.5); c.setFillColor(INK)
        c.drawString(32,h*.40,'当前工作区实现快照 / 架构、算法、安全与验证')
        c.drawString(32,h*.34,'项目负责人：宫文馨')
        c.drawString(32,h*.30,'团队成员：董有轩、李浩林、郭嘉乐、李苏润')
        for offset in [0,23,46]:
            c.setLineWidth(.7); c.setStrokeColor(colors.HexColor('#84adb2'))
            path=c.beginPath(); path.moveTo(-55,120-offset)
            path.curveTo(90,190-offset,230,315+offset*.2,315,235)
            path.curveTo(365,190,430,135, w+55,100-offset)
            c.drawPath(path)


class TechnicalDoc(BaseDocTemplate):
    def beforeDocument(self):
        self.body_start = None; self.current_chapter = ''; self.headings = []

    def afterFlowable(self, flowable):
        if isinstance(flowable, Paragraph) and hasattr(flowable, 'heading_level'):
            level, text = flowable.heading_level, flowable.getPlainText()
            if self.body_start is None:
                self.body_start = self.page-1
            key = 'heading-' + hashlib.sha256(text.encode()).hexdigest()[:14]
            self.canv.bookmarkPage(key)
            self.canv.addOutlineEntry(text,key,level=level,closed=False)
            self.notify('TOCEntry',(level,text,self.page-self.body_start,key))
            if level == 0: self.current_chapter = text
            self.headings.append({'title':text,'level':level,'physical_page':self.page,
                                  'body_page':self.page-self.body_start})


def markup(text, emphasis=True):
    text = html.escape(text)
    text = re.sub(r'`([^`]+)`',r'<font name="Body">\1</font>',text)
    bold = r'<font color="#146b27"><b>\1</b></font>' if emphasis else r'<b>\1</b>'
    text = re.sub(r'\*\*([^*]+)\*\*',bold,text)
    # Keep the citation attached to the final body characters instead of letting
    # an isolated superscript fall onto a new line.
    return re.sub(r'([\u4e00-\u9fffA-Za-z0-9，。；、！？：）.]{0,4})((?:\[S\d+\])+)',
                  lambda m:'<nobr>'+m[1]+'<super size="6.8" rise="4">'+m[2]+'</super></nobr>',text)


def build(source, output, records, font_dir):
    material_evidence(ROOT, json.loads((ROOT/'docs/assets/提交日源码验收.json').read_text(encoding='utf-8')))
    fonts(font_dir)
    text=source.read_text(encoding='utf-8')
    rows=source_records()
    source_table='| 编号 | 文件与实际位置 | 证据说明 |\n|---|---|---|\n'
    source_table+='\n'.join('| '+r['id']+' | '+r['path']+(f":{r['line']}" if r['line'] else '')+' | '+r['note']+' |' for r in rows)
    region='<!-- sources:start -->\n'+source_table+'\n<!-- sources:end -->'
    text=text.replace(':::sources',region)
    text=re.sub(r'<!-- sources:start -->.*?<!-- sources:end -->',lambda _:region,text,flags=re.S)
    if text!=source.read_text(encoding='utf-8'):
        source.write_text(text,encoding='utf-8')
    citations=set(re.findall(r'\[(S\d+)\]',text))
    if citations-set(SOURCES): raise ValueError('Unknown source reference')
    styles={
      'body':ParagraphStyle('body',fontName='Body',fontSize=10.5,leading=17.5,
                            textColor=INK,spaceAfter=8,firstLineIndent=21,wordWrap='CJK',allowWidows=0),
      'h1':ParagraphStyle('h1',fontName='Heading',fontSize=17,leading=25,textColor=INK,
                          backColor=LIGHT,borderPadding=6,spaceAfter=17,keepWithNext=True,wordWrap='CJK'),
      'h2':ParagraphStyle('h2',fontName='Heading',fontSize=12.5,leading=20,spaceBefore=9,
                          spaceAfter=10,textColor=INK,keepWithNext=True,wordWrap='CJK'),
      'h3':ParagraphStyle('h3',fontName='Heading',fontSize=10.8,leading=18,spaceBefore=10,
                          spaceAfter=7,textColor=INK,keepWithNext=True,wordWrap='CJK'),
      'point':ParagraphStyle('point',fontName='Heading',fontSize=10.5,leading=18,spaceBefore=9,
                            spaceAfter=6,leftIndent=15,bulletIndent=0,bulletFontName='Heading',
                            bulletFontSize=10.5,bulletColor=GREEN,textColor=GREEN,
                            keepWithNext=True,wordWrap='CJK'),
      'pointBody':ParagraphStyle('pointBody',fontName='Body',fontSize=10.5,leading=17.5,
                            textColor=INK,spaceAfter=9,leftIndent=30,firstLineIndent=0,
                            bulletIndent=16,bulletFontName='Body',bulletFontSize=7,bulletColor=GREEN,
                            wordWrap='CJK',allowWidows=0),
      'cell':ParagraphStyle('cell',fontName='Body',fontSize=9,leading=14,wordWrap='CJK',textColor=INK),
      'caption':ParagraphStyle('caption',fontName='Heading',fontSize=9.5,leading=15,textColor=INK,alignment=TA_CENTER,spaceAfter=12),
      'sourceCaption':ParagraphStyle('sourceCaption',fontName='Heading',fontSize=8.5,leading=14,
                                    textColor=INK,spaceBefore=4,spaceAfter=7,wordWrap='CJK',keepWithNext=True),
      'code':ParagraphStyle('code',fontName='Body',fontSize=9,leading=14,backColor=colors.HexColor('#f3f6f4'),
                            borderPadding=8,spaceBefore=7,spaceAfter=10,wordWrap='CJK'),
    }
    width,height=A4; margin=46; content_width=width-2*margin
    doc=TechnicalDoc(str(output),pagesize=A4,leftMargin=margin,rightMargin=margin,
                     topMargin=44,bottomMargin=43,title='澜银行 AI 智能体技术文档',
                     author='澜银行 AI 智能体项目',subject='竞赛离线Mock架构、算法、安全与验证')
    def footer(c,d):
        if d.body_start is None: return
        c.saveState();c.setFont('Body',8);c.setFillColor(colors.HexColor('#64756f'))
        c.drawString(margin,22,'澜银行 AI 智能体 · 技术文档')
        c.drawCentredString(width/2,22,str(d.page-d.body_start))
        c.drawRightString(width-margin,22,'竞赛离线 Mock 实现')
        c.restoreState()
    frame=Frame(margin,43,content_width,height-87,leftPadding=0,rightPadding=0,topPadding=0,bottomPadding=0)
    doc.addPageTemplates(PageTemplate(id='technical',frames=frame,onPageEnd=footer))
    toc=TableOfContents();toc.dotsMinLevel=0
    toc.levelStyles=[ParagraphStyle('toc0',fontName='Heading',textColor=INK,fontSize=10.5,leading=17,spaceBefore=6,wordWrap='CJK'),
                     ParagraphStyle('toc1',fontName='Heading',textColor=INK,fontSize=10,leading=15.5,leftIndent=17,wordWrap='CJK'),
                     ParagraphStyle('toc2',fontName='Heading',textColor=INK,fontSize=9.5,leading=14,leftIndent=32,wordWrap='CJK')]
    story=[Cover(content_width,height-95),PageBreak(),
           Paragraph('目　录',ParagraphStyle('tocTitle',fontName='Heading',textColor=INK,fontSize=18,leading=28,alignment=TA_CENTER,spaceAfter=20)),toc]

    citation_wrap_repairs=[]
    def paragraph(s,kind='body'):
        value=markup(s,kind not in ('h1','h2','h3','caption','sourceCaption'))
        bullet='•' if kind=='point' else '○' if kind=='pointBody' else None
        item=Paragraph(value,styles[kind],bulletText=bullet)
        if kind in ('body','pointBody') and '<super ' in value:
            # ReportLab's CJK breaker ignores <nobr>. Inspect its real layout and
            # move the final short body fragment with the superscript if needed.
            item.wrap(content_width,100000)
            last=item.blPara.lines[-1]
            tail=''.join(getattr(f,'text','') for f in getattr(last,'words',[])).strip()
            if re.fullmatch(r'(?:\[S\d+\])+',tail):
                cut=value.rfind('<nobr>')
                if cut>=0:
                    value=value[:cut]+'<br/>'+value[cut:]
                    item=Paragraph(value,styles[kind],bulletText=bullet)
                    citation_wrap_repairs.append(s[-100:])
        return item

    def codeblock(code,language='python'):
        # Display only. Source lines remain unchanged; wrapped display rows retain
        # the original indentation and have no second line number.
        code=[line.expandtabs(4) for line in code]
        shades=[['#111111']*len(line) for line in code]
        if language=='python':
            try:
                for item in tokenize.generate_tokens(io.StringIO('\n'.join(code)+'\n').readline):
                    color=None
                    if item.type==token.COMMENT:color='#B65C00'
                    elif item.type in (token.STRING,getattr(token,'FSTRING_START',None),
                                        getattr(token,'FSTRING_MIDDLE',None),getattr(token,'FSTRING_END',None)):color='#0086B3'
                    elif item.type==token.NUMBER:color='#098658'
                    elif item.type==token.NAME and item.string in keyword.kwlist:color='#A000B5'
                    elif item.type==token.NAME and item.string=='self':color='#005CC5'
                    elif item.type==token.OP and item.string in ('=','+','-','*','/','//','%','==','!=','<=','>=','<','>','&','|',':=','+=','-='):color='#C92D3A'
                    if color:
                        for row in range(item.start[0]-1,min(item.end[0],len(code))):
                            begin=item.start[1] if row==item.start[0]-1 else 0
                            end=item.end[1] if row==item.end[0]-1 else len(code[row])
                            shades[row][begin:end]=[color]*(end-begin)
            except (tokenize.TokenError,IndentationError):
                # An excerpt can end in an open expression; keep parsed spans.
                pass
        size=8.6;available=content_width-34-18
        line_style=ParagraphStyle('codeLine',fontName='Code',fontSize=size,leading=12.5,
                                  textColor=INK,splitLongWords=False,wordWrap=None)
        number_style=ParagraphStyle('codeNumber',fontName='Code',fontSize=size,leading=12.5,
                                    alignment=2,textColor=colors.HexColor('#999999'))
        def char_font(char):return 'Code' if ord(char)<128 else 'Body'
        def width(value):return sum(pdfmetrics.stringWidth(c,char_font(c),size) for c in value)
        def rich(value,colors_for_chars):
            runs=[];last=None
            for char,color in zip(value,colors_for_chars):
                key=(char_font(char),color)
                if key!=last:runs.append([key,'']);last=key
                runs[-1][1]+=char
            return ''.join('<font name="'+key[0]+'" color="'+key[1]+'">'+html.escape(value).replace(' ','&#160;')+'</font>' for key,value in runs) or '&#160;'
        formatted=[];code_display=[]
        for number,line in enumerate(code,1):
            segments=[];offset=0;prefix=''
            while offset<len(line):
                end=offset;used=width(prefix)
                while end<len(line) and used+width(line[end])<=available:
                    used+=width(line[end]);end+=1
                if end<len(line):
                    breaks=[i+1 for i in range(offset,end) if line[i].isspace() or line[i]==',']
                    if breaks and breaks[-1]-offset>max(8,(end-offset)//2):end=breaks[-1]
                if end==offset:raise ValueError('Code display cannot fit a character')
                segments.append(rich(prefix+line[offset:end],['#111111']*len(prefix)+shades[number-1][offset:end]))
                offset=end;prefix=' '*min(len(line)-len(line.lstrip())+4,24)
            code_display.append({'line':number,'wrapped_rows':max(1,len(segments))})
            formatted.append([Paragraph(str(number),number_style),Paragraph('<br/>'.join(segments) or '&#160;',line_style)])
        code_layouts.append(code_display)
        block=Table(formatted,colWidths=[34,content_width-34],hAlign='LEFT')
        block.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),colors.HexColor('#F7F7F7')),
                                  ('VALIGN',(0,0),(-1,-1),'TOP'),('TOPPADDING',(0,0),(-1,-1),.7),
                                  ('BOTTOMPADDING',(0,0),(-1,-1),.7),('LEFTPADDING',(0,0),(-1,-1),9),
                                  ('RIGHTPADDING',(0,0),(-1,-1),9),('TOPPADDING',(0,0),(-1,0),6),
                                  ('BOTTOMPADDING',(0,-1),(-1,-1),6),
                                  ('BOX',(0,0),(-1,-1),.4,colors.HexColor('#EAEAEA'))]))
        return block

    def table(rows, source_table=False):
        count=len(rows[0]);weights=[1/count]*count
        if count==3: weights=[.21,.40,.39]
        if source_table: weights=[.09,.47,.44]
        formatted=[[Paragraph(('<b>'+markup(cell.strip(),False)+'</b>') if index==0 else markup(cell.strip()),styles['cell'])
                    for cell in row] for index,row in enumerate(rows)]
        result=Table(formatted,colWidths=[content_width*x for x in weights],repeatRows=1,hAlign='LEFT')
        result.setStyle(TableStyle([('VALIGN',(0,0),(-1,-1),'TOP'),('BACKGROUND',(0,0),(-1,0),LIGHT),
                                   ('FONTNAME',(0,0),(-1,0),'Heading'),('LINEBELOW',(0,0),(-1,0),.8,GREEN),
                                   ('LINEBELOW',(0,1),(-1,-1),.3,LINE),('TOPPADDING',(0,0),(-1,-1),5 if source_table else 7),
                                   ('BOTTOMPADDING',(0,0),(-1,-1),5 if source_table else 7),('LEFTPADDING',(0,0),(-1,-1),7),
                                   ('RIGHTPADDING',(0,0),(-1,-1),7)]))
        return result

    lines=text.splitlines();index=0;table_number=0;chapter=''
    figures=[];screenshots=[];excerpts=[];code_layouts=[];point_count=0;point_active=False
    def keep_upcoming_figure(next_index,extra):
        upcoming=[s.strip() for s in lines[next_index:] if s.strip()][:2]
        if upcoming and upcoming[0].startswith('- **'):
            upcoming=upcoming[1:];extra+=35
        if upcoming and upcoming[0].startswith(':::figure '):
            kind=upcoming[0].split()[1]
            story.append(CondPageBreak(TechnicalFigure.HEIGHTS[kind]+extra))
    while index<len(lines):
        line=lines[index].strip();index+=1
        if line.startswith('## '):
            point_active=False
            heading=line[3:];chapter=heading.split('.')[0];table_number=0
            # Keep diagram chapters intact; allow short continuations to share a
            # page with the following chapter rather than leave near-empty pages.
            story.append(PageBreak() if chapter in ('1','2','10','附录A') else CondPageBreak(190))
            item=paragraph(heading,'h1');item.heading_level=0;story.append(item)
        elif line.startswith('### '):
            point_active=False
            keep_upcoming_figure(index,80)
            item=paragraph(line[4:],'h2');item.heading_level=1;story.append(item)
        elif line.startswith('#### '):
            point_active=False
            keep_upcoming_figure(index,75)
            item=paragraph(line[5:],'h3');item.heading_level=2;story.append(item)
        elif line.startswith('# ') or line.startswith('<!--') or not chapter:
            # Metadata before chapter 1 is intentionally kept out of body/TOC.
            continue
        elif line.startswith('- **'):
            keep_upcoming_figure(index,55)
            point_active=True;point_count+=1;story.append(paragraph(line[2:],'point'))
        elif line.startswith(':::figure '):
            kind=line.split()[1];caption=lines[index].strip();index+=1
            figures.append({'kind':kind,'caption':caption})
            story.append(KeepTogether([TechnicalFigure(kind,content_width,ROOT),Spacer(1,8),paragraph(caption,'caption')]))
        elif line.startswith(':::image '):
            name=line.split(' ',1)[1];caption=lines[index].strip();index+=1
            allowed=(ROOT/'data/verification').resolve()
            image_path=(ROOT/name).resolve() if name.startswith('data/verification/') else (allowed/'t20-20261022/scenes'/name).resolve()
            if '_internal' in image_path.parts: raise ValueError('Private screenshot path rejected')
            if not image_path.is_relative_to(allowed) or not image_path.is_file():raise ValueError('Missing or unsafe document screenshot')
            image=Image(str(image_path));scale=min(content_width/image.imageWidth,390/image.imageHeight)
            image.drawWidth=image.imageWidth*scale;image.drawHeight=image.imageHeight*scale
            screenshots.append({'path':image_path.relative_to(ROOT).as_posix(),'caption':caption,
                                'sha256':hashlib.sha256(image_path.read_bytes()).hexdigest()})
            story.append(KeepTogether([image,Spacer(1,8),paragraph(caption,'caption')]))
        elif line.startswith(':::code '):
            _,ident,offset,count=line.split();ref=next(r for r in rows if r['id']==ident)
            begin=ref['line']+int(offset);count=int(count)
            if not 1<=count<=35 or begin<1:raise ValueError('Invalid source excerpt')
            snippet=(ROOT/ref['path']).read_text(encoding='utf-8').splitlines()[begin-1:begin-1+count]
            excerpts.append({'reference':ident,'path':ref['path'],'start_line':begin,'lines':len(snippet)})
            label=paragraph(f"源码：{ref['path']}:{begin}-{begin+count-1} [{ident}]",'sourceCaption')
            story.extend([label,codeblock(snippet),Spacer(1,10)])
        elif line==':::sources':
            for group in range(0,len(rows),10):
                group_rows=[['编号','文件与实际位置','证据说明']]
                group_rows += [[r['id'],r['path']+(f":{r['line']}" if r['line'] else ''),r['note']] for r in rows[group:group+10]]
                story.append(table(group_rows,True));story.append(Spacer(1,12))
        elif line.startswith('```'):
            language=line[3:]
            block=[]
            while index<len(lines) and not lines[index].strip().startswith('```'):
                block.append(lines[index]);index+=1
            index+=1
            story.extend([codeblock(block,language),Spacer(1,10)])
        elif line.startswith('> '):
            point_active=True;point_count+=1;story.append(paragraph(line[2:],'point'))
        elif line.startswith('|'):
            block=[line]
            while index<len(lines) and lines[index].strip().startswith('|'):
                block.append(lines[index].strip());index+=1
            data=[[x.strip() for x in s.strip('|').split('|')] for s in block if not re.fullmatch(r'[| :\-]+',s)]
            if any(len(row)!=len(data[0]) for row in data): raise ValueError('Malformed table')
            if data[0][0]=='编号':
                story.extend([table(data,True),Spacer(1,10)])
            else:
                table_number+=1
                caption=paragraph(f'表{chapter}-{table_number} '+data[0][0]+'与实现边界','caption')
                caption.keepWithNext=True;story.extend([caption,table(data),Spacer(1,10)])
        elif line:
            story.append(paragraph(line,'pointBody' if point_active else 'body'))
    output.parent.mkdir(parents=True,exist_ok=True)
    doc.multiBuild(story,maxPasses=10)
    records.mkdir(parents=True,exist_ok=True)
    record={'output':output.relative_to(ROOT).as_posix(),'sha256':hashlib.sha256(output.read_bytes()).hexdigest(),
            'markdown':source.relative_to(ROOT).as_posix(),'markdown_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'source_references':rows,'headings':doc.headings,'pages':doc.page,
            'vector_figures':figures,'screenshots':screenshots,'source_excerpts':excerpts,
            'point_count':point_count,'code_layouts':code_layouts,
            'citation_wrap_repairs':len(citation_wrap_repairs),
            'typography':{'headings':'black, Microsoft YaHei Bold','emphasis':'green bold',
                          'citations':'superscript, 6.8pt, raised 4pt',
                          'code':'Consolas 8.6pt, SimSun CJK fallback, snippet line numbers, light gray panel'},
            'runtime_dependency_added':False,'scope':'authoring only; no bank code changes'}
    (records/'build.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'pages':doc.page,'output':str(output),'references':len(rows)},ensure_ascii=True))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--font-dir',type=Path,default=Path('C:/Windows/Fonts'))
    parser.add_argument('--records',type=Path,default=ROOT/'data/verification/t21-20261022')
    args=parser.parse_args()
    build(ROOT/'docs/技术文档.md',ROOT/'submission/澜银行AI智能体_技术文档.pdf',
          args.records,args.font_dir)


if __name__=='__main__': main()

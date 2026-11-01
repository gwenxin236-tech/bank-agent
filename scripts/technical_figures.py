"""Vector technical figures for the document authoring tool, not app runtime."""
from pathlib import Path
import json
import re
from collections import Counter
from math import atan2, cos, sin

from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import Flowable

NAVY='#173E4B'; TEAL='#167E83'; BLUE='#4475A1'; AMBER='#B58431'; RED='#A85256'
PALE='#EFF6F5'; LINE='#CCDBDC'; INK='#243B42'; MUTED='#627B82'


class TechnicalFigure(Flowable):
    HEIGHTS={'architecture':368,'preflight':295,'supervisor':316,'authentication':352,
             'birthday':285,'transaction':276,'recovery':286,'audit':264,'sandbox':302,
             'ledger':278,'testcounts':270,'deployment':270,
             'rag':310,'multimodal':290,'dynamic_dag':280}

    def __init__(self,kind,width,root):
        super().__init__(); self.kind=kind;self.width=width;self.height=self.HEIGHTS[kind];self.root=Path(root)

    def draw(self):
        self.c=self.canv;self.c.saveState();self.c.scale(self.width/500,1)
        getattr(self,'draw_'+self.kind)();self.c.restoreState()

    def text(self,x,y,text,size=9,color=INK,bold=False,center=False):
        self.c.setFillColor(colors.black if bold else colors.HexColor(color));self.c.setFont('Heading' if bold else 'Body',size)
        fn=self.c.drawCentredString if center else self.c.drawString
        fn(x,y,text)

    def lines(self,x,y,items,size=9,color=INK,leading=14):
        for i,line in enumerate(items):self.text(x,y-i*leading,line,size,color)

    def rect(self,x,y,w,h,fill='#FFFFFF',stroke=LINE,r=5):
        self.c.setFillColor(colors.HexColor(fill));self.c.setStrokeColor(colors.HexColor(stroke));self.c.setLineWidth(.7)
        self.c.roundRect(x,y,w,h,r,fill=1,stroke=1)

    def box(self,x,y,w,h,title,body=(),color=TEAL):
        self.rect(x,y,w,h);self.c.setFillColor(colors.HexColor(color));self.c.rect(x,y+h-3,w,3,fill=1,stroke=0)
        self.text(x+10,y+h-19,title,10,color,True)
        self.lines(x+10,y+h-36,body,8.4,leading=12)

    def arrow(self,points,color=TEAL,dash=False,label=None,labelpos=None):
        c=self.c;c.saveState();c.setStrokeColor(colors.HexColor(color));c.setLineWidth(.9)
        if dash:c.setDash(3,2)
        for (x,y),(xx,yy) in zip(points,points[1:]):c.line(x,y,xx,yy)
        c.setDash();(x,y),(xx,yy)=points[-2:];angle=atan2(yy-y,xx-x)
        c.line(xx,yy,xx-5*cos(angle-.45),yy-5*sin(angle-.45));c.line(xx,yy,xx-5*cos(angle+.45),yy-5*sin(angle+.45));c.restoreState()
        if label and labelpos:self.text(*labelpos,label,8,MUTED)

    def tag(self,x,y,text,color=TEAL):
        w=pdfmetrics.stringWidth(text,'Heading',8.5)+16
        self.rect(x,y-4,w,17,PALE,PALE,3);self.text(x+8,y+1,text,8.5,color,True)

    def note(self,text,y=8):self.text(0,y,text,8,MUTED)

    def draw_architecture(self):
        self.tag(0,350,'当前实现 / 理解与建议、授权与执行分离')
        rows=[('接入层',['Web / 自建坐席','local_demo 终端','演示 / 密码+TOTP'],BLUE),
              ('理解层',['规则 + 可选云模型','本地RAG / OCR','原计划 / 动态提案'],BLUE),
              ('核对层',['只读预检与快照','六域分析 / 建议','Supervisor 来源比对'],TEAL),
              ('执行层',['PolicyEngine','确认 / 绑定强验证','持久 DAG / 固定工具'],TEAL),
              ('事实层',['Mock / Fineract','交易 / 幂等状态','SQLite / 自建坐席'],TEAL)]
        for i,(label,items,color) in enumerate(rows):
            y=280-i*55;self.rect(0,y,432,46,PALE,PALE)
            self.text(10,y+18,label,9,color,True)
            for j,t in enumerate(items):
                x=67+j*120;self.rect(x,y+7,114,32);self.text(x+57,y+19,t,8.3,center=True)
            if i<4:self.arrow([(240,y),(240,y-9)],color)
        self.rect(446,60,54,266,'#F2F4F8','#D1D9E4')
        for i,t in enumerate(['审计链','身份关联','关键决策','工具结果','状态回放']):self.text(473,293-i*45,t,8.4,BLUE,True,True)
        for i in range(5):self.arrow([(432,303-i*55),(446,303-i*55)],BLUE,True)
        self.rect(0,18,500,29,'#FFF7E9','#EDDFC4')
        self.text(10,29,'独立沙箱为运维演示；不接聊天任意代码，不给银行容器挂 Docker socket',8.5,AMBER)

    def draw_preflight(self):
        self.tag(0,277,'一次只读预检 / 六个维度')
        blocks=[('01 可用资金',['available >= amount','未知来源保持 unknown']),('02 日累计限额',['daily + amount <= limit','边界等于限额可满足']),
                ('03 收款人核验',['匹配可信目录 / 已核验账户','亲属称呼不等于核验']),('04 卡状态',['正常 / 受限 / 未知','空列表不视为正常']),
                ('05 认证条件',['none / confirmation / strong_auth','仅报告要求，不代替认证']),('06 预算约束',['余额 - 确认支出 - 缓冲 - 转账','预计收入不填补缺口'])]
        for i,(t,b) in enumerate(blocks):self.box((i%2)*258,193-(i//2)*77,242,66,t,b,BLUE if i<4 else TEAL)
        self.rect(0,13,500,26,PALE,PALE);self.text(10,23,'satisfied 满足     missing 已知不满足     unknown 无法核实     ≠ 执行授权',8.6,TEAL,True)

    def draw_supervisor(self):
        self.tag(0,297,'转账规划：按需协调，不新增银行查询')
        for i,(name,body) in enumerate([('支付预检','payment'),('风险快照','antifraud'),('预算投影','finance_health')]):
            y=219-i*69;self.box(0,y,116,54,name,[body]);self.box(142,y,123,54,body,['analyze / propose'],BLUE)
            self.arrow([(116,y+26),(142,y+26)])
        self.box(300,105,200,167,'Supervisor 核对门',[
            '1. Context / 信封 / 发送域', '2. 事实与平台快照逐项一致',
            '3. 工具、参数及选项白名单','4. 聚合 claims，保留冲突',
            '5. 原三步转账模板核对'])
        for i in range(3):self.arrow([(265,245-i*69),(300,245-i*40)])
        self.arrow([(400,105),(400,70)]);self.rect(0,22,500,48,PALE,PALE)
        self.text(12,51,'ready / needs_review / needs_user_choice / needs_clarification',9,TEAL,True)
        self.text(12,34,'ready 仍需原确认与认证；家庭代办额外核对 family 授权快照',8.6)

    def draw_authentication(self):
        labels=['用户 / Web','AgentRuntime','凭证簿（进程内）','Mock 核心'];xs=[44,174,310,456]
        for x,label in zip(xs,labels):
            self.rect(x-43,315,86,27,PALE,PALE);self.text(x,325,label,8.4,TEAL,True,True)
            self.c.setStrokeColor(colors.HexColor(LINE));self.c.setDash(2,3);self.c.line(x,305,x,30);self.c.setDash()
        steps=[(0,1,'1 发送转账请求'),(1,0,'2 原计划 / 摘要 / 风险'),(0,1,'3 首次确认'),(1,2,'4 签发绑定凭据'),
               (1,0,'5 请求演示认证'),(0,1,'6 提交演示验证码'),(1,2,'7 检查绑定并标记通过'),(1,0,'8 回到原计划，尚未扣款'),
               (0,1,'9 再次确认，消费凭据'),(1,3,'10 复核后提交幂等指令'),(3,1,'11 核心状态 / 工具事实'),(1,0,'12 逐项回执与审计关联')]
        for i,(a,b,label) in enumerate(steps):
            y=297-i*22;self.arrow([(xs[a],y),(xs[b],y)],BLUE if a>b else TEAL)
            self.text(min(xs[a],xs[b])+5,y+4,label,7.9)
        self.note('凭据绑定用户、设备、任务、节点、操作、金额与收款人；验证成功不直接扣款')

    def draw_birthday(self):
        self.tag(0,266,'已授权延迟任务 / 时间参数分别绑定')
        self.box(143,193,214,53,'A 锁定预算',['原黄色确认后，先落盘再推进'])
        for x,title,subtitle in [(0,'delay-B：生日 - 鲜花提前天数','默认2天；可独立指定'),(276,'delay-C：生日 - 蛋糕提前天数','默认1天；可独立指定')]:
            self.box(x,108,224,56,title,[subtitle],BLUE)
            self.arrow([(205 if x==0 else 295,193),(x+112,164)])
            self.box(x,31,224,49,'B 订花' if x==0 else 'C 订蛋糕',['固定工具 / 节点幂等']);self.arrow([(x+112,108),(x+112,80)])
        self.note('取消仅逆序补偿已声明的预算锁；已受理商户订单不伪造自动退款')

    def draw_transaction(self):
        self.tag(0,256,'核心状态与用户视图分开')
        for i,(state,col) in enumerate([('created',BLUE),('processing',AMBER),('succeeded',TEAL),('reversed',MUTED)]):
            self.box(i*129,157,113,43,state,[],col)
            if i<3:self.arrow([(i*129+113,178),((i+1)*129,178)],col)
        self.box(130,82,113,42,'failed',[],RED)
        self.arrow([(56,157),(56,104),(130,104)],RED);self.arrow([(185,157),(185,124)],RED)
        self.rect(273,66,227,60,'#FFF7E9','#EDDFC4')
        self.lines(283,107,['UNKNOWN 是结果核验中的视图', '不是新的核心终态；只查询原请求', '没有新幂等键、没有再次扣款'],8.6,AMBER)
        self.arrow([(242,157),(263,143),(285,126)],AMBER,True)
        self.note('实线：允许核心转移；虚线：不确定结果的解释 / 查询边界',27)

    def draw_recovery(self):
        self.tag(0,267,'恢复根据持久证据，不根据对话推断')
        cols=[('01 执行前 checkpoint',['节点 running / 指令标识','先保存快照与审计']),('02 核心受理',['真实副作用可能已经发生','同一幂等键绑定原请求']),('03 结果 checkpoint',['工具结果与终态持久化','成功节点不再次分派'])]
        for i,(title,body) in enumerate(cols):
            x=i*174;self.box(x,170,152,68,title,body,BLUE if i==0 else TEAL)
            if i<2:self.arrow([(x+152,203),(x+174,203)])
        for i,(title,body) in enumerate([('未开始',['原授权复核后执行','不能跳过确认门']),('已受理 / 未落结果',['只查询原交易','unknown 保留待核验']),('已有成功证据',['重建本地投影','剩余动作重新授权'])]):
            x=i*174;self.arrow([(x+76,170),(x+76,144)],AMBER,True);self.box(x,58,152,86,title,body,AMBER)
        self.note('C8 为单机演练；恢复不代表磁盘损坏、异地容灾或生产 RPO/RTO 认证',20)

    def draw_audit(self):
        self.tag(0,245,'全局 seq 哈希链 / trace 作为筛选关联')
        for i in range(3):
            x=i*174;self.box(x,112,152,104,f'事件 E{i+1}',[f'seq = k + {i}', 'actor / subject / device', '决策 / 工具 / 脱敏 payload', 'prev_hash + current_hash'])
            if i<2:self.arrow([(x+152,162),(x+174,162)],BLUE)
        self.rect(0,45,500,42,PALE,PALE)
        self.text(12,70,'H(Ei) = SHA256(规范化事件字段 + prev_hash)',10,TEAL,True)
        self.text(12,54,'GENESIS 为首事件前驱；回放某 trace 时仍核对全局链关联',8.5)
        self.note('SQLite 哈希链支持本地篡改检测；不等同生产 WORM 或外部签名',17)

    def draw_sandbox(self):
        self.tag(0,282,'独立运维演示 / 无银行权限')
        self.box(0,98,175,154,'宿主控制器', ['受限 AST 检查','固定探针选择','镜像 ID / 平台检查','时长与输出监控','摘要审计与熔断'],BLUE)
        self.rect(234,57,266,203,PALE,'#98BDBD');self.text(247,240,'隔离容器边界',11,TEAL,True)
        self.lines(247,214,['network none / 无宿主挂载','只读根目录 / UID 65534','cap-drop ALL / 禁止提权','64MiB内存 / 0.5 CPU / 32 PID','结构化整数结果 / 有限输出'],9.2,leading=25)
        self.arrow([(175,199),(234,199)],TEAL,label='校验后代码',labelpos=(177,210))
        self.arrow([(234,108),(175,108)],BLUE,label='结构化回传',labelpos=(177,119))
        self.note('没有聊天任意代码入口、真实模型生成或生产敌对代码隔离认证',23)

    def draw_ledger(self):
        self.tag(0,258,'同一授权账务切片 / 确定性整数分')
        self.box(110,191,280,48,'已入账事实与明确跨源关联',['先排除处理中 / 受限 / 预计到账并分列'])
        values=[('消费口径',['实际消费 - 已核实退款','本人转账 / 还款不重算消费']),('现金流口径',['已入账流出 - 流入','转账 / 还款计入资金流出']),('资金状态',['可用 / 处理中 / 受限','预计到账单列，不合并资产'])]
        for i,(title,body) in enumerate(values):
            x=i*174;self.box(x,68,152,86,title,body,BLUE if i<2 else AMBER)
            self.arrow([(250,191),(250,174),(x+76,174),(x+76,154)])
        self.note('只按 canonical_id / original_transaction_id / linked_transaction_id 去重',34)
        self.note('相同商户、金额、日期不足以认定重复；coverage 明确来源边界',17)

    def draw_testcounts(self):
        evidence=json.loads((self.root/'docs/assets/银伴更名源码验收.json').read_text(encoding='utf-8'))
        counts=evidence['test_modules'];total=evidence['regression']['current_full_windows']['tests']
        if sum(counts.values())!=total:raise ValueError('actual test module counts mismatch')
        record={'total':total,'modules':counts}
        names=[('test_auth_credentials','认证凭据'),('test_task_dag','DAG原语'),('test_dag_integration','DAG集成'),('test_supervisor_integration','多域主链'),('test_handoff_queue','本地接管'),('test_banking_flow_ux','交互与浏览器'),('test_birthday_linkage','生日联动')]
        counts=record['modules'];maximum=max(counts.get(k,0) for k,_ in names)
        self.tag(0,248,'指定测试模块的实际执行数')
        for i,(key,label) in enumerate(names):
            y=210-i*26;value=counts.get(key,0);self.text(0,y,label,9)
            self.c.setFillColor(colors.HexColor(TEAL if i%2==0 else BLUE));self.c.roundRect(116,y-3,320*value/maximum,13,2,fill=1,stroke=0)
            self.text(445,y,str(value),9,TEAL,True)
        self.note('全部 '+str(record['total'])+' 项来自实际完整报告；此图仅列部分模块，不是覆盖率',10)

    def draw_deployment(self):
        self.tag(0,248,'当前源码与默认容器 / AI完整镜像另验',TEAL)
        self.box(0,149,224,70,'维护者准备', ['审核源码与互补回归','默认容器按最终CI核验'],BLUE)
        self.box(276,149,224,70,'历史三配置回退',['模型/依赖随旧镜像保留','校验后导入，不冒称当前功能'],TEAL)
        self.arrow([(224,184),(276,184)],TEAL)
        self.box(0,38,224,70,'接收方前置环境',['Python 3.12/3.13默认源码模式','可选镜像需兼容Docker引擎'],BLUE)
        self.box(276,38,224,70,'目标电脑验收',['校验 → load → 本地启动','实际断网复核与保留验收记录'],AMBER)
        self.arrow([(388,149),(388,108)],AMBER,True);self.arrow([(224,73),(276,73)],BLUE)
        self.note('历史AI三配置不代表当前集成；物理异机与真人验收另列',12)

    def draw_rag(self):
        self.tag(0,288,'本机向量 + 独立重排 / 访问权限前后复核')
        self.box(0,199,224,60,'用户问题 + 当前阅读主体',['明确问题边界；资料不能成为指令'],BLUE)
        self.box(276,199,224,60,'资料权限过滤',['owner / readers / 家庭显式共享'],TEAL)
        self.arrow([(224,229),(276,229)])
        self.box(0,101,224,65,'本地混合召回',['BGE-small-zh ONNX / 512维','词项 + 向量融合；索引可重建'],TEAL)
        self.box(276,101,224,65,'独立BGE重排',['cross-encoder按问题/片段打分','有限候选；繁忙/低分有边界'],BLUE)
        self.arrow([(388,199),(388,181),(112,181),(112,166)])
        self.arrow([(224,133),(276,133)])
        self.box(0,15,500,56,'引用摘录 / 拒答',['再次核验访问权；版本、片段ID、源行与SHA可追溯；不生成银行账户事实'],TEAL)
        self.arrow([(388,101),(388,71)])

    def draw_multimodal(self):
        self.tag(0,267,'图片和声音先进入核对流程，不能直接办理业务')
        self.box(0,164,224,70,'图片路径',['文件类型/大小/解码预算','本地OCR → 位置/置信度/冲突'],BLUE)
        self.box(276,164,224,70,'声音路径',['录制 → 试听 → 明确本次同意','本机hash；云ASR须单独确认'],BLUE)
        self.box(0,66,224,66,'人工核对字段',['金额采用整数分','低置信度/冲突不自动采纳'],TEAL)
        self.box(276,66,224,66,'人工核对文本',['不保留原录音；证据单次绑定','转写仅供编辑，不能自动提交'],TEAL)
        self.arrow([(112,164),(112,132)]);self.arrow([(388,164),(388,132)])
        self.arrow([(112,66),(112,43),(250,43)]);self.arrow([(388,66),(388,43),(250,43)])
        self.text(250,18,'普通草稿 → 原权限、确认、认证和审计链',10,TEAL,True,True)

    def draw_dynamic_dag(self):
        self.tag(0,258,'动态的是合法拓扑，不是模型授权')
        self.box(0,166,145,63,'原可信业务计划',['实体与必须步骤','来自原请求/平台'],BLUE)
        self.box(178,166,145,63,'不受信结构提案',['本地 / 可选模型','无金额、身份或结果'],BLUE)
        self.box(356,166,144,63,'Supervisor审核',['必须步骤/白名单','无环/节点/边/深度'],TEAL)
        self.arrow([(145,197),(178,197)]);self.arrow([(323,197),(356,197)])
        self.box(0,51,224,68,'采纳：撤销旧卡与旧认证',['持久化图与摘要；尚不执行','用户重新核对并确认'],TEAL)
        self.box(276,51,224,68,'原DagExecutor执行',['最后一刻PolicyEngine复核','节点边界暂停/恢复/声明补偿'],TEAL)
        self.arrow([(428,166),(428,140),(112,140),(112,119)])
        self.arrow([(224,85),(276,85)])
        self.note('真实模型结构本轮已验；生产并行调度及任意跨业务组合仍未完成',14)

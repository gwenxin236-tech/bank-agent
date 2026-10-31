#!/usr/bin/env python3
"""Human-operated local support acceptance. Never claims an automated human.

Metadata only in the report; temporary case database stores normal support text.
The helper writes its generated local operator token to a private ignored file,
never prints it, and never accepts a model/bank key as an operator credential.
"""
import argparse,hashlib,hmac,json,os,secrets,sys,tempfile,threading,time
from pathlib import Path
from datetime import datetime
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'app'))
USER_PHRASE='这是用户端人工验收，请协助核对，不办理业务。'
OPERATOR_PHRASE='这是坐席端人工回复，请核对原记录，不要重复提交。'


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);parser.add_argument('--timeout-seconds',type=int,default=1800);parser.add_argument('--fineract-directory',type=Path)
    args=parser.parse_args();output=args.output.resolve()
    if not output.is_relative_to(ROOT/'data/verification') or not 60<=args.timeout_seconds<=7200:raise ValueError('owned verification path and bounded duration required')
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='p08-human-') as tmp:
        os.environ['BANK_AGENT_DB_PATH']=str(Path(tmp)/'import.sqlite3');os.environ['BANK_AGENT_OFFLINE']='1'
        import banking_agent as ba
        original=ba.runtime
        if args.fineract_directory:
            sys.path.insert(0,str(ROOT/'scripts'));import fineract_demo
            _,rt,_,_=fineract_demo.runtime(args.fineract_directory.resolve())
        else:rt=ba.AgentRuntime(db_path=Path(tmp)/'human.sqlite3',offline=True)
        token=secrets.token_urlsafe(32);rt.family.demo_sessions[token]={'user_id':'human-operator','device_id':'human-operator-device','role':'admin'}
        ba.runtime=rt;ba.gateway=ba.AgentGateway(rt);done=threading.Event();human=False
        before=rt.bank.balance().data.get('available_cents');started=time.time()
        def snapshot():
            with rt.audit.lock:
                rows=[r for r in rt.audit.db.execute('SELECT * FROM support_messages ORDER BY seq').fetchall()
                      if datetime.fromisoformat(r['created_at']).timestamp()>=started]
                case_ids={r['case_id'] for r in rows};checks={};evidence=[]
                for case_id in case_ids:
                    case=rt.audit.db.execute('SELECT * FROM local_handoff_cases WHERE case_id=?',(case_id,)).fetchone()
                    user=[r for r in rows if r['case_id']==case_id and r['sender_role']=='user' and r['text']==USER_PHRASE]
                    ops=[r for r in rows if r['case_id']==case_id and r['sender_role']=='operator' and r['actor_id']=='human-operator' and r['text']==OPERATOR_PHRASE]
                    if not user or not ops:continue
                    peers=rt.audit.db.execute('SELECT role,read_seq FROM support_peers WHERE case_id=?',(case_id,)).fetchall();read={p['role']:p['read_seq'] for p in peers}
                    checks={'fixed_user_message':True,'fixed_operator_reply':True,'both_sides_acknowledged_display':read.get('operator',0)>=user[-1]['seq'] and read.get('user',0)>=ops[-1]['seq'],
                            'original_audit_valid':rt.audit.verify_trace(case['trace_id'])['valid']}
                    evidence=[{'case_id':case_id,'trace_id':case['trace_id'],'message_id':r['server_id'],'seq':r['seq'],'sender_role':r['sender_role'],
                        'content_sha256':r['fingerprint'],'created_at':r['created_at']} for r in [user[-1],ops[-1]]];break
            current=rt.bank.balance().data.get('available_cents');checks['bank_balance_unchanged']=current==before
            return {'scope':'human attestation, same operator may operate both pages; self-hosted support, not official bank customer service or production identity',
                'status':'PASS_USER_ATTESTED_TWO_PAGES' if human and len(checks)==5 and all(checks.values()) else 'PENDING_HUMAN',
                'human_checked':human,'checks':checks,'messages':evidence,'raw_text_in_report':False,'operator_token_in_report':False,
                'bank_balance_before_cents':before,'bank_balance_after_cents':current,'bank_business_executed':False,'started_at_epoch':started}
        def save(report=None):output.write_text(json.dumps(report or snapshot(),ensure_ascii=False,indent=2),encoding='utf-8')
        class Handler(ba.Handler):
            def setup(self):super().setup();self.connection.settimeout(30)
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path=='/api/v1/support/capabilities' and hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):
                    return self._send(200,{'ok':True,'manual_acceptance_available':True,'user_phrase':USER_PHRASE,'operator_phrase':OPERATOR_PHRASE})
                return super().do_GET()
            def do_POST(self):
                nonlocal human
                if self.path!='/qa/finish':return super().do_POST()
                if not hmac.compare_digest(self.headers.get('Authorization',''),'Bearer '+token):return self._send(403,{'ok':False})
                body=self._body()
                if set(body)!={'human_checked'} or body['human_checked'] is not True:return self._send(400,{'ok':False,'message':'请先手动核对两端，再勾选。'})
                report=snapshot()
                if len(report['checks'])!=5 or not all(report['checks'].values()):return self._send(400,{'ok':False,'message':'固定短句、两端显示回执或审计尚未齐备，请刷新对话核对。'})
                human=True;save();self._send(200,{'ok':True,'message':'验收元数据已保存'});done.set()
        server=ba.ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=False;thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_port)
        login=output.parent/'operator-login.txt';login.write_text('仅用于本次本机P08人工验收的坐席令牌。不要发到聊天或提交Git。\n\n'+token+'\n',encoding='utf-8')
        instructions=output.parent/'人工验收操作.md';instructions.write_text('# P08本机人工验收\n\n1. 打开用户端：'+base+'/\n2. 点一键人工客服；在下方对话输入并发送：'+USER_PHRASE+'\n3. 打开坐席端：'+base+'/support/operator\n4. 从同目录operator-login.txt复制本次临时令牌，粘到坐席令牌框，核验会话，领取工单。不要输入SiliconFlow或银行密钥。\n5. 核对用户文字，回复：'+OPERATOR_PHRASE+'\n6. 用户端刷新对话，确认收到回复；坐席端勾选人工核对，点保存人工验收元数据并结束。\n\n本次允许同一人扮演两角色；不会办理银行业务或连接官方客服。报告只保存摘要和消息元数据。\n',encoding='utf-8')
        print('USER_URL: '+base+'/',flush=True);print('OPERATOR_URL: '+base+'/support/operator',flush=True);print('INSTRUCTIONS: '+str(instructions),flush=True);save()
        try:
            deadline=time.monotonic()+args.timeout_seconds
            while not done.wait(1) and time.monotonic()<deadline:pass
        finally:
            save();server.shutdown();server.server_close();thread.join(timeout=5)
            for item in (rt,original):item.recovery_scheduler.stop();item.bank.close();item.audit.db.close()
        print('HUMAN_REPORT: '+str(output),flush=True)


if __name__=='__main__':main()

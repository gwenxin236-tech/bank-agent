"""Self-hosted support messaging on the original case/identity/audit boundary.

No LLM, bank tool dispatch, external messaging provider or bank-state updates.
Browser display acknowledgement is not proof that a human read/understood text.
"""
import hashlib,re,time,uuid
from datetime import datetime,timezone


class LiveSupport:
    MAX_CASE_MESSAGES=1000
    MAX_MESSAGES=10000
    def __init__(self,queue,clock=time.time):
        self.queue=queue;self.audit=queue.audit;self.db=queue.db;self.lock=queue.lock;self.clock=clock
        with self.lock,self.db:
            self.db.executescript('''CREATE TABLE IF NOT EXISTS support_messages(
              seq INTEGER PRIMARY KEY AUTOINCREMENT,server_id TEXT UNIQUE NOT NULL,case_id TEXT NOT NULL,
              sender_role TEXT NOT NULL,actor_id TEXT NOT NULL,device_id TEXT NOT NULL,
              client_id TEXT NOT NULL,fingerprint TEXT NOT NULL,text TEXT NOT NULL,created_at TEXT NOT NULL,
              UNIQUE(case_id,sender_role,actor_id,device_id,client_id));
              CREATE TABLE IF NOT EXISTS support_peers(
              case_id TEXT NOT NULL,role TEXT NOT NULL,actor_id TEXT NOT NULL,device_id TEXT NOT NULL,
              last_seen REAL NOT NULL,offered_seq INTEGER NOT NULL DEFAULT 0,read_seq INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(case_id,role,actor_id,device_id));''')
    def _case(self,case_id,identity):
        if not isinstance(case_id,str) or not re.fullmatch('case_[a-p]{32}',case_id):raise ValueError('invalid case reference')
        row=self.db.execute('SELECT * FROM local_handoff_cases WHERE case_id=?',(case_id,)).fetchone()
        if not row or not isinstance(identity,dict):raise PermissionError('case unavailable')
        actor,device=identity.get('user_id'),identity.get('device_id')
        if identity.get('role')=='admin':
            if row['status'] not in {'LOCAL_CLAIMED','LOCAL_CLOSED'} or (actor,device)!=(row['operator_id'],row['operator_device']):raise PermissionError('assigned operator/device required')
            role='operator'
        elif identity.get('role')=='user' and (actor,device)==(row['user_id'],row['device_id']):role='user'
        else:raise PermissionError('case owner/device required')
        return row,role,actor,device
    @staticmethod
    def _cursor(value):
        if type(value) is not int or not 0<=value<2**63:raise ValueError('invalid message cursor')
    @staticmethod
    def _text(text):
        if not isinstance(text,str) or not text.strip() or len(text)>2000 or len(text.encode())>8000:raise ValueError('bounded message required')
        if any(ord(c)<32 and c not in '\n\t' for c in text):raise ValueError('invalid control character')
        if (re.search(r'(?<!\d)\d{6}(?!\d)|(?<!\d)\d{11,19}(?!\d)|sk-[A-Za-z0-9_-]{12,}',text,re.I)
                or re.search(r'(?:密码|验证码|API\s*key)\s*[:：=]?\s*[A-Za-z0-9_-]{4,}',text,re.I)):
            raise ValueError('do not send credentials, account numbers or verification codes')
        return text.strip()
    def _peer(self,row,role,actor,device):
        self.db.execute('INSERT OR IGNORE INTO support_peers(case_id,role,actor_id,device_id,last_seen) VALUES(?,?,?,?,?)',(row['case_id'],role,actor,device,self.clock()))
        return self.db.execute('SELECT * FROM support_peers WHERE case_id=? AND role=? AND actor_id=? AND device_id=?',(row['case_id'],role,actor,device)).fetchone()
    def _record(self,row,identity,event,payload):
        with self.audit.identity(identity['user_id'],row['user_id'],identity['device_id']):
            self.audit.write(row['trace_id'],event,dict(payload,case_id=row['case_id'],bank_business_executed=False),actor=identity['user_id'],intent='support_communication',commit=False)
    def _view(self,message,case):
        if message['sender_role']=='user':role,actor,device='operator',case['operator_id'],case['operator_device']
        else:role,actor,device='user',case['user_id'],case['device_id']
        peer=self.db.execute('SELECT read_seq FROM support_peers WHERE case_id=? AND role=? AND actor_id=? AND device_id=?',(case['case_id'],role,actor,device)).fetchone()
        return {'seq':message['seq'],'message_id':message['server_id'],'client_message_id':message['client_id'],'case_id':message['case_id'],
                'sender_role':message['sender_role'],'text':message['text'],'created_at':message['created_at'],'delivery':'SERVER_STORED',
                'peer_displayed':bool(peer and peer[0]>=message['seq']),'bank_business_executed':False}
    def send(self,case_id,identity,message_id,text):
        text=self._text(text)
        if not isinstance(message_id,str) or not re.fullmatch('[A-Za-z0-9_-]{1,96}',message_id):raise ValueError('invalid message id')
        fingerprint=hashlib.sha256(text.encode()).hexdigest()
        with self.lock,self.db:
            case,role,actor,device=self._case(case_id,identity)
            prior=self.db.execute('SELECT * FROM support_messages WHERE case_id=? AND sender_role=? AND actor_id=? AND device_id=? AND client_id=?',(case_id,role,actor,device,message_id)).fetchone()
            if prior:
                if prior['fingerprint']!=fingerprint:raise ValueError('message id reused with different text')
                return dict(self._view(prior,case),duplicate=True)
            if case['status']=='LOCAL_CLOSED':raise ValueError('case closed; request a new support case')
            if self.db.execute('SELECT count(*) FROM support_messages WHERE case_id=?',(case_id,)).fetchone()[0]>=self.MAX_CASE_MESSAGES or self.db.execute('SELECT count(*) FROM support_messages').fetchone()[0]>=self.MAX_MESSAGES:raise ValueError('support message capacity reached')
            server_id='support_'+uuid.uuid4().hex.translate(str.maketrans('0123456789abcdef','abcdefghijklmnop'))
            self.db.execute('INSERT INTO support_messages(server_id,case_id,sender_role,actor_id,device_id,client_id,fingerprint,text,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                (server_id,case_id,role,actor,device,message_id,fingerprint,text,datetime.fromtimestamp(self.clock(),timezone.utc).isoformat()))
            row=self.db.execute('SELECT * FROM support_messages WHERE server_id=?',(server_id,)).fetchone()
            self._record(case,identity,'support.message.stored',{'message_id':server_id,'seq':row['seq'],'sender_role':role,
                        'characters':len(text),'content_sha256_bytes':list(hashlib.sha256(text.encode()).digest())})
            return dict(self._view(row,case),duplicate=False)
    def poll(self,case_id,identity,after_seq):
        self._cursor(after_seq)
        with self.lock,self.db:
            case,role,actor,device=self._case(case_id,identity);peer=self._peer(case,role,actor,device)
            rows=self.db.execute('SELECT * FROM support_messages WHERE case_id=? AND seq>? ORDER BY seq LIMIT 50',(case_id,after_seq)).fetchall()
            offered=max([peer['offered_seq']]+[r['seq'] for r in rows])
            self.db.execute('UPDATE support_peers SET last_seen=?,offered_seq=? WHERE case_id=? AND role=? AND actor_id=? AND device_id=?',(self.clock(),offered,case_id,role,actor,device))
            operator=self.db.execute("SELECT last_seen FROM support_peers WHERE case_id=? AND role='operator' AND actor_id=? AND device_id=?",(case_id,case['operator_id'],case['operator_device'])).fetchone()
            target=('operator',case['operator_id'],case['operator_device']) if role=='user' else ('user',case['user_id'],case['device_id'])
            reader=self.db.execute('SELECT read_seq FROM support_peers WHERE case_id=? AND role=? AND actor_id=? AND device_id=?',(case_id,*target)).fetchone()
            return {'ok':True,'mode':'SELF_HOSTED_SUPPORT','case_id':case_id,'case_status':case['status'],'messages':[self._view(r,case) for r in rows],
                    'next_cursor':rows[-1]['seq'] if rows else after_seq,'more':len(rows)==50,'operator_page_online':bool(operator and self.clock()-operator[0]<=15),
                    'peer_displayed_upto':reader[0] if reader else 0,
                    'human_reading_proven':False,'third_party_bank_customer_service':False,'bank_business_executed':False}
    def ack(self,case_id,identity,upto_seq):
        self._cursor(upto_seq)
        with self.lock,self.db:
            case,role,actor,device=self._case(case_id,identity);peer=self._peer(case,role,actor,device)
            if upto_seq>peer['offered_seq'] or upto_seq and not self.db.execute('SELECT 1 FROM support_messages WHERE case_id=? AND seq=?',(case_id,upto_seq)).fetchone():raise ValueError('only an offered batch can be acknowledged')
            if upto_seq>peer['read_seq']:
                self.db.execute('UPDATE support_peers SET read_seq=?,last_seen=? WHERE case_id=? AND role=? AND actor_id=? AND device_id=?',(upto_seq,self.clock(),case_id,role,actor,device))
                self._record(case,identity,'support.page.displayed',{'role':role,'upto_seq':upto_seq,'human_reading_proven':False})
            return {'ok':True,'displayed_upto':max(upto_seq,peer['read_seq']),'human_reading_proven':False,'bank_business_executed':False}


OPERATOR_SCRIPT=r'''
'use strict';
const byId=id=>document.getElementById(id);
let operatorToken='',activeCase=null,operatorCursor=0,operatorPending=null,operatorTimer=null,operatorPolling=false,operatorCaseClosed=false;
let operatorIdentityMode=false;
async function loadOperatorIdentity(){try{const cap=await(await fetch('/api/v1/identity/capabilities')).json();if(cap.mode!=='SELF_HOSTED')return;operatorIdentityMode=true;const r=await fetch('/api/v1/identity/me');const me=await r.json();if(!r.ok||me.role!=='admin'){location.assign('/identity/login');return;}byId('operatorToken').disabled=true;byId('operatorToken').placeholder='已通过密码与动态码登录';byId('operatorLogin').textContent='使用已登录坐席会话';notice('已登录本机坐席，请点击核验进入工单；不用输入令牌。');}catch(_){}}
loadOperatorIdentity();
const operatorMessages=new Map();
async function supportPost(path,body){const r=await fetch(path,{method:'POST',headers:{Authorization:'Bearer '+operatorToken,'Content-Type':'application/json'},body:JSON.stringify(body)});const d=await r.json();if(!r.ok||!d.ok)throw Error(d.message||'请求未获确认，请核查原记录');return d;}
function notice(message){byId('operatorNotice').textContent=message;}
function pendingKey(){return 'operatorSupportPending:'+activeCase;}
function loadPending(){try{return JSON.parse(sessionStorage.getItem(pendingKey())||'null');}catch(_){return null;}}
function clearPending(){operatorPending=null;try{sessionStorage.removeItem(pendingKey());}catch(_){}}
async function textHash(text){return Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text))),n=>n.toString(16).padStart(2,'0')).join('');}
function messageId(){return 'op_'+Array.from(crypto.getRandomValues(new Uint8Array(16)),n=>n.toString(16).padStart(2,'0')).join('');}
function showMessages(rows){for(const row of rows){operatorMessages.set(row.seq,row);if(operatorPending&&row.sender_role==='operator'&&row.client_message_id===operatorPending.id)clearPending();}const log=byId('operatorMessages');log.replaceChildren();for(const row of [...operatorMessages.values()].sort((a,b)=>a.seq-b.seq)){const item=document.createElement('li');item.className=row.sender_role;const title=document.createElement('strong');title.textContent=row.sender_role==='user'?'用户':'坐席';const text=document.createElement('p');text.textContent=row.text;const status=document.createElement('small');status.textContent=row.peer_displayed?'对方页面已显示':'服务器已保存，未核对对方显示';item.append(title,text,status);log.append(item);}}
async function pollSupport(){if(!activeCase||operatorPolling)return;operatorPolling=true;const selectedCase=activeCase;try{const d=await supportPost('/api/v1/support/poll',{case_id:selectedCase,after_seq:operatorCursor});if(selectedCase!==activeCase)return;for(const message of operatorMessages.values()){if(message.sender_role==='operator')message.peer_displayed=message.seq<=d.peer_displayed_upto;}showMessages(d.messages);if(d.messages.length){operatorCursor=d.next_cursor;await supportPost('/api/v1/support/ack',{case_id:activeCase,upto_seq:d.next_cursor});}operatorCaseClosed=d.case_status==='LOCAL_CLOSED';byId('operatorSend').disabled=operatorCaseClosed;notice(d.case_status==='LOCAL_CLOSED'?'工单已结单；历史对话保留，不能再发送。':'自建坐席会话。留言不办理交易，不能代替银行核账。');}catch(e){notice('对话连接未确认：'+e.message);}finally{operatorPolling=false;}}
async function selectCase(item){activeCase=item.case_id;operatorCursor=0;operatorMessages.clear();operatorPending=loadPending();operatorCaseClosed=false;byId('activeCase').textContent=item.case_id;byId('operatorText').value='';byId('operatorComposer').hidden=false;await pollSupport();}
async function loadInbox(){const session=operatorToken;const d=await supportPost('/api/v1/handoff/inbox',{});if(!operatorToken||operatorToken!==session)return;const list=byId('operatorCases');list.replaceChildren();for(const item of d.items){const li=document.createElement('li');const title=document.createElement('p');title.textContent=item.case_id+' · '+item.status;const open=document.createElement('button');open.textContent=item.status==='LOCAL_QUEUED'?'领取并打开':'打开对话';open.addEventListener('click',async()=>{try{if(item.status==='LOCAL_QUEUED')await supportPost('/api/v1/handoff/claim',{case_id:item.case_id});await selectCase(item);}catch(e){notice(e.message);}});li.append(title,open);list.append(li);}if(!d.items.length)notice('暂无工单；请先在用户端发起人工求助。');}
byId('operatorLogin').addEventListener('click',async()=>{operatorToken=operatorIdentityMode?'cookie-session':byId('operatorToken').value.trim();byId('operatorToken').value='';try{await loadInbox();byId('operatorSession').hidden=false;byId('operatorLoginPanel').hidden=true;operatorTimer=setInterval(()=>{if(document.visibilityState==='visible')pollSupport();},2500);notice('已核验本地坐席会话，领取工单后才能查看和回复。');}catch(e){operatorToken='';notice('会话未通过：'+e.message);}});
byId('operatorRefresh').addEventListener('click',()=>loadInbox().catch(e=>notice(e.message)));
byId('operatorLogout').addEventListener('click',async()=>{if(operatorIdentityMode){try{const r=await fetch('/api/v1/identity/logout',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});if(!r.ok)throw Error('logout not confirmed');location.assign('/identity/login');return;}catch(_){notice('退出结果未确认，请重试。');return;}}operatorToken='';activeCase=null;operatorPending=null;operatorMessages.clear();clearInterval(operatorTimer);byId('operatorMessages').replaceChildren();byId('operatorCases').replaceChildren();byId('operatorText').value='';byId('activeCase').textContent='';byId('operatorComposer').hidden=true;byId('operatorSession').hidden=true;byId('operatorLoginPanel').hidden=false;notice('已退出，令牌和页面对话引用已清空。');});
byId('operatorSend').addEventListener('click',async()=>{if(!activeCase)return;const text=byId('operatorText').value.trim();if(!text)return;const hash=await textHash(text);if(operatorPending&&operatorPending.hash!==hash){notice('上一条发送结果未知，请保留原文重试或先查询。');return;}operatorPending ||= {id:messageId(),hash};try{sessionStorage.setItem(pendingKey(),JSON.stringify(operatorPending));}catch(_){}const btn=byId('operatorSend');btn.disabled=true;try{await supportPost('/api/v1/support/send',{case_id:activeCase,message_id:operatorPending.id,text});clearPending();byId('operatorText').value='';await pollSupport();}catch(e){notice('发送未确认，原消息编号保留：'+e.message);}finally{btn.disabled=operatorCaseClosed;}});
byId('operatorResolve').addEventListener('click',async()=>{if(!activeCase)return;try{await supportPost('/api/v1/handoff/resolve',{case_id:activeCase,resolution:byId('operatorResolution').value});await pollSupport();await loadInbox();}catch(e){notice('结单未通过：'+e.message);}});
async function showAcceptance(){try{const r=await fetch('/api/v1/support/capabilities',{headers:{Authorization:'Bearer '+operatorToken}});const d=await r.json();if(d.manual_acceptance_available){byId('operatorAcceptance').hidden=false;byId('acceptanceInstructions').textContent='用户端发送：'+d.user_phrase+'；坐席端回复：'+d.operator_phrase+'。两端确认显示后再勾选保存。';}}catch(_){}}
byId('operatorLogin').addEventListener('click',()=>setTimeout(showAcceptance,300));
byId('humanChecked').addEventListener('change',()=>{byId('saveHumanAcceptance').disabled=!byId('humanChecked').checked;});
byId('saveHumanAcceptance').addEventListener('click',async()=>{if(!byId('humanChecked').checked)return;try{await supportPost('/qa/finish',{human_checked:true});notice('人工验收元数据已保存，可以关闭页面。');byId('saveHumanAcceptance').disabled=true;clearInterval(operatorTimer);}catch(e){notice('尚不能保存验收：'+e.message);}});
window.addEventListener('pagehide',()=>{clearInterval(operatorTimer);operatorToken='';});
'''

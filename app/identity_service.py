"""Optional self-hosted password/TOTP provider. No bank identity certification.

RFC6238 factor verification and opaque, revocable browser sessions; stdlib only.
Secrets live only in the owned local DB/enrollment file, never in audit or HTTP.
"""
import argparse,base64,hashlib,hmac,json,os,re,secrets,struct,sys,time
from contextlib import contextmanager,nullcontext
from contextvars import ContextVar
from dataclasses import replace
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit
from production_contracts import AdapterContext,AdapterResult,new_request_id

_CURRENT=ContextVar('verified_identity',default=None)
ROOT=Path(__file__).resolve().parents[1]

def digest(value):return hashlib.sha256(value.encode()).hexdigest()

def totp(secret,instant,digits=6):
    key=base64.b32decode(secret+'='*((-len(secret))%8),casefold=True)
    mac=hmac.new(key,struct.pack('>Q',int(instant)//30),hashlib.sha1).digest();offset=mac[-1]&15
    return str((struct.unpack('>I',mac[offset:offset+4])[0]&0x7fffffff)%10**digits).zfill(digits)


class IdentityService:
    IDLE_TTL=900;SESSION_TTL=8*3600;LOCK_SECONDS=300;ITERATIONS=600000
    def __init__(self,runtime):
        self.rt=runtime;self.db=runtime.audit.db;self.lock=runtime.audit.lock;self.clock=time.time
        self.public_origin=os.getenv('IDENTITY_PUBLIC_ORIGIN','').rstrip('/')
        if self.public_origin:
            u=urlsplit(self.public_origin)
            if u.scheme not in {'http','https'} or u.hostname not in {'127.0.0.1','localhost','::1'} or u.username or u.password or u.path or u.query or u.fragment:raise ValueError('identity origin must be explicit loopback')
        with self.lock,self.db:
            self.db.executescript('''CREATE TABLE IF NOT EXISTS identity_accounts(
              user_id TEXT PRIMARY KEY,role TEXT NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL,
              totp_secret TEXT NOT NULL,last_counter INTEGER NOT NULL DEFAULT -1,active INTEGER NOT NULL DEFAULT 1,
              version INTEGER NOT NULL DEFAULT 1,failures INTEGER NOT NULL DEFAULT 0,locked_until REAL NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS identity_sessions(
              session_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,device_hash TEXT NOT NULL,device_id TEXT NOT NULL,
              account_version INTEGER NOT NULL,created_at REAL NOT NULL,last_seen REAL NOT NULL,expires_at REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS identity_challenges(
              challenge_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,actor_id TEXT NOT NULL,subject_id TEXT NOT NULL,
              device_id TEXT NOT NULL,operation_digest TEXT NOT NULL,expires_at REAL NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
              passed INTEGER NOT NULL DEFAULT 0,used_at REAL,trace_id TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS identity_login_limits(
              client_id TEXT PRIMARY KEY,attempts INTEGER NOT NULL,expires_at REAL NOT NULL);''')
        runtime.bank.guardianships.require_explicit_consent=True
        runtime.bank.guardianships.identity_device_resolver=self.family_device
        self._dummy=hashlib.pbkdf2_hmac('sha256',b'non-account-timing',b'fixed-dummy-salt',self.ITERATIONS).hex()
        service=getattr(runtime,'bank_service',None)
        if service is not None:
            # Only the already-restricted local adapter can be injected in this runtime.
            # Device permission is scoped to a verified request, never a new global grant.
            self._bank_binding=service.resolve_binding
            service.resolve_binding=self._local_bank_binding
    def family_device(self,user):
        if user not in self.rt.bank.guardianships.accounts:return None
        with self.lock:
            now=self.clock();rows=self.db.execute("SELECT DISTINCT s.device_id FROM identity_sessions s JOIN identity_accounts a ON a.user_id=s.user_id WHERE s.user_id=? AND a.active=1 AND a.role='user' AND s.revoked=0 AND s.account_version=a.version AND s.expires_at>? AND s.last_seen>?",(user,now,now-self.IDLE_TTL)).fetchall()
        return rows[0][0] if len(rows)==1 else None

    def approve_family_scope(self,grant_id,permissions,version,code):
        identity=self.current_identity();grant=self.rt.bank.guardianships.get(grant_id)
        if not identity or identity['role']!='user' or not grant or grant.subject_id!=identity['user_id']:return False
        with self.transaction():
            account=self.db.execute('SELECT * FROM identity_accounts WHERE user_id=?',(identity['user_id'],)).fetchone();counter=self._counter(account,code)
            if account['locked_until']>self.clock():return False
            if counter is None:
                failures=account['failures']+1
                self.db.execute('UPDATE identity_accounts SET failures=?,locked_until=? WHERE user_id=?',(failures,self.clock()+self.LOCK_SECONDS if failures>=5 else 0,identity['user_id']))
                self.record('family_scope_denied',identity['user_id'],reason='invalid_or_reused_totp')
                return False
            self.db.execute('UPDATE identity_accounts SET last_counter=?,failures=0 WHERE user_id=?',(counter,identity['user_id']))
            self.record('family_scope_approved',identity['user_id'],grant_id=grant_id,permissions=permissions,previous_version=version)
        return True

    def _local_bank_binding(self,actor):
        binding=self._bank_binding(actor);identity=self.current_identity()
        if binding and identity and identity['role']=='user' and actor==identity['user_id']==binding.actor_id==binding.subject_id=='demo-user':
            return replace(binding,device_ids=tuple(dict.fromkeys((*binding.device_ids,identity['device_id']))))
        return binding
    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute('BEGIN IMMEDIATE')
            try:yield;self.db.commit()
            except BaseException:self.db.rollback();raise
    def record(self,event,actor,trace='',**data):
        self.rt.audit.write(trace or ('identity_'+new_request_id()),'identity.'+event,data,actor=actor or 'anonymous',intent='identity',commit=False)
    def provision(self,user,password,role='user',secret=None):
        if not isinstance(user,str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}',user) or role not in {'user','admin'}:raise ValueError('invalid principal')
        if not isinstance(password,str) or not 15<=len(password)<=128:raise ValueError('password must contain 15 to 128 characters')
        secret=secret or base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
        if not re.fullmatch('[A-Z2-7]{16,64}',secret):raise ValueError('invalid TOTP enrollment')
        salt=secrets.token_hex(16);hashed=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),self.ITERATIONS).hex()
        with self.transaction():
            self.db.execute('INSERT INTO identity_accounts(user_id,role,salt,password_hash,totp_secret) VALUES(?,?,?,?,?)',(user,role,salt,hashed,secret))
            self.record('provisioned',user,role=role)
        return secret
    def _counter(self,account,code):
        if not isinstance(code,str) or not re.fullmatch('[0-9]{6}',code):return None
        now=self.clock();candidates=[int(now)//30+d for d in (0,-1,1)]
        for counter in candidates:
            if counter>account['last_counter'] and hmac.compare_digest(totp(account['totp_secret'],counter*30),code):return counter
        return None
    def login(self,user,password,code,device='',old_session='',client='local'):
        if not isinstance(user,str) or len(user)>64 or not isinstance(password,str) or len(password)>128:return None
        client_id=digest(str(client));now=self.clock()
        with self.transaction():
            self.db.execute('DELETE FROM identity_login_limits WHERE expires_at<?',(now,))
            limit=self.db.execute('SELECT * FROM identity_login_limits WHERE client_id=?',(client_id,)).fetchone()
            if limit and limit['attempts']>=20:return None
            if not limit and self.db.execute('SELECT count(*) FROM identity_login_limits').fetchone()[0]>=10000:return None
            self.db.execute('INSERT INTO identity_login_limits VALUES(?,1,?) ON CONFLICT(client_id) DO UPDATE SET attempts=attempts+1',(client_id,now+self.LOCK_SECONDS))
            account=self.db.execute('SELECT * FROM identity_accounts WHERE user_id=?',(user,)).fetchone()
            if account and account['locked_until']>now:return None
            salt=bytes.fromhex(account['salt']) if account else b'fixed-dummy-salt'
            computed=hashlib.pbkdf2_hmac('sha256',password.encode(),salt,self.ITERATIONS).hex()
            counter=self._counter(account,code) if account else None
            if not account or not account['active'] or not hmac.compare_digest(computed,account['password_hash'] if account else self._dummy) or counter is None:
                if account:
                    failures=account['failures']+1
                    self.db.execute('UPDATE identity_accounts SET failures=?,locked_until=? WHERE user_id=?',(failures,now+self.LOCK_SECONDS if failures>=5 else 0,user))
                self.record('login_denied',user if account else 'anonymous',reason='credentials_or_replay');return None
            # Expired failures do not become a permanent account lock.
            self.db.execute('UPDATE identity_accounts SET failures=0,locked_until=0,last_counter=? WHERE user_id=?',(counter,user))
            if old_session:self.db.execute('UPDATE identity_sessions SET revoked=1 WHERE session_id=?',(digest(old_session),))
            self.db.execute('DELETE FROM identity_sessions WHERE expires_at<?',(now,))
            if self.db.execute('SELECT count(*) FROM identity_sessions').fetchone()[0]>=10000:raise ValueError('session capacity reached')
            raw=secrets.token_urlsafe(32);device=device if re.fullmatch('[A-Za-z0-9_-]{40,64}',device or '') else secrets.token_urlsafe(32)
            sid=digest(raw);device_id='device_'+digest(device)[:32]
            self.db.execute('INSERT INTO identity_sessions VALUES(?,?,?,?,?,?,?,?,0)',(sid,user,digest(device),device_id,account['version'],now,now,now+self.SESSION_TTL))
            self.record('login_passed',user,device_id=device_id,method='password_totp')
            return {'token':raw,'device':device,'expires_at':now+self.SESSION_TTL}
    def _resolve(self,token,device,touch=False):
        if not isinstance(token,str) or not isinstance(device,str) or len(token)>128 or len(device)>128:raise PermissionError('session required')
        row=self.db.execute('SELECT s.*,a.role,a.active,a.version FROM identity_sessions s JOIN identity_accounts a ON a.user_id=s.user_id WHERE session_id=?',(digest(token),)).fetchone()
        now=self.clock()
        if not row or row['revoked'] or not row['active'] or row['account_version']!=row['version'] or now>=row['expires_at'] or now-row['last_seen']>=self.IDLE_TTL or not hmac.compare_digest(row['device_hash'],digest(device)):raise PermissionError('session/device invalid')
        if touch:self.db.execute('UPDATE identity_sessions SET last_seen=? WHERE session_id=?',(now,row['session_id']))
        return {'user_id':row['user_id'],'device_id':row['device_id'],'role':row['role'],'session_id':row['session_id'],'family':row['user_id'] in self.rt.bank.guardianships.accounts}
    @contextmanager
    def session(self,token,device):
        with self.transaction():identity=self._resolve(token,device,True)
        context=_CURRENT.set((self,identity,token,device))
        try:
            ctx=AdapterContext('identity-session',identity['user_id'],identity['device_id'],subject_id=identity['user_id'],authorization_ref=identity['session_id'])
            if not self.verify_subject(ctx,token).ok or not self.assess(ctx,device).ok:raise PermissionError('identity/device provider denied')
            service=getattr(self.rt,'bank_service',None)
            scope=(service.scope(service.context(identity['user_id'],device_id=identity['device_id'],trace_id=new_request_id('trace_identity')))
                   if service is not None and identity['role']=='user' and identity['user_id']=='demo-user' else nullcontext())
            with scope:yield identity
        finally:_CURRENT.reset(context)
    def current_identity(self):
        current=_CURRENT.get()
        if not current or current[0] is not self:return None
        with self.lock:
            try:return self._resolve(current[2],current[3])
            except PermissionError:return None
    def logout(self,token):
        with self.transaction():
            row=self.db.execute('SELECT user_id FROM identity_sessions WHERE session_id=?',(digest(token),)).fetchone()
            self.db.execute('UPDATE identity_sessions SET revoked=1 WHERE session_id=?',(digest(token),))
            if row:self.record('logout',row['user_id'])
    def revoke(self,user):
        with self.transaction():
            self.db.execute('UPDATE identity_accounts SET active=0,version=version+1 WHERE user_id=?',(user,));self.record('revoked',user)
    def rotate(self,user,password,secret=None):
        if not isinstance(password,str) or not 15<=len(password)<=128:raise ValueError('password must contain 15 to 128 characters')
        secret=secret or base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
        if not re.fullmatch('[A-Z2-7]{16,64}',secret):raise ValueError('invalid TOTP enrollment')
        salt=secrets.token_hex(16);hashed=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),self.ITERATIONS).hex()
        with self.transaction():
            updated=self.db.execute('UPDATE identity_accounts SET salt=?,password_hash=?,totp_secret=?,last_counter=-1,active=1,version=version+1,failures=0,locked_until=0 WHERE user_id=?',(salt,hashed,secret,user)).rowcount
            if updated!=1:raise ValueError('principal not found')
            self.record('rotated',user)
        return secret
    def _bound(self,ctx,banking=True):
        account=self.current_identity()
        return bool(account and account['user_id']==ctx.actor_id==ctx.subject_id and account['device_id']==ctx.device_id and account['session_id']==ctx.authorization_ref
                    and (not banking or account['role']=='user' and (account['user_id']=='demo-user' or account['user_id'] in self.rt.bank.guardianships.accounts)))
    def result(self,ctx,ok,code,data=None):return AdapterResult(bool(ok),code,'认证已核对' if ok else '认证未通过，请重新登录或核对操作',data or {},ctx.trace_id,new_request_id('identity'))
    def verify_subject(self,ctx,assertion):
        current=_CURRENT.get();ok=self._bound(ctx,False) and current and hmac.compare_digest(current[2],assertion or '')
        return self.result(ctx,ok,'IDENTITY_VERIFIED' if ok else 'IDENTITY_DENIED')
    def assess(self,ctx,device_attestation):
        current=_CURRENT.get();ok=self._bound(ctx,False) and current and hmac.compare_digest(current[3],device_attestation or '')
        return self.result(ctx,ok,'DEVICE_BOUND' if ok else 'DEVICE_DENIED',{'hardware_attestation':False,'risk_level':'bound_browser' if ok else 'unknown'})
    def begin(self,ctx,operation_digest,method):
        if method!='totp' or not self._bound(ctx) or not re.fullmatch('[0-9a-f]{64}',operation_digest):return self.result(ctx,False,'AUTH_BINDING_MISMATCH')
        cid=ctx.idempotency_key or new_request_id('challenge')
        with self.transaction():
            prior=self.db.execute('SELECT * FROM identity_challenges WHERE challenge_id=?',(cid,)).fetchone()
            if prior:return self.result(ctx,False,'AUTH_REPLAY')
            self.db.execute('DELETE FROM identity_challenges WHERE expires_at<?',(self.clock()-86400,))
            if self.db.execute('SELECT count(*) FROM identity_challenges').fetchone()[0]>=10000:return self.result(ctx,False,'AUTH_CAPACITY')
            expires=self.clock()+300
            self.db.execute('INSERT INTO identity_challenges(challenge_id,session_id,actor_id,subject_id,device_id,operation_digest,expires_at,trace_id) VALUES(?,?,?,?,?,?,?,?)',(cid,ctx.authorization_ref,ctx.actor_id,ctx.subject_id,ctx.device_id,operation_digest,expires,ctx.trace_id))
            self.record('challenge',ctx.actor_id,ctx.trace_id,challenge_id=cid,device_id=ctx.device_id,method='totp',operation_digest=operation_digest,expires_at=expires)
        return self.result(ctx,True,'AUTH_CHALLENGE',{'challenge_id':cid,'method':'totp','expires_at':expires})
    def check(self,ctx,cid,operation_digest,require_passed=False):
        if not self._bound(ctx):return 'AUTH_BINDING_MISMATCH'
        row=self.db.execute('SELECT * FROM identity_challenges WHERE challenge_id=?',(cid,)).fetchone()
        if not row:return 'AUTH_REQUIRED'
        if (row['session_id'],row['actor_id'],row['subject_id'],row['device_id'],row['operation_digest'])!=(ctx.authorization_ref,ctx.actor_id,ctx.subject_id,ctx.device_id,operation_digest):return 'AUTH_BINDING_MISMATCH'
        if row['used_at'] is not None:return 'AUTH_REPLAY'
        if self.clock()>=row['expires_at']:return 'AUTH_EXPIRED'
        if row['attempts']>=3:return 'AUTH_LOCKED'
        if require_passed and not row['passed']:return 'AUTH_REQUIRED'
        return 'OK'
    def verify(self,ctx,challenge_id,assertion,operation_digest):
        with self.transaction():
            status=self.check(ctx,challenge_id,operation_digest)
            if status!='OK':return self.result(ctx,False,status)
            row=self.db.execute('SELECT passed FROM identity_challenges WHERE challenge_id=?',(challenge_id,)).fetchone()
            if row['passed']:return self.result(ctx,False,'AUTH_REPLAY')
            account=self.db.execute('SELECT * FROM identity_accounts WHERE user_id=?',(ctx.actor_id,)).fetchone();counter=self._counter(account,assertion)
            if account['locked_until']>self.clock():return self.result(ctx,False,'AUTH_LOCKED')
            if counter is None:
                self.db.execute('UPDATE identity_challenges SET attempts=attempts+1 WHERE challenge_id=?',(challenge_id,));self.record('factor_denied',ctx.actor_id,ctx.trace_id,challenge_id=challenge_id)
                failures=account['failures']+1
                self.db.execute('UPDATE identity_accounts SET failures=?,locked_until=? WHERE user_id=?',(failures,self.clock()+self.LOCK_SECONDS if failures>=5 else 0,ctx.actor_id))
                return self.result(ctx,False,'AUTH_FAILED')
            self.db.execute('UPDATE identity_accounts SET last_counter=?,failures=0,locked_until=0 WHERE user_id=?',(counter,ctx.actor_id))
            self.db.execute('UPDATE identity_challenges SET passed=1 WHERE challenge_id=?',(challenge_id,))
            self.record('factor_verified',ctx.actor_id,ctx.trace_id,challenge_id=challenge_id,method='totp',operation_digest=operation_digest)
            return self.result(ctx,True,'AUTH_VERIFIED')
    def consume(self,ctx,challenge_id,operation_digest):
        with self.transaction():
            status=self.check(ctx,challenge_id,operation_digest,True)
            if status!='OK':return self.result(ctx,False,status)
            changed=self.db.execute('UPDATE identity_challenges SET used_at=? WHERE challenge_id=? AND used_at IS NULL AND passed=1',(self.clock(),challenge_id)).rowcount
            if changed!=1:return self.result(ctx,False,'AUTH_REPLAY')
            self.record('consumed',ctx.actor_id,ctx.trace_id,challenge_id=challenge_id,operation_digest=operation_digest,device_id=ctx.device_id)
            return self.result(ctx,True,'AUTH_CONSUMED')
    @staticmethod
    def cookies(headers):
        jar=SimpleCookie()
        try:jar.load(headers.get('Cookie',''))
        except Exception:return '', ''
        return tuple(jar[k].value if k in jar else '' for k in ('bank_session','bank_device'))
    def _set_cookies(self,h,token,device,delete=False):
        secure='; Secure' if self.public_origin.startswith('https:') else ''
        age=0 if delete else self.SESSION_TTL
        h._identity_response_cookies=[f'bank_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age={age}{secure}',f'bank_device={device}; HttpOnly; SameSite=Strict; Path=/; Max-Age={age}{secure}']
    def handle(self,h,callback):
        path=urlsplit(h.path).path;method=h.command;token,device=self.cookies(h.headers)
        if method=='GET' and path=='/api/v1/identity/capabilities':return h._send(200,{'ok':True,'mode':'SELF_HOSTED','methods':['password','totp'],'production_certified':False,'device_hardware_attestation':False,'family_delegation':False,'family_assistance':True,'family_owner_totp_required':True})
        if method=='GET' and path=='/identity/login':return h._send(200,LOGIN_HTML.encode(),'text/html; charset=utf-8')
        if method=='GET' and path=='/identity/client.js':return h._send(200,LOGIN_SCRIPT.encode(),'application/javascript; charset=utf-8')
        if method=='GET' and path.startswith(('/static/','/healthz','/health/live')):return callback()
        if method=='POST':
            expected=self.public_origin or 'http://'+h.headers.get('Host','')
            if h.headers.get('Origin','')!=expected or h.headers.get('Sec-Fetch-Site','same-origin') not in {'same-origin','none'}:
                return h._send(403,{'ok':False,'error_code':'ORIGIN_DENIED','message':'请从本机登录页面操作。'})
            if path=='/api/v1/identity/login':
                if h.headers.get('Content-Type','').split(';')[0]!='application/json':return h._send(400,{'ok':False})
                body=h._body()
                if set(body)!={'username','password','code'}:return h._send(400,{'ok':False})
                item=self.login(body['username'],body['password'],body['code'],device,token,h._client_key())
                if not item:return h._send(403,{'ok':False,'message':'登录未通过。请核对密码和当前未使用的动态码；多次失败需稍后重试。'})
                self._set_cookies(h,item['token'],item['device']);return h._send(200,{'ok':True,'message':'已登录；动态码只能使用一次，敏感操作请使用下一枚动态码。'})
        try:
            with self.session(token,device) as account:
                if path=='/api/v1/identity/me' and method=='GET':return h._send(200,{'ok':True,'user_id':account['user_id'],'device_id':account['device_id'],'role':account['role'],'method':'password_totp','production_certified':False})
                if path=='/api/v1/identity/logout' and method=='POST':
                    self.logout(token);self._set_cookies(h,'','',True);return h._send(200,{'ok':True})
                if path.startswith('/api/') and account['role']=='user' and account['user_id'] in self.rt.bank.guardianships.accounts:
                    reads={'/api/state','/api/capabilities','/api/audit','/api/v1/business-tasks','/api/v1/transactions/status'}
                    writes={'/api/chat','/api/confirm','/api/verify','/api/cancel','/api/v1/tasks/status','/api/v1/family/chat','/api/v1/family/review','/api/v1/family/permissions'}
                    allowed=(method=='GET' and (path in reads or path.startswith(('/api/v1/family/','/api/v1/business-tasks/','/api/v1/audit/')))) or (method=='POST' and (path in writes or path.startswith('/api/dag/')))
                    if not allowed:return h._send(403,{'ok':False,'error_code':'FAMILY_ROUTE_SCOPE','message':'该家庭账户尚未接入此功能，不能使用全局演示或默认账户入口。'})
                if path.startswith('/api/'):
                    operator=path.startswith(('/api/v1/handoff/','/api/v1/support/'))
                    if account['role']=='admin' and not operator:return h._send(403,{'ok':False,'message':'坐席身份没有银行业务权限。'})
                    if account['role']=='user' and account['user_id']!='demo-user' and account['user_id'] not in self.rt.bank.guardianships.accounts:return h._send(403,{'ok':False,'message':'当前主体没有绑定可办理的银行账户。'})
                if path=='/support/operator' and account['role']!='admin':return h._send(403,{'ok':False,'message':'需要坐席身份。'})
                return callback()
        except PermissionError:
            with self.transaction():self.record('session_denied','anonymous',reason='session_device_or_authorization_invalid')
            if method=='GET' and path in {'/','/support/operator'}:
                h.send_response(303);h.send_header('Location','/identity/login');h.send_header('Cache-Control','no-store');h.send_header('Content-Length','0');h.end_headers();return
            return h._send(403,{'ok':False,'error_code':'LOGIN_REQUIRED','message':'请重新登录，原业务不会自动重做。'})


LOGIN_HTML='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>银伴 · 身份核验</title><style>body{font:18px/1.7 system-ui;background:#f4f7f1;color:#17251d;max-width:600px;margin:40px auto;padding:20px}h1,h2{color:#000;font-weight:800}label,input,button{display:block;box-sizing:border-box;width:100%;margin:12px 0;padding:10px}button{background:#156052;color:white;border:0;border-radius:8px}a{color:#156052}</style><h1>本机身份核验</h1><p>密码与身份验证器动态码。本机自建服务，未取得银行机构或生产认证。不要输入银行密码、银行验证码或模型密钥。</p><form id="identityLogin"><label>本机用户名<input id="identityUser" required autocomplete="username" maxlength="64"></label><label>本机密码<input id="identityPassword" required type="password" autocomplete="current-password" maxlength="128"></label><label>身份验证器动态码<input id="identityCode" required inputmode="numeric" pattern="[0-9]{6}" autocomplete="one-time-code" maxlength="6"></label><button id="identitySubmit">登录并核验</button></form><p id="identityNotice" role="status"></p><a href="/">进入用户端</a> · <a href="/support/operator">进入坐席端</a><script src="/identity/client.js"></script></html>'''
LOGIN_SCRIPT=r'''document.querySelector('#identityLogin').addEventListener('submit',async e=>{e.preventDefault();const b=document.querySelector('#identitySubmit');b.disabled=true;try{const r=await fetch('/api/v1/identity/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.querySelector('#identityUser').value,password:document.querySelector('#identityPassword').value,code:document.querySelector('#identityCode').value})});const d=await r.json();document.querySelector('#identityNotice').textContent=d.message||'请求未确认';if(d.ok){document.querySelector('#identityPassword').value='';document.querySelector('#identityCode').value='';const me=await(await fetch('/api/v1/identity/me')).json();location.assign(me.role==='admin'?'/support/operator':'/');}}catch(_){document.querySelector('#identityNotice').textContent='结果未确认，请核对本机连接；未办理业务。';}finally{b.disabled=false;}});'''

def main():
    parser=argparse.ArgumentParser(description='Owned local identity provision/serve/revoke; no external IAM or production claim')
    parser.add_argument('action',choices=['provision','rotate','serve','revoke']);parser.add_argument('--directory',type=Path,required=True);parser.add_argument('--user',default='demo-user');parser.add_argument('--role',choices=['user','admin'],default='user');parser.add_argument('--port',type=int,default=8090)
    args=parser.parse_args();folder=args.directory.resolve()
    if not folder.is_relative_to(ROOT/'data') or not 1<=args.port<=65535:raise ValueError('owned data directory and valid port required')
    folder.mkdir(parents=True,exist_ok=True);os.environ.update(IDENTITY_MODE='self-hosted',BANK_AGENT_OFFLINE='1',BANK_AGENT_DB_PATH=str(folder/'identity.sqlite3'),IDENTITY_PUBLIC_ORIGIN='http://127.0.0.1:'+str(args.port))
    import banking_agent as ba
    rt=ba.runtime
    try:
        if args.action in {'provision','rotate'}:
            import getpass
            password=getpass.getpass('New local password (15-128 characters): ')
            if password!=getpass.getpass('Repeat password: '):raise ValueError('passwords differ')
            secret=rt.identity_service.provision(args.user,password,args.role) if args.action=='provision' else rt.identity_service.rotate(args.user,password)
            p=folder/(args.user+'-enrollment.json');p.write_text(json.dumps({'user_id':args.user,'issuer':'LanBankLocal','secret':secret,'algorithm':'SHA1','digits':6,'period':30,'scope':'private local authenticator enrollment; never publish'}),encoding='utf-8');p.chmod(0o600);print('Enrollment file saved locally; import into your authenticator, do not publish: '+str(p))
        elif args.action=='revoke':rt.identity_service.revoke(args.user);print('Principal revoked, existing sessions invalidated')
        else:
            host='127.0.0.1'
            server=ba.ThreadingHTTPServer((host,args.port),ba.Handler);print('Self-hosted identity: http://127.0.0.1:'+str(args.port)+'/identity/login',flush=True)
            try:server.serve_forever()
            finally:server.server_close()
    finally:rt.recovery_scheduler.stop();rt.bank.close();rt.audit.db.close()

if __name__=='__main__':main()

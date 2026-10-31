#!/usr/bin/env python3
"""Human-operated physical microphone/ASR acceptance. No fake device or auto-send.

Only a fixed, reviewed <=10s phrase can be submitted, at most twice. Audio stays
in request/browser memory; report contains metadata and match metrics, not audio.
--allow-cloud requires the owner's prior authorization of this exact phrase.
"""
import argparse,hashlib,io,json,math,os,re,secrets,sys,tempfile,threading,time,unicodedata,wave
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
PHRASE='这是语音识别验收，仅生成草稿，不办理业务'


def phrase_metrics(text):
    def normalize(value):return ''.join(c for c in value if not c.isspace() and not unicodedata.category(c).startswith('P'))
    expected=normalize(PHRASE);actual=normalize(text);previous=list(range(len(actual)+1))
    for i,a in enumerate(expected,1):
        row=[i]
        for j,b in enumerate(actual,1):row.append(min(row[-1]+1,previous[j]+1,previous[j-1]+(a!=b)))
        previous=row
    errors=previous[-1]
    return {'expected_characters':len(expected),'edit_distance':errors,'cer':errors/len(expected),'target_cer_max':.10,'matched':errors/len(expected)<=.10}


def inspect_capture(audio):
    sys.path.insert(0,str(ROOT/'app'))
    from speech_adapter import prepare_audio
    evidence,normalized=prepare_audio(audio)
    if not 16000<=evidence['frames']<=160000:raise ValueError('capture must be 1 to 10 seconds')
    import array
    samples=array.array('h',normalized[44:])
    if sys.byteorder!='little':samples.byteswap()
    rms_ppm=math.isqrt(sum(v*v for v in samples)*10**12//len(samples))//32768
    if rms_ppm<1000:raise ValueError('signal too quiet; check microphone')
    return evidence,rms_ppm


def encoder_source():
    text=(ROOT/'app/static/app.js').read_text(encoding='utf-8')
    return text[text.index('function encodeVoiceWav('):text.index('async function beginRecording()')]


PAGE=r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>真实麦克风与ASR验收</title><style>body{font:17px/1.65 system-ui;background:#f4f7f5;color:#17231e;margin:24px}main{max-width:820px;margin:auto;background:white;padding:28px;border-radius:16px}h1,h2{color:#000}button{padding:12px 18px;margin:8px;border-radius:8px;border:1px solid #28664e;cursor:pointer}button:disabled{opacity:.45}strong{color:#21694e}textarea{width:95%;min-height:130px}pre{white-space:pre-wrap;word-break:break-word}label{display:block;margin:14px 0}</style>
<main><h1>真实麦克风 / 云端ASR验收</h1><p>本页不会办理银行业务。只朗读下面固定短句，避免录入背景人声、个人资料或密钥。<strong>先本地录制、试听，再自行决定是否外发。</strong></p>
<h2 id="phrase">__PHRASE__</h2><label><input id="localConsent" type="checkbox">同意本次本机录音与字节摘要检查</label>
<button id="record">开始真实麦克风录音</button><button id="stop" disabled>停止（最长10秒）</button><button id="clear">取消并清理</button><p id="state" role="status">尚未录音；浏览器会请求麦克风权限。</p><audio id="preview" controls></audio>
<label><input id="review" type="checkbox">我已试听：只有上述固定短句，没有无关人声或隐私，允许把此录音发送到SiliconFlow（最多2次）</label>
<button id="cloud" disabled>发送这段录音到真实ASR</button><p>真实云端调用需已获批准且配置密钥，可能产生少量费用；不会自动重试。</p>
<pre id="result"></pre><button id="deletion" disabled>生成供应商处理/删除申请草稿（不发送）</button><textarea id="draft" readonly aria-label="未发送的供应商申请草稿"></textarea>
<label><input id="human" type="checkbox">我使用了真实麦克风，并已人工核对本页显示的识别结果</label><button id="finish">保存验收元数据并结束</button>
<p>录音不写文件；只保存摘要、时长、信号指标、供应商请求号与匹配指标。供应商声明不等于独立删除回执。</p></main>
<script>
__ENCODER__
const nonce=location.hash.slice(1),$=s=>document.querySelector(s);let stream,context,source,processor,gain,timer,blob=null,url=null,chunks=[],frames=0,lastTrace=null,busy=false;
const allowed=__ALLOW_CLOUD__;
function enable(){ $('#cloud').disabled=!allowed || !blob || !$('#review').checked || busy; }
async function request(path,body,binary=false){const r=await fetch(path,{method:'POST',headers:{'X-Voice-QA':nonce,'Content-Type':binary?'audio/wav':'application/json','X-QA-Consent':$('#localConsent').checked?'yes':'no','X-QA-Reviewed':$('#review').checked?'yes':'no'},body:binary?body:JSON.stringify(body)});const d=await r.json();if(!r.ok)throw Error(d.message || d.reason || '请求失败');return d}
function release(){clearTimeout(timer);if(processor){processor.onaudioprocess=null;processor.disconnect();source.disconnect();gain.disconnect()}stream?.getTracks().forEach(t=>t.stop());context?.close().catch(()=>{});processor=null;stream=null;}
function zero(){for(const c of chunks)c.fill(0);chunks=[];frames=0;}
function clear(){release();zero();blob=null;if(url)URL.revokeObjectURL(url);url=null;$('#preview').removeAttribute('src');$('#review').checked=false;$('#stop').disabled=true;$('#record').disabled=false;enable();}
$('#record').onclick=async()=>{if(!$('#localConsent').checked)return alert('请先同意本机录音');clear();try{context=new AudioContext();await context.resume();stream=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:true,noiseSuppression:true}});source=context.createMediaStreamSource(stream);processor=context.createScriptProcessor(4096,1,1);gain=context.createGain();gain.gain.value=0;processor.onaudioprocess=e=>{const remaining=Math.max(0,context.sampleRate*10-frames);const c=new Float32Array(e.inputBuffer.getChannelData(0).subarray(0,remaining));chunks.push(c);frames+=c.length;$('#state').textContent='真实设备录音中：'+(frames/context.sampleRate).toFixed(1)+'秒';if(frames>=context.sampleRate*10)stop()};source.connect(processor);processor.connect(gain);gain.connect(context.destination);timer=setTimeout(stop,10000);$('#stop').disabled=false;$('#record').disabled=true;}catch(e){clear();$('#state').textContent='麦克风未通过：'+e.name;}}
async function stop(){if(!processor)return;const rate=context.sampleRate;blob=encodeVoiceWav(chunks,rate);release();zero();$('#stop').disabled=true;$('#record').disabled=false;url=URL.createObjectURL(blob);$('#preview').src=url;try{const d=await request('/qa/local',blob,true);$('#state').textContent='本地检查通过，请先试听。尚未外发。';$('#result').textContent=JSON.stringify(d,null,2);enable()}catch(e){clear();$('#state').textContent=e.message}}
$('#stop').onclick=stop;$('#clear').onclick=()=>{clear();$('#state').textContent='录音引用和采样已清理；已外发数据不能由本页撤回。'};$('#review').onchange=enable;
$('#localConsent').onchange=()=>{if(!$('#localConsent').checked)clear()};
$('#cloud').onclick=async()=>{if($('#cloud').disabled)return;busy=true;enable();try{const d=await request('/qa/cloud',blob,true);lastTrace=d.trace_id;$('#result').textContent=JSON.stringify(d,null,2);$('#deletion').disabled=!d.provider_receipt?.request_id;$('#state').textContent=d.ok?'真实ASR已返回，请人工核对。':'真实ASR未通过；没有自动重试。'}catch(e){$('#state').textContent=e.message}finally{busy=false;$('#review').checked=false;enable()}};
$('#deletion').onclick=async()=>{try{const d=await request('/qa/deletion',{trace_id:lastTrace});$('#draft').value=d.draft || d.message}catch(e){$('#draft').value=e.message}};
$('#finish').onclick=async()=>{try{const d=await request('/qa/finish',{human_checked:$('#human').checked});clear();$('#state').textContent=d.message}catch(e){$('#state').textContent=e.message}};
window.addEventListener('pagehide',clear);
</script></html>'''


class AcceptanceState:
    def __init__(self,allow_cloud=False):
        self.allow_cloud=allow_cloud;self.lock=threading.Lock();self.calls=0;self.local=None;self.cloud=None;self.human=False
    def permit(self,audio,reviewed):
        evidence,_=inspect_capture(audio)
        with self.lock:
            if not self.allow_cloud or reviewed is not True:raise PermissionError('specific cloud authorization and review required')
            if not self.local or evidence['upload_sha256']!=self.local['audio_evidence']['upload_sha256']:raise PermissionError('recording changed; review locally again')
            if self.calls>=2:raise PermissionError('two-call acceptance budget exhausted')
            self.calls+=1
        return evidence


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--allow-cloud',action='store_true');p.add_argument('--output',type=Path,required=True);p.add_argument('--timeout-seconds',type=int,default=1200);a=p.parse_args()
    output=a.output.resolve();output.relative_to(ROOT/'data/verification')
    if not 60<=a.timeout_seconds<=3600:raise ValueError('invalid acceptance timeout')
    output.parent.mkdir(parents=True,exist_ok=True)
    os.environ.update(VOICE_ENABLED='1' if a.allow_cloud else '0',BANK_AGENT_OFFLINE='0' if a.allow_cloud else '1')
    sys.path.insert(0,str(ROOT/'app'))
    with tempfile.TemporaryDirectory(prefix='physical-voice-') as tmp:
        os.environ['BANK_AGENT_DB_PATH']=str(Path(tmp)/'qa.sqlite3')
        import banking_agent as ba
        from speech_adapter import provider_retention_info
        original=ba.runtime
        rt=ba.AgentRuntime(db_path=Path(tmp)/'app.sqlite3',offline=True);ba.runtime=rt;ba.gateway=ba.AgentGateway(rt)
        if a.allow_cloud and not ba.SpeechAdapter().enabled:raise ValueError('ASR environment not configured; no key is printed')
        nonce=secrets.token_urlsafe(32);state=AcceptanceState(a.allow_cloud);done=threading.Event()
        def save():
            cloud_pass=bool(state.cloud and state.cloud.get('ok') and state.cloud.get('phrase_metrics',{}).get('matched') and state.human)
            report={'scope':'human-operated native microphone; human attestation, not device certification','expected_phrase':PHRASE,'cloud_calls':state.calls,
                'physical_microphone':'USER_VERIFIED' if state.local and state.human else 'PENDING','cloud_asr':'PASS' if cloud_pass else 'PENDING_OR_FAILED',
                'local':state.local,'cloud':state.cloud,'human_checked':state.human,'provider_retention':provider_retention_info(),
                'audio_persisted':False,'actual_bank_business_executed':False}
            output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        class Handler(ba.Handler):
            def setup(self):
                super().setup()
                self.connection.settimeout(30)  # bound idle browser preconnects at shutdown
            def log_message(self,*args):pass
            def do_GET(self):
                if self.path!='/':return self._send(404,{'ok':False})
                body=PAGE.replace('__ENCODER__',encoder_source()).replace('__PHRASE__',PHRASE).replace('__ALLOW_CLOUD__','true' if a.allow_cloud else 'false').encode('utf-8')
                self.send_response(200);self.send_header('Content-Type','text/html; charset=utf-8');self.send_header('Cache-Control','no-store');self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'");self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def do_POST(self):
                self.close_connection=True
                if not secrets.compare_digest(self.headers.get('X-Voice-QA',''),nonce):return self._send(403,{'ok':False,'message':'验收会话无效'})
                try:
                    n=int(self.headers.get('Content-Length','0'))
                    if self.headers.get('Transfer-Encoding') or not 0<n<=2097152:raise ValueError('invalid request size')
                    self.connection.settimeout(10);raw=self.rfile.read(n)
                    if len(raw)!=n:raise ValueError('incomplete body')
                    if self.path in {'/qa/local','/qa/cloud'}:
                        if self.headers.get('X-QA-Consent')!='yes':raise PermissionError('local consent required')
                        evidence,rms=inspect_capture(raw)
                        cloud=self.path=='/qa/cloud'
                        if cloud:state.permit(raw,self.headers.get('X-QA-Reviewed')=='yes')
                        h=object.__new__(ba.Handler);h.headers={'Authorization':'Bearer demo-user-session','Content-Type':'audio/wav','Content-Length':str(len(raw)),'X-Audio-Consent':'yes'}
                        if cloud:h.headers['X-Audio-Cloud-Consent']='yes'
                        h.rfile=io.BytesIO(raw);h._send=lambda status,result:(status,result)
                        status,result=h._transcribe_voice()
                        if cloud:
                            metrics=phrase_metrics(result.get('text',''))
                            state.cloud={k:result[k] for k in ('ok','reason','trace_id','audio_evidence','provider_receipt') if k in result}
                            state.cloud['phrase_metrics']=metrics
                            result=dict(result,phrase_metrics=metrics)
                        else:state.local={'trace_id':result['trace_id'],'audio_evidence':evidence,'rms_ppm':rms,'capture_method':'native_getUserMedia_user_operated'}
                        save();return self._send(status,result)
                    body=json.loads(raw)
                    if not isinstance(body,dict):raise ValueError('request must be an object')
                    if self.path=='/qa/deletion':return self._send(200,rt.voice_deletion_request(body.get('trace_id'),'demo-user','demo-device'))
                    if self.path=='/qa/finish':
                        state.human=body.get('human_checked') is True;save();done.set()
                        return self._send(200,{'ok':True,'message':'验收元数据已保存，音频未落盘。可关闭此页。'})
                    return self._send(404,{'ok':False})
                except (ValueError,PermissionError,TimeoutError,OSError) as e:return self._send(400,{'ok':False,'message':str(e)[:180]})
        server=ba.ThreadingHTTPServer(('127.0.0.1',0),Handler);server.daemon_threads=False
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        print('PHYSICAL_MIC_URL: http://127.0.0.1:'+str(server.server_port)+'/#'+nonce,flush=True);save()
        try:
            deadline=time.monotonic()+a.timeout_seconds
            while not done.wait(1) and time.monotonic()<deadline:pass
        finally:
            save();server.shutdown();server.server_close();thread.join(timeout=5)
            for item in (rt,original):item.recovery_scheduler.stop();item.bank.close();item.audit.db.close()
        print('ACCEPTANCE_REPORT:',str(output),flush=True)


if __name__=='__main__':main()

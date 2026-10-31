"""Voice is untrusted draft input, never a payment instruction by itself."""
import io
import http.client
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import wave
import hashlib
import struct
from pathlib import Path
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import banking_agent as ba
from speech_adapter import SpeechAdapter, MAX_AUDIO_BYTES, NoRedirect


def wav(seconds=1):
    out = io.BytesIO()
    with wave.open(out, 'wb') as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(16000)
        f.writeframes(b'\0\0' * (16000 * seconds))
    return out.getvalue()


class VoiceTests(unittest.TestCase):
    def test_physical_acceptance_tool_rejects_silence_unreviewed_and_extra_calls(self):
        sys.path.insert(0,str(ROOT/'scripts'))
        from voice_acceptance import AcceptanceState,inspect_capture,phrase_metrics,PHRASE
        raw=wav();audible=raw[:44]+struct.pack('<h',4096)*16000
        with self.assertRaises(ValueError):inspect_capture(raw)
        evidence,rms=inspect_capture(audible);self.assertGreater(rms,1000)
        self.assertTrue(phrase_metrics(PHRASE)['matched']);self.assertFalse(phrase_metrics('无关内容')['matched'])
        state=AcceptanceState(False);state.local={'audio_evidence':evidence}
        with self.assertRaises(PermissionError):state.permit(audible,True)
        state.allow_cloud=True
        with self.assertRaises(PermissionError):state.permit(audible,False)
        state.permit(audible,True);state.permit(audible,True)
        with self.assertRaises(PermissionError):state.permit(audible,True)
    def test_provider_manual_request_is_owned_audited_and_never_sent(self):
        from speech_adapter import prepare_audio
        with tempfile.TemporaryDirectory() as tmp:
            rt=ba.AgentRuntime(db_path=Path(tmp)/'deletion.sqlite3',offline=True)
            try:
                trace='trace_'+'b'*32;request='asr_'+'a'*32;e,_=prepare_audio(wav())
                rt.audit.write(trace,'voice.transcribed',{'user_id':'demo-user','device_id':'demo-device','outbound_attempted':True,
                    'audio_evidence':ba.voice_audit_evidence(e),'provider_receipt':ba.voice_audit_evidence({'provider_request_id':request})},actor='demo-user')
                with patch('speech_adapter.urllib.request.build_opener') as network:
                    self.assertFalse(rt.voice_deletion_request(trace,'other','demo-device')['ok'])
                    self.assertFalse(rt.voice_deletion_request(trace,'demo-user','other')['ok'])
                    out=rt.voice_deletion_request(trace,'demo-user','demo-device')
                    self.assertEqual(out['status'],'MANUAL_REQUEST_DRAFT');self.assertEqual(out['delivery'],'NOT_SENT')
                    self.assertFalse(out['deletion_verified']);self.assertIn(request,out['draft']);network.assert_not_called()
                self.assertTrue(rt.audit.verify_trace(trace)['valid'])
                local='trace_'+'c'*32
                rt.audit.write(local,'voice.evidenced',{'user_id':'demo-user','device_id':'demo-device','outbound_attempted':False},actor='demo-user')
                self.assertEqual(rt.voice_deletion_request(local,'demo-user','demo-device')['status'],'LOCAL_ONLY_NO_PROVIDER_UPLOAD')
            finally:rt.recovery_scheduler.stop();rt.bank.close();rt.audit.db.close()
    def test_retention_reports_asr_scope_without_calling_voice_style_deletion(self):
        from speech_adapter import provider_retention_info
        info=provider_retention_info()
        self.assertEqual(info['record_delete_api'],'NOT_DOCUMENTED')
        self.assertEqual(info['deletion_verified'],False)
        self.assertEqual(info['scope'],'ASR_REQUEST')
        self.assertEqual(info['manual_contact'],'contact@siliconflow.cn')

    def test_real_provider_trace_header_is_bound_and_echo_verified(self):
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock()
        response.read.return_value=b'{"text":"fixed phrase"}';response.status=200
        def respond(request,timeout):
            response.headers={'x-siliconcloud-trace-id':request.get_header('X-trace-id')}
            return response
        opener=Mock();opener.open.side_effect=respond
        with patch('speech_adapter.urllib.request.build_opener',return_value=opener):out=self.adapter().transcribe(wav())
        receipt=out['provider_receipt']
        self.assertTrue(receipt['response_trace_matches'])
        self.assertEqual(receipt['request_id'],opener.open.call_args.args[0].get_header('X-trace-id'))
        self.assertFalse(receipt['deletion_verified'])
    def test_live_http_evidence_only_review_link_and_original_permission_chain(self):
        import threading
        import urllib.request
        with tempfile.TemporaryDirectory() as tmp:
            rt=ba.AgentRuntime(db_path=Path(tmp)/'voice.sqlite3',offline=True)
            class Quiet(ba.Handler):
                def log_message(self,*args):pass
            server=ba.ThreadingHTTPServer(('127.0.0.1',0),Quiet)
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            def post(path,body,*,cloud=False,token='demo-user-session'):
                binary=isinstance(body,bytes)
                h={'Authorization':'Bearer '+token,'Content-Type':'audio/wav' if binary else 'application/json'}
                if binary:h['X-Audio-Consent']='yes'
                if cloud:h['X-Audio-Cloud-Consent']='yes'
                req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}'+path,
                    data=body if binary else json.dumps(body).encode(),headers=h)
                try:
                    with opener.open(req,timeout=10) as r:return r.status,json.load(r)
                except urllib.error.HTTPError as e:
                    try:return e.code,json.load(e)
                    finally:e.close()
            try:
                with patch.object(ba,'runtime',rt),patch.object(ba,'gateway',ba.AgentGateway(rt)),patch.object(ba.http_rate_limiter,'allow',return_value=True),patch.dict(os.environ,{'BANK_AGENT_OFFLINE':'0','VOICE_ENABLED':'1','LLM_PROVIDER':'siliconflow','LLM_API_KEY':'fixture'}),patch.object(ba.SpeechAdapter,'transcribe',return_value={'ok':True,'text':'给妈妈转1元','source':'siliconflow_asr'}) as asr:
                    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                    before=rt.bank.balance().data['available_cents']
                    code,local=post('/api/v1/voice/transcribe',wav())
                    self.assertEqual(code,200);self.assertEqual(local['mode'],'local_evidence_only')
                    self.assertNotIn('voice_source_token',local);asr.assert_not_called()
                    with patch.dict(os.environ,{'BANK_AGENT_OFFLINE':'1'}):
                        self.assertEqual(post('/api/v1/voice/transcribe',wav(),cloud=True)[1]['mode'],'local_evidence_only')
                    asr.assert_not_called()
                    code,v=post('/api/v1/voice/transcribe',wav(),cloud=True);self.assertEqual(code,200)
                    self.assertEqual(v['audio_evidence']['upload_sha256'],hashlib.sha256(wav()).hexdigest())
                    self.assertFalse(rt.pending);self.assertEqual(rt.bank.balance().data['available_cents'],before)
                    body={'message':v['text'],'voice_source_token':v['voice_source_token']}
                    self.assertEqual(post('/api/chat',body)[0],403)
                    self.assertEqual(post('/api/chat',dict(body,voice_reviewed=True),token='demo-daughter-session')[0],403)
                    code,plan=post('/api/chat',dict(body,voice_reviewed=True));self.assertEqual(code,200)
                    self.assertTrue(plan['plan_view']['requires_confirmation'])
                    self.assertEqual(rt.bank.balance().data['available_cents'],before)
                    events=rt.audit.list_for_trace(plan['trace_id']);links=[e for e in events if e['event_type']=='voice.linked']
                    self.assertEqual(len(links),1);self.assertEqual(links[0]['payload']['source_trace_id'],v['trace_id'])
                    self.assertEqual(links[0]['payload']['pcm_sha256'],v['audio_evidence']['pcm_sha256'])
                    self.assertTrue(rt.audit.verify_trace(plan['trace_id'])['valid'])
                    voice_events=rt.audit.list_for_trace(v['trace_id'])
                    self.assertEqual(voice_events[-1]['payload']['audio_evidence'],v['audio_evidence'])
                    self.assertNotIn(v['text'],json.dumps(voice_events,ensure_ascii=False))
                    self.assertNotIn('RIFF',json.dumps(voice_events))
                    self.assertTrue(rt.audit.verify_trace(v['trace_id'])['valid'])
                    code,out=post('/api/confirm',{'plan_id':plan['plan']['plan_id'],'action_token':plan['plan_view']['action_token']})
                    self.assertEqual(code,200);self.assertEqual(out['result']['status'],'SUCCESS')
                    self.assertEqual(rt.bank.balance().data['available_cents'],before-100)
                    self.assertEqual(post('/api/chat',dict(body,voice_reviewed=True))[0],403)
                    self.assertEqual(rt.bank.balance().data['available_cents'],before-100)
                    asr.assert_called_once()
                    self.assertEqual([p for p in Path(tmp).rglob('*') if p.suffix in {'.wav','.pcm','.webm'}],[])
            finally:
                server.shutdown();server.server_close();thread.join(timeout=5)
                rt.recovery_scheduler.stop();rt.bank.close();rt.audit.db.close()

    def test_untrusted_same_named_audit_fields_still_redact_sensitive_digits(self):
        number='13800138000';value='a'*20+number+'f'*33
        clean=ba.sanitize_audit_value({'pcm_sha256':value,'source_trace_id':'trace_'+number})
        self.assertNotIn(number,json.dumps(clean))
    def test_provider_upload_drops_metadata_and_retains_original_digest(self):
        raw=wav();extra=b'JUNK'+struct.pack('<I',4)+b'meta'
        decorated=raw[:4]+struct.pack('<I',len(raw)+len(extra)-8)+raw[8:12]+extra+raw[12:]
        response=Mock();response.__enter__=Mock(return_value=response);response.__exit__=Mock()
        response.read.return_value=b'{"text":"draft"}';opener=Mock();opener.open.return_value=response
        with patch('speech_adapter.urllib.request.build_opener',return_value=opener):out=self.adapter().transcribe(decorated)
        self.assertNotIn(extra,opener.open.call_args.args[0].data)
        self.assertIn(raw,opener.open.call_args.args[0].data)
        self.assertEqual(out['audio_evidence']['upload_sha256'],hashlib.sha256(decorated).hexdigest())

    def test_voice_evidence_budget_prunes_expired_metadata(self):
        from speech_adapter import VoiceEvidenceStore,prepare_audio
        now=[10];s=VoiceEvidenceStore(clock=lambda:now[0]);e,_=prepare_audio(wav())
        for i in range(64):s.issue(e,'owner','device',str(i))
        with self.assertRaises(ValueError):s.issue(e,'owner','device','overflow')
        now[0]+=301;s.issue(e,'owner','device','new');self.assertEqual(len(s._items),1)
    def test_audio_evidence_distinguishes_upload_pcm_and_canonical_container(self):
        from speech_adapter import prepare_audio
        raw=wav();canonical=raw
        extra=b'JUNK'+struct.pack('<I',4)+b'meta'
        decorated=raw[:4]+struct.pack('<I',len(raw)+len(extra)-8)+raw[8:12]+extra+raw[12:]
        e1,c1=prepare_audio(raw);e2,c2=prepare_audio(decorated)
        self.assertEqual(c1,canonical);self.assertEqual(c1,c2)
        self.assertNotEqual(e1['upload_sha256'],e2['upload_sha256'])
        self.assertEqual(e1['pcm_sha256'],e2['pcm_sha256'])
        self.assertEqual(e1['normalized_wav_sha256'],hashlib.sha256(c1).hexdigest())
        self.assertEqual(e1['frames'],16000);self.assertEqual(e1['duration_ms'],1000)
        self.assertNotIn('meta',str(e2))
        e3,_=prepare_audio(raw[:-2]+b'\x01\x00');self.assertNotEqual(e1['pcm_sha256'],e3['pcm_sha256'])
        wrong_rate=bytearray(raw);struct.pack_into('<I',wrong_rate,24,8000)
        wrong_channels=bytearray(raw);struct.pack_into('<H',wrong_channels,22,2)
        for bad in [raw+b'extra',raw[:-1],b'bad',wav(31),wav(0),bytes(wrong_rate),bytes(wrong_channels)]:
            with self.assertRaises(ValueError):prepare_audio(bad)

    def test_voice_evidence_token_is_metadata_only_bound_expiring_and_single_use(self):
        from speech_adapter import VoiceEvidenceStore,prepare_audio
        now=[100.];store=VoiceEvidenceStore(clock=lambda:now[0]);e,_=prepare_audio(wav())
        token=store.issue(e,'alice','phone','trace_voice')
        self.assertNotIn('RIFF',repr(store._items));self.assertNotIn('text',repr(store._items))
        with self.assertRaises(PermissionError):store.consume(token,'other','phone',reviewed=True)
        with self.assertRaises(PermissionError):store.consume(token,'alice','other',reviewed=True)
        with self.assertRaises(PermissionError):store.consume(token,'alice','phone',reviewed=False)
        self.assertEqual(store.consume(token,'alice','phone',reviewed=True)['source_trace_id'],'trace_voice')
        with self.assertRaises(PermissionError):store.consume(token,'alice','phone',reviewed=True)
        token=store.issue(e,'alice','phone','trace_voice');now[0]+=301
        with self.assertRaises(PermissionError):store.consume(token,'alice','phone',reviewed=True)
        from concurrent.futures import ThreadPoolExecutor
        token=store.issue(e,'alice','phone','trace_voice')
        def consume(_):
            try:store.consume(token,'alice','phone',reviewed=True);return True
            except PermissionError:return False
        with ThreadPoolExecutor(max_workers=4) as pool:self.assertEqual(sum(pool.map(consume,range(4))),1)
    def adapter(self, **extra):
        env = dict(LLM_PROVIDER='siliconflow', LLM_API_KEY='test-secret', VOICE_ENABLED='1')
        env.update(extra)
        with patch.dict(os.environ, env, clear=True):
            return SpeechAdapter()

    def test_missing_key_offline_and_invalid_provider_never_call_network(self):
        for overrides in ({'LLM_API_KEY': ''}, {'BANK_AGENT_OFFLINE': '1'},
                          {'VOICE_ENABLED': '0'}, {'LLM_PROVIDER': 'other'}):
            adapter = self.adapter(**overrides)
            with patch('speech_adapter.urllib.request.build_opener') as opener:
                self.assertFalse(adapter.transcribe(wav())['ok'])
                opener.assert_not_called()

    def test_bad_audio_and_duration_rejected_before_network(self):
        for audio in (b'', b'bad', wav(31), wav()[:-20], b'x' * (MAX_AUDIO_BYTES + 1)):
            with patch('speech_adapter.urllib.request.build_opener') as opener:
                self.assertFalse(self.adapter().transcribe(audio)['ok'])
                opener.assert_not_called()

    def test_no_redirect_and_capabilities_do_not_expose_key(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 307, '', {}, 'https://untrusted.invalid'))
        caps = self.adapter().capabilities()
        self.assertTrue(caps['enabled'])
        self.assertNotIn('test-secret', json.dumps(caps))

    def test_multipart_success_and_no_retry(self):
        response = Mock(); response.__enter__ = Mock(return_value=response); response.__exit__ = Mock()
        response.read.return_value = json.dumps({'text': '给妈妈转5000元'}).encode()
        opener = Mock(); opener.open.return_value = response
        with patch('speech_adapter.urllib.request.build_opener', return_value=opener):
            out = self.adapter().transcribe(wav())
        self.assertEqual(out['text'], '给妈妈转5000元')
        self.assertEqual(out['source'], 'siliconflow_asr')
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.siliconflow.cn/v1/audio/transcriptions')
        self.assertIn(b'FunAudioLLM/SenseVoiceSmall', request.data)
        self.assertIn(b'RIFF', request.data)
        self.assertNotIn('test-secret', json.dumps(out))

    def test_upstream_errors_are_safe_and_never_retried(self):
        for error in (TimeoutError('secret'), urllib.error.URLError('secret'),
                      urllib.error.HTTPError('x', 302, 'secret', {}, None),
                      http.client.IncompleteRead(b'secret'), http.client.BadStatusLine('secret')):
            opener = Mock(); opener.open.side_effect = error
            with patch('speech_adapter.urllib.request.build_opener', return_value=opener):
                out = self.adapter().transcribe(wav())
            self.assertFalse(out['ok']); self.assertNotIn('secret', json.dumps(out))
            opener.open.assert_called_once()
        for raw in (b'not json', b'[]', b'{"text":123}', b'{"text":""}', b'x' * 65537):
            response = Mock(); response.__enter__ = Mock(return_value=response); response.__exit__ = Mock()
            response.read.return_value = raw
            opener = Mock(); opener.open.return_value = response
            with patch('speech_adapter.urllib.request.build_opener', return_value=opener):
                self.assertFalse(self.adapter().transcribe(wav())['ok'])

    def test_http_auth_consent_limits_and_transcription_has_no_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = ba.AgentRuntime(db_path=Path(tmp)/'voice.db', offline=True)
            def post(headers, audio=wav()):
                h = object.__new__(ba.Handler); h.path = '/api/v1/voice/transcribe'
                h.headers = headers; h.rfile = io.BytesIO(audio)
                h._client_key = lambda: 'voice-test'; h._send = lambda status, data: (status, data)
                return h.do_POST()
            headers = {'Authorization': 'Bearer demo-user-session', 'Content-Type': 'audio/wav',
                       'Content-Length': str(len(wav())), 'X-Audio-Consent': 'yes','X-Audio-Cloud-Consent':'yes'}
            try:
                with patch.object(ba, 'runtime', rt), patch.object(ba.http_rate_limiter, 'allow', return_value=True), patch.object(ba.SpeechAdapter, 'transcribe') as transcribe, patch.dict(os.environ,{'BANK_AGENT_OFFLINE':'0','VOICE_ENABLED':'1','LLM_PROVIDER':'siliconflow','LLM_API_KEY':'test-secret'}):
                    transcribe.return_value = {'ok': True, 'text': '给妈妈转5000元', 'source': 'siliconflow_asr'}
                    self.assertEqual(post({})[0], 403)
                    self.assertEqual(post(dict(headers, **{'X-Audio-Consent': 'no'}))[0], 400)
                    self.assertEqual(post(dict(headers, **{'Content-Length': str(MAX_AUDIO_BYTES+1)}))[0], 413)
                    self.assertEqual(post(dict(headers, **{'Content-Length': '-1'}))[0], 400)
                    self.assertEqual(post(dict(headers, **{'Content-Type': 'text/plain'}))[0], 415)
                    transcribe.assert_not_called()
                    before = rt.bank.balance_cents
                    self.assertFalse(rt.pending)
                    status, out = post(headers)
                    self.assertEqual(status, 200); self.assertEqual(out['text'], '给妈妈转5000元')
                    self.assertEqual(out['audio_evidence']['upload_sha256'],hashlib.sha256(wav()).hexdigest())
                    self.assertTrue(out['voice_source_token'])
                    self.assertEqual(rt.bank.balance_cents, before)
                    self.assertFalse(rt.pending, '转写不可创建银行计划')
                    events = rt.audit.list_for_trace(out['trace_id'])
                    self.assertTrue(any(e['event_type'] == 'voice.transcribed' for e in events))
                    self.assertNotIn('给妈妈', json.dumps(events, ensure_ascii=False))
                    self.assertEqual(events[-1]['actor'], 'demo-user')
                    # Only explicitly submitting the draft enters the existing banking chain.
                    result = rt.chat(out['text'])
                    self.assertTrue(result['plan']['requires_confirmation'])
                    self.assertTrue(result['plan']['requires_strong_auth'])
                    self.assertEqual(rt.bank.balance_cents, before)
            finally:
                rt.bank.close(); rt.audit.db.close()


class VoiceBrowserTests(unittest.TestCase):
    def test_voice_review_send_cancel_and_responsive_layout(self):
        import subprocess
        import threading
        node = os.environ.get('C7_NODE_BINARY')
        module = os.environ.get('C7_PLAYWRIGHT_MODULE')
        if not node or not module:
            self.skipTest('需要显式配置 C7_NODE_BINARY / C7_PLAYWRIGHT_MODULE 才运行浏览器验证')
        script = r"""
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[]; page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>{
   window.stopped=0;
   Object.defineProperty(navigator.mediaDevices,'getUserMedia',{value:async()=>{
     if(window.denyMic) throw new DOMException('denied','NotAllowedError');
     return {getTracks:()=>[{stop:()=>window.stopped++}]};
   }});
   window.AudioContext=class {
     constructor(){this.sampleRate=16000;this.destination={};}
     async resume(){} async close(){}
     createMediaStreamSource(){return {connect(){},disconnect(){}};}
     createScriptProcessor(){const node={connect(){},disconnect(){}};
       setTimeout(()=>node.onaudioprocess?.({inputBuffer:{getChannelData:()=>new Float32Array(4096).fill(.25)}}),50);return node;}
     createGain(){return {gain:{value:0},connect(){},disconnect(){}};}
   };
 });
 let uploads=0, chats=0;
 await page.route('**/api/v1/voice/capabilities',r=>r.fulfill({json:{enabled:true}}));
 page.on('request',r=>{
   if(!r.url().endsWith('/api/v1/voice/transcribe'))return;
   uploads++; assert.equal(r.headers()['x-audio-consent'],'yes');
   assert.equal(r.postDataBuffer().toString('ascii',0,4),'RIFF');
 });
 page.on('request',r=>{if(r.url().endsWith('/api/chat'))chats++;});
 await page.goto(process.env.VOICE_TEST_URL);
 await page.locator('#voiceBtn').click();
 await page.locator('#voiceDialog').waitFor({state:'visible'});
 assert.equal(await page.locator('#voiceStart').isEnabled(),false);
 await page.locator('#voiceConsent').check();
 await page.locator('#voiceStart').click();
 await page.locator('#voiceStop').waitFor({state:'visible'});
 await page.waitForTimeout(150);
 await page.evaluate(()=>{window.savedVoiceCopy=voiceSession.chunks[0];if(!savedVoiceCopy.some(x=>x!==0))throw Error('missing audio fixture');});
 await page.locator('#voiceStop').click();
 await page.waitForFunction(()=>document.querySelector('#messageInput').value==='给妈妈转5000元');
 assert.equal(chats,0,'识别不得自动触发银行指令');assert.equal(uploads,1);
 assert.ok(await page.evaluate(()=>window.stopped>=1));
 assert.ok(await page.evaluate(()=>savedVoiceCopy.every(x=>x===0)),'uploaded samples must be cleared');
 await page.locator('#messageInput').fill('给妈妈转5000元');
 await page.locator('#sendBtn').click();
 await page.locator('#confirmBtn').waitFor();
 assert.equal(chats,1);
 assert.match(await page.locator('#planPanel').innerText(),/5,000|5000/);
 await page.locator('#voiceBtn').click();await page.locator('#voiceConsent').check();
 await page.locator('#voiceStart').click();await page.locator('#voiceStop').waitFor({state:'visible'});
 await page.waitForTimeout(150);await page.evaluate(()=>window.savedVoiceCopy=voiceSession.chunks[0]);
 await page.locator('#voiceCancel').click();assert.equal(uploads,1,'取消不上传');
 assert.ok(await page.evaluate(()=>savedVoiceCopy.every(x=>x===0)),'cancel must clear samples');
 await page.evaluate(()=>window.denyMic=true);
 await page.locator('#voiceBtn').click();await page.locator('#voiceConsent').check();await page.locator('#voiceStart').click();
 await page.waitForFunction(()=>document.querySelector('#voiceDialogStatus').textContent.includes('麦克风'));
 await page.locator('#voiceCancel').click();
 await page.route('**/api/v1/voice/capabilities',r=>r.fulfill({json:{enabled:false}}));
 await page.locator('#voiceBtn').click();
 await page.waitForFunction(()=>document.querySelector('#voiceDialogStatus').textContent.includes('未启用'));
 await page.locator('#voiceConsent').check();assert.equal(await page.locator('#voiceStart').isEnabled(),false);
 assert.equal(uploads,1);await page.locator('#voiceCancel').click();
 for(const elder of [true,false]){
   if((await page.locator('#elderToggle').getAttribute('aria-pressed'))!==String(elder))await page.locator('#elderToggle').click();
   for(const width of [1440,390]){
     await page.setViewportSize({width,height:1000});
     assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'横向溢出');
     if(elder)assert.ok(await page.locator('#messageInput').evaluate(e=>parseFloat(getComputedStyle(e).fontSize)>=18));
     if(process.env.VOICE_SCREENSHOT_DIR)await page.screenshot({path:process.env.VOICE_SCREENSHOT_DIR+'/voice-'+elder+'-'+width+'.png',fullPage:true});
   }
 }
 await page.evaluate(()=>window.denyMic=false);
 await page.route('**/api/v1/voice/capabilities',r=>r.fulfill({json:{enabled:false,evidence_enabled:true}}));
 await page.locator('#messageInput').fill('保留原草稿');
 await page.locator('#voiceBtn').click();await page.locator('#voiceConsent').check();await page.locator('#voiceStart').click();
 await page.locator('#voiceStop').waitFor({state:'visible'});await page.waitForTimeout(150);
 await page.evaluate(()=>window.savedVoiceCopy=voiceSession.chunks[0]);await page.locator('#voiceStop').click();
 await page.waitForFunction(()=>document.querySelector('#voiceDialogStatus').textContent.includes('摘要已生成'));
 assert.equal(await page.locator('#messageInput').inputValue(),'保留原草稿');assert.equal(uploads,2);assert.equal(chats,1);
 assert.ok(await page.evaluate(()=>savedVoiceCopy.every(x=>x===0)));
 await page.locator('#voiceCancel').click();
 await page.locator('#voiceBtn').click();await page.locator('#voiceConsent').check();await page.locator('#voiceStart').click();
 await page.locator('#voiceStop').waitFor({state:'visible'});await page.waitForTimeout(150);
 await page.evaluate(()=>window.savedVoiceCopy=voiceSession.chunks[0]);await page.locator('#voiceConsent').uncheck();
 await page.locator('#voiceDialog').waitFor({state:'hidden'});assert.equal(uploads,2);
 assert.ok(await page.evaluate(()=>savedVoiceCopy.every(x=>x===0)),'withdrawal must clear samples');
 assert.deepEqual(errors,[]);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        with tempfile.TemporaryDirectory() as tmp:
            rt = ba.AgentRuntime(db_path=Path(tmp)/'browser.db', offline=True)
            try:
                with patch.object(ba, 'runtime', rt), patch.object(ba, 'gateway', ba.AgentGateway(rt)), patch.object(ba.http_rate_limiter, 'allow', return_value=True),patch.dict(os.environ,{'BANK_AGENT_OFFLINE':'0','VOICE_ENABLED':'1','LLM_PROVIDER':'siliconflow','LLM_API_KEY':'fixture'}),patch.object(ba.SpeechAdapter,'transcribe',return_value={'ok':True,'text':'给妈妈转5000元','source':'siliconflow_asr'}):
                    server = ba.ThreadingHTTPServer(('127.0.0.1', 0), ba.Handler)
                    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                    try:
                        env = dict(os.environ, VOICE_TEST_URL='http://127.0.0.1:%d' % server.server_port)
                        run = subprocess.run([node, '-e', script], env=env, text=True, encoding='utf-8', capture_output=True, timeout=90)
                        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    finally:
                        server.shutdown(); server.server_close(); thread.join(timeout=5)
            finally:
                rt.bank.close(); rt.audit.db.close()


if __name__ == '__main__':
    unittest.main()

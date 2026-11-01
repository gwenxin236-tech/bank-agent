import os
import sys
import unittest
import subprocess
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from tts_output import TTSOutput, redact_speech


class TTSOutputTests(unittest.TestCase):
    def test_bundled_prompts_are_complete_public_and_hash_verified(self):
        import base64
        import hashlib
        import json
        sys.path.insert(0, str(ROOT / 'scripts'))
        from build_speech_prompts import PROMPTS, VOICES
        pack = json.loads((ROOT / 'app/static/speech-prompts.json').read_text(encoding='utf-8'))
        self.assertEqual(pack['rate'], 0)
        self.assertEqual(set(pack['voices']), set(VOICES))
        for voice in VOICES:
            self.assertEqual(set(pack['voices'][voice]), set(PROMPTS))
            for phrase, clip in pack['voices'][voice].items():
                self.assertFalse(any(c.isdigit() for c in phrase))
                audio = base64.b64decode(clip['mp3'], validate=True)
                self.assertGreater(len(audio), 1000)
                self.assertEqual(hashlib.sha256(audio).hexdigest(), clip['sha256'])

    def test_browser_shortcuts_play_locally_with_bounded_start_latency(self):
        import threading
        import banking_agent as ba
        from test_domain_snapshots import SnapshotFixture
        node, module = os.getenv('C7_NODE_BINARY'), os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:
            self.skipTest('Browser QA requires C7_NODE_BINARY and C7_PLAYWRIGHT_MODULE')
        fixture = SnapshotFixture(); fixture.setUp()
        script = r'''
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);const assert=require('node:assert/strict');
(async()=>{const b=await chromium.launch({headless:true});try{
 const p=await b.newPage();const errors=[];let synth=0;p.on('pageerror',e=>errors.push(e.message));
 await p.addInitScript(()=>{
   localStorage.setItem('focusMode','false');
   localStorage.setItem('bankSpeechPreferences.v1',JSON.stringify({enabled:true,cloud:true,voice:'zh-CN-XiaoxiaoNeural',rate:0}));
   window.starts=[];window.clickAt=0;window.localCalls=0;
   speechSynthesis.speak=()=>window.localCalls++;
   const RealAudio=window.Audio;
   window.Audio=class extends RealAudio{constructor(...args){super(...args);this.addEventListener('playing',()=>window.starts.push({ms:performance.now()-window.clickAt,rate:this.playbackRate,src:this.src}));}};
   document.addEventListener('click',e=>{if(e.target.closest('.quick'))window.clickAt=performance.now();},true);
 });
 await p.route('**/api/v1/voice/synthesize',r=>{synth++;return r.abort();});
 await p.goto(process.env.SPEECH_QA_URL);
 let count=0;
 for(const voice of ['zh-CN-XiaoxiaoNeural','zh-CN-YunxiNeural']){
   if(count){await p.locator('#settingsBtn').click();await p.locator('#speechVoice').selectOption(voice);await p.locator('#settingsClose').click();}
   for(const name of ['查询账户余额','发起转账','银行卡服务']){
     await p.getByRole('button',{name,exact:true}).click();count++;
     await p.waitForFunction(n=>window.starts.length===n,count);
     await p.waitForFunction(()=>!document.querySelector('#sendBtn').disabled);
     if(name==='发起转账'){await p.locator('#transferDialog').waitFor();await p.locator('#transferClose').click();}
   }
 }
 const starts=await p.evaluate(()=>window.starts);
 assert.ok(starts.every(x=>x.ms<1000),'local click-to-playing budget: '+JSON.stringify(starts));
 assert.ok(starts.every(x=>x.src.startsWith('blob:')));assert.equal(synth,0);assert.equal(await p.evaluate(()=>window.localCalls),0);
 // Decode every real clip and measure initial silence, not just mocked play().
 const clips=await p.evaluate(async()=>{
   const pack=await (await fetch('/static/speech-prompts.json')).json();const ctx=new AudioContext();const rows=[];
   try{for(const [voice,items] of Object.entries(pack.voices))for(const [text,item] of Object.entries(items)){
     const bytes=Uint8Array.from(atob(item.mp3),c=>c.charCodeAt(0));const decoded=await ctx.decodeAudioData(bytes.buffer);
     const channel=decoded.getChannelData(0);let first=channel.findIndex(x=>Math.abs(x)>.01);
     rows.push({voice,text,duration:decoded.duration,leading_ms:first*1000/decoded.sampleRate});
   }}finally{await ctx.close();}return rows;
 });
 assert.equal(clips.length,44);assert.ok(clips.every(x=>x.duration>.25 && x.duration<8 && x.leading_ms>=0 && x.leading_ms<350));
 assert.deepEqual(errors,[]);console.log(JSON.stringify({starts:starts.map(x=>Math.round(x.ms)),synthesis_requests:synth,max_leading_ms:Math.max(...clips.map(x=>x.leading_ms)),clips:clips.length}));
}finally{await b.close()}})().catch(e=>{console.error(e);process.exit(1)});
'''
        try:
            with patch.object(ba, 'runtime', fixture.rt), patch.object(ba, 'gateway', ba.AgentGateway(fixture.rt)), patch.object(ba.http_rate_limiter, 'allow', return_value=True):
                with ba.ThreadingHTTPServer(('127.0.0.1', 0), ba.Handler) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
                    try:
                        run = subprocess.run([node, '-e', script], env=dict(os.environ, SPEECH_QA_URL=f'http://127.0.0.1:{server.server_port}'), capture_output=True, text=True, encoding='utf-8', timeout=90)
                        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                        print('Local speech latency:', run.stdout.strip())
                    finally:
                        server.shutdown(); thread.join(timeout=5)
        finally:
            fixture.tearDown()

    def test_browser_remembers_voice_and_never_silently_downgrades(self):
        import threading
        import banking_agent as ba
        from test_domain_snapshots import SnapshotFixture
        node, module = os.getenv('C7_NODE_BINARY'), os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:
            self.skipTest('Browser QA requires C7_NODE_BINARY and C7_PLAYWRIGHT_MODULE')
        fixture=SnapshotFixture();fixture.setUp()
        script=r'''
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);const assert=require('node:assert/strict');
(async()=>{const b=await chromium.launch({headless:true});try{
 const p=await b.newPage();const errors=[];p.on('pageerror',e=>errors.push(e.message));let available=true;const requests=[];
 await p.addInitScript(()=>{window.localSpeechCalls=0;window.speechSynthesis.getVoices=()=>[{lang:'zh-CN',localService:true,name:'Local'}];window.speechSynthesis.speak=()=>window.localSpeechCalls++;});
 await p.route('**/api/v1/voice/output-capabilities',r=>r.fulfill({json:{available}}));
 await p.route('**/static/speech-prompts.json',r=>r.abort()); // Missing pack exercises the explicit online fallback.
 await p.route('**/api/v1/voice/synthesize',r=>{requests.push(r.request().postDataJSON());return r.fulfill({status:503,json:{ok:false}})});
 await p.goto(process.env.SPEECH_QA_URL);await p.locator('#settingsBtn').click();
 await p.locator('#speechCloud').check();await p.locator('#speechVoice').selectOption('zh-CN-YunxiNeural');await p.locator('#speechRate').selectOption('0');
 await p.locator('#speechEnabled').uncheck();await p.reload();await p.locator('#settingsBtn').click();
 assert.equal(await p.locator('#speechCloud').isChecked(),true);assert.equal(await p.locator('#speechEnabled').isChecked(),false);
 assert.equal(await p.locator('#speechVoice').inputValue(),'zh-CN-YunxiNeural');assert.equal(await p.locator('#speechRate').inputValue(),'0');
 await p.locator('#speechPreview').click();await p.waitForFunction(()=>document.querySelector('#speechStatus').textContent.includes('暂不可用'));
 assert.equal(requests.length,1);assert.equal(requests[0].voice,'zh-CN-YunxiNeural');assert.equal(requests[0].rate,0);assert.equal(requests[0].text,'看消费');
 assert.equal(await p.evaluate(()=>window.localSpeechCalls),0);
 available=false;await p.reload();await p.locator('#settingsBtn').click();await p.waitForFunction(()=>document.querySelector('#speechStatus').textContent.includes('保留您的选择'));
 assert.equal(await p.locator('#speechCloud').isChecked(),true);assert.equal(await p.locator('#speechCloud').isEnabled(),true);
 await p.locator('#speechCloud').uncheck();await p.reload();await p.locator('#settingsBtn').click();assert.equal(await p.locator('#speechCloud').isChecked(),false);
 const blocked=await b.newPage();await blocked.addInitScript(()=>Object.defineProperty(window,'localStorage',{get(){throw Error('blocked')}}));
 await blocked.goto(process.env.SPEECH_QA_URL);await blocked.locator('#settingsBtn').click();await blocked.locator('#speechCloud').check();
 assert.match(await blocked.locator('#speechStatus').innerText(),/无法保存/);assert.deepEqual(errors,[]);
}finally{await b.close()}})().catch(e=>{console.error(e);process.exit(1)});
'''
        try:
            with patch.object(ba,'runtime',fixture.rt),patch.object(ba,'gateway',ba.AgentGateway(fixture.rt)),patch.object(ba.http_rate_limiter,'allow',return_value=True):
                with ba.ThreadingHTTPServer(('127.0.0.1',0),ba.Handler) as server:
                    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                    try:
                        run=subprocess.run([node,'-e',script],env=dict(os.environ,SPEECH_QA_URL=f'http://127.0.0.1:{server.server_port}'),capture_output=True,text=True,encoding='utf-8',timeout=90)
                        self.assertEqual(run.returncode,0,run.stdout+run.stderr)
                    finally:server.shutdown();thread.join(timeout=5)
        finally:fixture.tearDown()

    def test_audio_policy_allows_only_local_blob_playback(self):
        import banking_agent as ba
        from io import BytesIO
        handler=object.__new__(ba.Handler)
        handler.command='POST';handler.path='/api/v1/voice/synthesize'
        handler.send_response=lambda status:None
        headers={};handler.send_header=lambda k,v:headers.update({k:v})
        handler.end_headers=lambda:None;handler.wfile=BytesIO()
        handler._send(200,b'ID3audio','audio/mpeg')
        self.assertIn("media-src 'self' blob:",headers['Content-Security-Policy'])
        self.assertIn("connect-src 'self'",headers['Content-Security-Policy'])
        self.assertEqual(headers['Cache-Control'],'no-store')

    def test_http_identity_and_binary_response(self):
        import banking_agent as ba
        from types import SimpleNamespace
        from unittest.mock import Mock
        handler=object.__new__(ba.Handler)
        handler.path='/api/v1/voice/synthesize'
        handler.headers={'Authorization':'Bearer demo-user-session'}
        handler._client_key=lambda:'tts-test'
        handler._body=lambda:{'text':'请核对。','consent':True}
        handler._send=lambda *args:args
        family=Mock()
        audit=Mock()
        family.http_context.return_value={'ok':False,'message':'denied'}
        with patch.object(ba,'runtime',SimpleNamespace(family=family, audit=audit)), \
             patch.object(ba.http_rate_limiter,'allow',return_value=True), \
             patch.object(ba,'TTSOutput') as tts:
            self.assertEqual(handler._do_POST()[0],403)
            tts.assert_not_called()
            family.http_context.return_value={'ok':True,'user_id':'demo-user','device_id':'demo-device'}
            def audited_synthesis(text, **kwargs):
                import hashlib
                kwargs['before_outbound']({'text_sha256':hashlib.sha256(text.encode()).hexdigest(),
                                           'text_characters':len(text)})
                return {'ok':True,'audio':b'ID3audio','outbound_attempted':True}
            tts.return_value.synthesize.side_effect=audited_synthesis
            self.assertEqual(handler._do_POST(),(200,b'ID3audio','audio/mpeg'))
            self.assertTrue(family.http_context.call_args.kwargs['required'])
            self.assertEqual([call.args[1] for call in audit.write.call_args_list],
                             ['tts.rejected','tts.requested','tts.outbound','tts.completed'])

    def test_offline_and_missing_consent_never_start_worker(self):
        with patch('tts_output.subprocess.run') as run:
            for env, consent in [({'BANK_AGENT_OFFLINE':'1','TTS_ENABLED':'1'}, True),
                                 ({'BANK_AGENT_OFFLINE':'0','TTS_ENABLED':'1'}, False)]:
                with patch.dict(os.environ, env, clear=True):
                    result = TTSOutput().synthesize('请核对办理内容。', consent=consent)
                    self.assertFalse(result['ok'])
            run.assert_not_called()

    def test_redaction_does_not_speak_credentials_or_accounts(self):
        value = redact_speech('金额￥5,000.00元，卡号6222021234567890123，手机138****8000，验证码123456，sk-abcdefghijklmno')
        for secret in ('5,000','622202','138','8000','123456','abcdefghijklmno'):
            self.assertNotIn(secret, value)
        self.assertIn('金额', value)

    def test_worker_receives_sanitized_stdin_without_shell_or_disk_audio(self):
        with patch.dict(os.environ, {'BANK_AGENT_OFFLINE':'0','TTS_ENABLED':'1'}, clear=True), \
             patch('tts_output.importlib.util.find_spec', return_value=object()), \
             patch('tts_output.subprocess.run', return_value=subprocess.CompletedProcess([],0,b'ID3'+b'x'*120,b'')) as run:
            result = TTSOutput().synthesize('已转账5000元。', consent=True, voice='zh-CN-XiaoxiaoNeural', rate=-8,
                                            before_outbound=lambda metadata: None)
            self.assertTrue(result['ok'])
            args, kwargs = run.call_args
            self.assertNotIn('5000', kwargs['input'].decode())
            self.assertNotIn('已转账', str(args))
            self.assertFalse(kwargs.get('shell', False))
            self.assertLessEqual(kwargs['timeout'], 25)
            run.assert_called_once()

    def test_invalid_parameters_and_provider_failures_are_bounded(self):
        with patch.dict(os.environ, {'BANK_AGENT_OFFLINE':'0','TTS_ENABLED':'1'}, clear=True), \
             patch('tts_output.importlib.util.find_spec', return_value=object()), \
             patch('tts_output.subprocess.run') as run:
            tts=TTSOutput()
            for kwargs in ({'text':'x'*601}, {'text':'hello','voice':'https://evil'},
                           {'text':'hello','voice':[]},{'text':'hello','rate':True},{'text':'hello','rate':99}):
                self.assertFalse(tts.synthesize(consent=True, **kwargs)['ok'])
            run.assert_not_called()
            run.side_effect=subprocess.TimeoutExpired('private contents',20)
            out=tts.synthesize('请核对。',consent=True,before_outbound=lambda metadata: None)
            self.assertFalse(out['ok'])
            self.assertNotIn('private',str(out))
            run.assert_called_once()

    def test_missing_optional_module_keeps_offline_app_usable(self):
        with patch.dict(os.environ, {'BANK_AGENT_OFFLINE':'0','TTS_ENABLED':'1'}, clear=True), \
             patch('tts_output.importlib.util.find_spec', return_value=None):
            self.assertFalse(TTSOutput().capabilities()['available'])


if __name__ == '__main__':
    unittest.main()

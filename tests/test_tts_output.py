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
        family.http_context.return_value={'ok':False,'message':'denied'}
        with patch.object(ba,'runtime',SimpleNamespace(family=family)), \
             patch.object(ba.http_rate_limiter,'allow',return_value=True), \
             patch.object(ba,'TTSOutput') as tts:
            self.assertEqual(handler._do_POST()[0],403)
            tts.assert_not_called()
            family.http_context.return_value={'ok':True,'user_id':'demo-user','device_id':'demo-device'}
            tts.return_value.synthesize.return_value={'ok':True,'audio':b'ID3audio'}
            self.assertEqual(handler._do_POST(),(200,b'ID3audio','audio/mpeg'))
            self.assertTrue(family.http_context.call_args.kwargs['required'])

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
            result = TTSOutput().synthesize('已转账5000元。', consent=True, voice='zh-CN-XiaoxiaoNeural', rate=-8)
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
            out=tts.synthesize('请核对。',consent=True)
            self.assertFalse(out['ok'])
            self.assertNotIn('private',str(out))
            run.assert_called_once()

    def test_missing_optional_module_keeps_offline_app_usable(self):
        with patch.dict(os.environ, {'BANK_AGENT_OFFLINE':'0','TTS_ENABLED':'1'}, clear=True), \
             patch('tts_output.importlib.util.find_spec', return_value=None):
            self.assertFalse(TTSOutput().capabilities()['available'])


if __name__ == '__main__':
    unittest.main()

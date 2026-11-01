"""PR1 online speech must remain authenticated, auditable and non-transactional."""
import hashlib
import json
import os
import sqlite3
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import banking_agent as ba


class SpeechAuditIntegrationTests(unittest.TestCase):
    sample = '本地合成验收私有文字，金额5000元。'

    def request(self, *, consent=True, identity=None, audit_failure=None,
                offline=False, worker_error=None, worker_returncode=0, busy=False,
                text=None):
        import tts_output as tts
        events = []

        def write(trace, event, payload, **kwargs):
            if audit_failure == event:
                raise sqlite3.OperationalError('PRIVATE_STORAGE_DETAILS')
            events.append(dict(trace=trace, event=event, payload=payload, **kwargs))

        audit = SimpleNamespace(write=write)
        family = Mock()
        family.http_context.return_value = identity or {
            'ok': True, 'user_id': 'demo-user', 'device_id': 'demo-device'}
        handler = object.__new__(ba.Handler)
        handler.path = '/api/v1/voice/synthesize'
        handler.headers = {'Authorization': 'Bearer LOCAL_TEST_ONLY'}
        handler._client_key = lambda: 'speech-audit-test'
        handler._body = lambda: {'text': self.sample if text is None else text,
                                'consent': consent, 'voice': 'zh-CN-XiaoxiaoNeural', 'rate': -8}
        handler._send = lambda *args: args
        slots = Mock()
        slots.acquire.return_value = not busy

        def synthesize(*args, **kwargs):
            self.assertTrue(any(e['event'] == 'tts.outbound' for e in events),
                            'Provider invocation must follow a persisted outbound audit')
            if worker_error:
                raise worker_error
            return subprocess.CompletedProcess([], worker_returncode, b'ID3' + b'x' * 100, b'')

        with patch.dict(os.environ, {'TTS_ENABLED': '1', 'BANK_AGENT_OFFLINE': '1' if offline else '0'}, clear=True), \
             patch.object(ba, 'runtime', SimpleNamespace(family=family, audit=audit)), \
             patch.object(ba.http_rate_limiter, 'allow', return_value=True), \
             patch('tts_output.importlib.util.find_spec', return_value=object()), \
             patch('tts_output._slots', slots), \
             patch('tts_output.subprocess.run', side_effect=synthesize) as worker:
            response = handler._do_POST()
            calls = worker.call_count
        self.assertNotIn('PRIVATE_STORAGE_DETAILS', str(response))
        self.assertNotIn(self.sample, json.dumps(events, ensure_ascii=False))
        self.assertNotIn('LOCAL_TEST_ONLY', json.dumps(events))
        return response, events, calls, handler, slots

    def test_success_is_pre_audited_and_has_one_trace_without_plaintext(self):
        response, events, calls, handler, slots = self.request()
        self.assertEqual(response[0], 200)
        self.assertEqual(response[2], 'audio/mpeg')
        self.assertEqual(calls, 1)
        self.assertEqual([e['event'] for e in events], ['tts.requested', 'tts.outbound', 'tts.completed'])
        self.assertEqual(len({e['trace'] for e in events}), 1)
        self.assertEqual(events[0]['trace'], handler._tts_trace_id)
        self.assertTrue(all(e['actor'] == 'demo-user' for e in events))
        payload = events[1]['payload']
        import tts_output
        redacted = tts_output.redact_speech(self.sample)
        self.assertEqual(payload['text_sha256'], hashlib.sha256(redacted.encode()).hexdigest())
        self.assertEqual(payload['text_characters'], len(redacted))
        self.assertNotIn('5000', json.dumps(events))
        slots.release.assert_called_once()

    def test_missing_consent_is_audited_without_worker(self):
        response, events, calls, _, _ = self.request(consent=False)
        self.assertEqual(response[0], 400)
        self.assertEqual(calls, 0)
        self.assertEqual(events[-1]['event'], 'tts.rejected')
        self.assertEqual(events[-1]['payload']['reason'], 'consent_required')
        self.assertFalse(events[-1]['payload']['outbound_attempted'])

    def test_identity_denial_is_audited_without_trusting_claimed_actor(self):
        response, events, calls, _, _ = self.request(identity={
            'ok': False, 'user_id': 'unverified-actor', 'device_id': 'unverified-device'})
        self.assertEqual(response[0], 403)
        self.assertEqual(calls, 0)
        self.assertEqual(events[-1]['event'], 'tts.rejected')
        self.assertEqual(events[-1]['actor'], 'unknown')
        self.assertNotIn('unverified', json.dumps(events))

    def test_requested_audit_failure_prevents_worker(self):
        response, _, calls, _, _ = self.request(audit_failure='tts.requested')
        self.assertEqual(response[0], 503)
        self.assertEqual(calls, 0)

    def test_outbound_audit_failure_prevents_worker_and_releases_slot(self):
        response, events, calls, _, slots = self.request(audit_failure='tts.outbound')
        self.assertEqual(response[0], 503)
        self.assertEqual(calls, 0)
        self.assertFalse(events[-1]['payload']['outbound_attempted'])
        slots.release.assert_called_once()

    def test_completion_audit_failure_does_not_retry_provider(self):
        response, events, calls, _, _ = self.request(audit_failure='tts.completed')
        self.assertEqual(response[0], 503)
        self.assertEqual(calls, 1)
        self.assertIn('tts.outbound', [e['event'] for e in events])

    def test_offline_is_audited_without_worker(self):
        response, events, calls, _, _ = self.request(offline=True)
        self.assertEqual(response[0], 503)
        self.assertEqual(calls, 0)
        self.assertFalse(events[-1]['payload']['outbound_attempted'])

    def test_busy_is_audited_without_worker_or_unowned_release(self):
        response, events, calls, _, slots = self.request(busy=True)
        self.assertEqual(response[0], 429)
        self.assertEqual(calls, 0)
        self.assertEqual(events[-1]['payload']['reason'], 'busy')
        slots.release.assert_not_called()

    def test_timeout_is_audited_with_no_retry_or_exception_text(self):
        response, events, calls, _, _ = self.request(
            worker_error=subprocess.TimeoutExpired('PRIVATE_PROVIDER_DETAILS', 22))
        self.assertEqual(response[0], 503)
        self.assertEqual(calls, 1)
        self.assertEqual(events[-1]['event'], 'tts.failed')
        self.assertTrue(events[-1]['payload']['outbound_attempted'])
        self.assertNotIn('PRIVATE_PROVIDER_DETAILS', str(response) + json.dumps(events))

    def test_bad_provider_response_is_audited(self):
        response, events, calls, _, _ = self.request(worker_returncode=1)
        self.assertEqual(response[0], 503)
        self.assertEqual(calls, 1)
        self.assertEqual(events[-1]['event'], 'tts.failed')
        self.assertEqual(events[-1]['payload']['reason'], 'provider_error')

    def test_invalid_input_is_audited_without_worker(self):
        response, events, calls, _, _ = self.request(text='x' * 601)
        self.assertEqual(response[0], 400)
        self.assertEqual(calls, 0)
        self.assertEqual(events[-1]['payload']['reason'], 'invalid_request')

    def test_direct_adapter_cannot_send_without_pre_outbound_audit(self):
        import tts_output
        with patch.dict(os.environ, {'TTS_ENABLED': '1', 'BANK_AGENT_OFFLINE': '0'}, clear=True), \
             patch('tts_output.importlib.util.find_spec', return_value=object()), \
             patch('tts_output.subprocess.run') as worker:
            response = tts_output.TTSOutput().synthesize('仅验收。', consent=True)
        self.assertFalse(response['ok'])
        self.assertEqual(response['reason'], 'audit_required')
        worker.assert_not_called()

    def test_binary_response_exposes_only_server_generated_trace_header(self):
        from io import BytesIO
        handler = object.__new__(ba.Handler)
        handler.command = 'POST'
        handler.path = '/api/v1/voice/synthesize'
        handler._tts_trace_id = 'trace_' + 'a' * 32
        handler.send_response = lambda status: None
        headers = {}
        handler.send_header = lambda key, value: headers.update({key: value})
        handler.end_headers = lambda: None
        handler.wfile = BytesIO()
        handler._send(200, b'ID3audio', 'audio/mpeg')
        self.assertEqual(headers['X-Trace-ID'], handler._tts_trace_id)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(handler.wfile.getvalue(), b'ID3audio')

    def test_real_http_and_audit_store_preserve_digest_without_trusting_user_hex(self):
        import threading
        import urllib.request
        from test_domain_snapshots import SnapshotFixture
        fixture = SnapshotFixture()
        fixture.setUp()
        fixture.rt.family.demo_sessions['pr1-local-session'] = {
            'user_id': 'demo-user', 'device_id': 'demo-device'}
        server = ba.ThreadingHTTPServer(('127.0.0.1', 0), ba.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        text = '请核对演示资料丁。'
        expected = hashlib.sha256(text.encode()).hexdigest()
        try:
            balance = fixture.rt.bank.balance_cents
            with patch.object(ba, 'runtime', fixture.rt), \
                 patch.object(ba.http_rate_limiter, 'allow', return_value=True), \
                 patch.dict(os.environ, {'TTS_ENABLED': '1', 'BANK_AGENT_OFFLINE': '0'}), \
                 patch('tts_output.importlib.util.find_spec', return_value=object()), \
                 patch('tts_output.subprocess.run', return_value=subprocess.CompletedProcess(
                     [], 0, b'ID3' + b'x' * 100, b'')) as worker:
                thread.start()
                request = urllib.request.Request(
                    f'http://127.0.0.1:{server.server_port}/api/v1/voice/synthesize',
                    data=json.dumps({'text': text, 'consent': True}).encode(),
                    headers={'Content-Type': 'application/json',
                             'Authorization': 'Bearer pr1-local-session'}, method='POST')
                with urllib.request.urlopen(request, timeout=10) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b'ID3' + b'x' * 100)
                    trace = response.headers['X-Trace-ID']
                worker.assert_called_once()
            events = fixture.rt.audit.list_for_trace(trace)
            outbound = next(e['payload'] for e in events if e['event_type'] == 'tts.outbound')
            self.assertEqual(outbound['text_sha256'], expected)
            self.assertTrue(fixture.rt.audit.verify_trace(trace)['valid'])
            self.assertNotIn(text, json.dumps(events, ensure_ascii=False))
            self.assertEqual(fixture.rt.bank.balance_cents, balance)
            self.assertNotEqual(ba.sanitize_audit_value(expected, 'text_sha256'), expected,
                                'A user-supplied string must not gain the server identifier exception')
        finally:
            if thread.is_alive():
                server.shutdown()
                thread.join(timeout=5)
            server.server_close()
            fixture.tearDown()

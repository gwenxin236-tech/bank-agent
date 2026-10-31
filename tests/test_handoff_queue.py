"""T09: local operator closure is not authority to execute banking tools."""
import copy
import json
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import banking_agent as ba


class HandoffQueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'handoff.sqlite3'
        self.rt = ba.AgentRuntime(db_path=self.path, offline=True)
        self.rt.family.demo_sessions.update({
            'ops': {'user_id': 'operator', 'device_id': 'ops-device', 'role': 'admin'},
            'other': {'user_id': 'alice', 'device_id': 'alice-device', 'role': 'user'}})

    def close(self):
        self.rt.recovery_scheduler.stop()
        self.rt.bank.close()
        self.rt.audit.db.close()

    def tearDown(self):
        self.close()
        self.tmp.cleanup()

    def post(self, action, body=None, token='demo-user-session'):
        handler = object.__new__(ba.Handler)
        handler.path = '/api/v1/' + action
        handler.headers = {'Authorization': 'Bearer ' + token} if token else {}
        handler._client_key = lambda: 'handoff-test'
        handler._body = lambda: copy.deepcopy(body or {})
        handler._send = lambda status, data, *args: (status, data)
        with patch.object(ba, 'runtime', self.rt), patch.object(ba.http_rate_limiter, 'allow', return_value=True):
            return handler.do_POST()

    def queued(self):
        status, out = self.post('customer-service', {'request_id': 'support'})
        self.assertEqual(status, 200)
        return out['handoff_case']

    def test_full_local_lifecycle_no_bank_effect_and_audit(self):
        case = self.queued()
        self.assertEqual(case['status'], 'LOCAL_QUEUED')
        code, listing = self.post('handoff/inbox', token='ops')
        self.assertEqual(code, 200)
        self.assertEqual(listing['items'][0]['case_id'], case['case_id'])
        body = {'case_id': case['case_id']}
        self.assertEqual(self.post('handoff/claim', body, 'ops')[1]['case']['status'], 'LOCAL_CLAIMED')
        closed = self.post('handoff/resolve', dict(body, resolution='REFERRED_OFFICIAL'), 'ops')[1]['case']
        self.assertEqual(closed['status'], 'LOCAL_CLOSED')
        self.assertEqual(self.post('handoff/status', body)[1]['case']['resolution'], 'REFERRED_OFFICIAL')
        self.assertEqual(self.rt.bank.balance_cents, self.rt.bank.INITIAL_BALANCE_CENTS)
        self.assertEqual(self.rt.bank.daily_transfer_cents, 0)
        proof = self.rt.audit.verify_trace(case['trace_id'])
        self.assertTrue(proof['valid'])
        types = [e['event_type'] for e in self.rt.audit.list_for_trace(case['trace_id'])]
        for expected in ('human.case.queued', 'human.case.claimed', 'human.case.closed'):
            self.assertEqual(types.count(expected), 1)

    def test_authentication_roles_and_owner_device_are_enforced(self):
        case = self.queued()
        for action in ('handoff/inbox', 'handoff/claim', 'handoff/resolve', 'handoff/status'):
            self.assertEqual(self.post(action, {'case_id': case['case_id']}, '')[0], 403)
        self.assertEqual(self.post('customer-service', {'request_id': 'anonymous'}, '')[0], 403)
        for action in ('inbox', 'claim', 'resolve'):
            self.assertEqual(self.post('handoff/' + action, {'case_id': case['case_id']})[0], 403)
        self.assertEqual(self.post('handoff/status', {'case_id': case['case_id']}, 'other')[0], 403)
        self.assertEqual(self.post('handoff/claim', {'case_id': case['case_id'], 'role': 'admin'})[0], 403)
        with self.assertRaises(PermissionError):
            self.rt.handoffs.status(case['case_id'], 'demo-user', 'wrong-device')

    def test_idempotent_registration_claim_close_and_restart(self):
        case = self.queued()
        self.assertEqual(self.queued()['case_id'], case['case_id'])
        body = {'case_id': case['case_id']}
        a = self.post('handoff/claim', body, 'ops')
        self.assertEqual(self.post('handoff/claim', body, 'ops'), a)
        done = self.post('handoff/resolve', dict(body, resolution='GUIDANCE_PROVIDED'), 'ops')
        self.assertEqual(self.post('handoff/resolve', dict(body, resolution='GUIDANCE_PROVIDED'), 'ops'), done)
        self.close()
        self.rt = ba.AgentRuntime(db_path=self.path, offline=True)
        restored = self.rt.handoffs.status(case['case_id'], 'demo-user', 'demo-device')
        self.assertEqual(restored['status'], 'LOCAL_CLOSED')
        self.assertTrue(self.rt.audit.verify_trace(case['trace_id'])['valid'])

    def test_illegal_transitions_payloads_and_foreign_operator(self):
        case = self.queued()
        body = {'case_id': case['case_id']}
        self.assertEqual(self.post('handoff/resolve', dict(body, resolution='GUIDANCE_PROVIDED'), 'ops')[0], 409)
        self.post('handoff/claim', body, 'ops')
        self.rt.family.demo_sessions['ops2'] = {'user_id': 'second', 'device_id': 'second-device', 'role': 'admin'}
        self.assertEqual(self.post('handoff/claim', body, 'ops2')[0], 409)
        self.assertEqual(self.post('handoff/resolve', dict(body, resolution='GUIDANCE_PROVIDED'), 'ops2')[0], 409)
        for extra in ({'resolution': 'TRANSFER_SUCCESS'}, {'resolution': 'GUIDANCE_PROVIDED', 'note': '验证码123456'}, {'case_id': []}):
            self.assertEqual(self.post('handoff/resolve', dict(body, **extra), 'ops')[0], 400)

    def test_atomic_rollback_when_audit_fails(self):
        case = self.queued()
        with patch.object(self.rt.audit, 'write', side_effect=RuntimeError('private')):
            code, out = self.post('handoff/claim', {'case_id': case['case_id']}, 'ops')
        self.assertEqual(code, 503)
        self.assertNotIn('private', str(out))
        self.assertEqual(self.rt.handoffs.status(case['case_id'], 'demo-user', 'demo-device')['status'], 'LOCAL_QUEUED')

    def test_concurrent_claims_have_one_owner(self):
        case = self.queued()
        def claim(who):
            try:
                return self.rt.handoffs.transition(case['case_id'], 'claim', who, 'device', '')
            except RuntimeError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, ['ops-a', 'ops-b']))
        self.assertEqual(sum(r is not None for r in results), 1)

    def test_operator_card_is_redacted_and_never_carries_action_token(self):
        plan = self.rt.chat('给妈妈转1元')['plan']
        internal = self.rt.pending[plan['plan_id']]
        internal.entities['beneficiary_account'] = '13812345678'
        internal.entities['raw_conversation'] = 'secret raw dialogue'
        self.rt._create_handoff_card('user_requested', plan=internal)
        encoded = json.dumps(self.rt.handoffs.inbox())
        self.assertNotIn('13812345678', encoded)
        self.assertNotIn('secret raw dialogue', encoded)
        self.assertNotIn(plan['consent_token'], encoded)
        self.assertIn('amount_cents', encoded)

    def test_unknown_transaction_handoff_cannot_claim_settlement(self):
        self.rt.bank.inject_fault('timeout')
        plan = self.rt.chat('给妈妈转200元')['plan']
        self.rt.confirm(plan['plan_id'], plan['consent_token'])
        with patch.object(self.rt.bank, 'transfer', wraps=self.rt.bank.transfer) as transfer:
            self.rt.dag_control('approve', 'plan-' + plan['plan_id'], 'demo-user', 'demo-device', decision='handoff')
            case = self.rt.handoffs.inbox()[0]
            self.rt.handoffs.transition(case['case_id'], 'claim', 'ops', 'device', '')
            with self.assertRaises(ValueError):
                self.rt.handoffs.transition(case['case_id'], 'resolve', 'ops', 'device', 'GUIDANCE_PROVIDED')
            out = self.rt.handoffs.transition(case['case_id'], 'resolve', 'ops', 'device', 'NEEDS_RECONCILIATION')
            self.assertEqual(out['resolution'], 'NEEDS_RECONCILIATION')
            transfer.assert_not_called()

    def test_browser_user_can_observe_local_operator_closure(self):
        node, module = os.getenv('C7_NODE_BINARY'), os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:
            self.skipTest('Run scripts/browser_qa.py --full for browser acceptance')
        script = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage(); const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.T09_URL);
  await page.locator('#customerServiceBtn').click();
  await page.waitForFunction(()=>document.querySelector('#customerServiceStatus').dataset.caseId);
  const case_id=await page.locator('#customerServiceStatus').getAttribute('data-case-id');
  assert.equal(await page.locator('#newSupportRequestBtn').isDisabled(),true);
  const post=(action,body,token='ops')=>page.request.post(process.env.T09_URL+'/api/v1/handoff/'+action,
     {headers:{Authorization:'Bearer '+token},data:body});
  assert.equal((await post('claim',{case_id},'demo-user-session')).status(),403);
  assert.equal((await post('status',{case_id},'other')).status(),403);
  assert.equal((await post('claim',{case_id})).status(),200);
  await page.locator('#handoffStatusRefresh').click();
  await page.waitForFunction(()=>document.querySelector('#customerServiceStatus').textContent.includes('已领取'));
  assert.equal((await post('resolve',{case_id,resolution:'REFERRED_OFFICIAL'})).status(),200);
  await page.locator('#handoffStatusRefresh').click();
  await page.waitForFunction(()=>document.querySelector('#customerServiceStatus').textContent.includes('已记录处理结论'));
  assert.match(await page.locator('#customerServiceStatus').textContent(),/尚未接通真人客服/);
  assert.equal(await page.locator('#newSupportRequestBtn').isEnabled(),true);
  // Explicit new round after closure. A lost reply must reuse the new round's key.
  let loseReply=true; const keys=[];
  await page.route('**/api/v1/customer-service',async r=>{
    keys.push(r.request().postDataJSON().request_id);
    const response=await r.fetch();
    if(loseReply){loseReply=false;await r.abort();}else await r.fulfill({response});
  });
  await page.locator('#newSupportRequestBtn').click();
  await page.waitForFunction(()=>document.querySelector('#customerServiceStatus').textContent.includes('暂时无法确认'));
  assert.equal(await page.locator('#newSupportRequestBtn').isDisabled(),true);
  await page.locator('#customerServiceBtn').click();
  await page.waitForFunction(old=>document.querySelector('#customerServiceStatus').dataset.caseId && document.querySelector('#customerServiceStatus').dataset.caseId!==old,case_id);
  const nextCase=await page.locator('#customerServiceStatus').getAttribute('data-case-id');
  assert.equal(keys.length,2); assert.equal(keys[0],keys[1]);
  await page.reload(); await page.locator('#customerServiceBtn').click();
  await page.waitForFunction(id=>document.querySelector('#customerServiceStatus').dataset.caseId===id,nextCase);
  assert.equal(keys[2],keys[0]);
  const inbox=await (await post('inbox',{})).json();
  assert.equal(inbox.items.length,2,'重试和刷新不能重复创建工单');
  await page.route('**/api/v1/handoff/status',r=>r.abort());
  await page.locator('#handoffStatusRefresh').click();
  await page.waitForFunction(()=>document.querySelector('#customerServiceStatus').textContent.includes('暂时无法核对'));
  await page.setViewportSize({width:390,height:844});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.deepEqual(errors,[]);
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        class QuietHandler(ba.Handler):
            def log_message(self, *args):
                pass
        with patch.object(ba, 'runtime', self.rt), patch.object(ba, 'gateway', ba.AgentGateway(self.rt)), patch.object(ba.http_rate_limiter, 'allow', return_value=True):
            with ba.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler) as server:
                thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
                thread.start()
                try:
                    env = dict(os.environ, T09_URL='http://127.0.0.1:' + str(server.server_port))
                    run = subprocess.run([node, '-e', script], env=env, capture_output=True, text=True, encoding='utf-8', timeout=60)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                finally:
                    server.shutdown()
                    thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()

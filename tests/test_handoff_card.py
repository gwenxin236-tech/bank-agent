"""E5: structured, least-privilege human handoff evidence."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import banking_agent as ba
from handoff_contract import HandoffCard


class HandoffCardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = ba.AgentRuntime(db_path=Path(self.tmp.name) / 'handoff.db', offline=True)

    def tearDown(self):
        self.rt.recovery_scheduler.stop()
        self.rt.bank.close()
        self.rt.audit.db.close()
        self.tmp.cleanup()

    def test_contract_has_required_fields_and_least_privilege_redaction(self):
        card = HandoffCard.from_plan({
            'plan_id': 'plan-1', 'trace_id': 'trace-1', 'intent': 'transfer',
            'actor_id': 'demo-user', 'device_id': 'demo-device',
            'summary': '给妈妈转账',
            'entities': {
                'beneficiary': '妈妈', 'amount_cents': 300000,
                'card_number': '6222021234561234',
                'raw_conversation': '给妈妈转三千，验证码123456',
                'otp': '123456',
            },
            'steps': [{'id': 's1', 'tool': 'check_transfer_constraints', 'status': 'completed'}],
        }, trigger='human_gate_rejected')
        data = card.to_dict()
        for field in ('user_goal', 'verified_information', 'authorization_scope',
                      'executed_steps', 'business_identifiers', 'pending_items',
                      'redacted_context'):
            self.assertIn(field, data)
        encoded = json.dumps(data, ensure_ascii=False)
        self.assertNotIn('6222021234561234', encoded)
        self.assertIn('1234', encoded)
        self.assertNotIn('给妈妈转三千，验证码123456', encoded)
        self.assertNotIn('123456', encoded)
        self.assertEqual(data['redacted_context']['amount_cents'], 300000)
        self.assertNotIn('raw_conversation', encoded)

    def test_user_request_creates_card_and_audit(self):
        out = self.rt.request_customer_service('help-e5', 'demo-user', 'demo-device')
        self.assertTrue(out['ok'])
        self.assertIn('handoff_card', out)
        card = out['handoff_card']
        self.assertEqual(card['business_identifiers']['trace_id'], out['trace_id'])
        self.assertEqual(card['redacted_context']['trigger'], 'user_requested')
        events = self.rt.audit.list_for_trace(out['trace_id'])
        self.assertTrue(any(e['event_type'] == 'human.handoff.created' for e in events))

    def test_human_gate_rejection_creates_card(self):
        out = self.rt.chat('给妈妈转 200 元')
        plan = out['plan']
        rejected = self.rt.dag_control('approve', 'plan-' + plan['plan_id'],
                                       'demo-user', 'demo-device', decision='reject')
        self.assertTrue(rejected['ok'], rejected)
        self.assertEqual(rejected['handoff_card']['redacted_context']['trigger'], 'human_gate_rejected')
        self.assertEqual(rejected['handoff_card']['business_identifiers']['plan_id'], plan['plan_id'])

    def test_unknown_result_handoff_creates_card_without_resubmitting(self):
        self.rt.bank.inject_fault('timeout')
        out = self.rt.chat('给妈妈转 200 元')
        plan = out['plan']
        self.rt.confirm(plan['plan_id'], plan['consent_token'])
        with patch.object(self.rt.bank, 'transfer', wraps=self.rt.bank.transfer) as submit:
            handoff = self.rt.dag_control('approve', 'plan-' + plan['plan_id'],
                                          'demo-user', 'demo-device', decision='handoff')
            self.assertTrue(handoff['ok'], handoff)
            self.assertEqual(handoff['handoff_card']['redacted_context']['trigger'], 'unknown_result')
            self.assertEqual(submit.call_count, 0)

    def test_card_build_failure_does_not_block_customer_service(self):
        with patch.object(self.rt, '_create_handoff_card', side_effect=RuntimeError('card unavailable')):
            out = self.rt.request_customer_service('help-no-card', 'demo-user', 'demo-device')
        self.assertTrue(out['ok'])
        self.assertEqual(out['status'], 'LOCAL_QUEUED')
        self.assertNotIn('handoff_card', out)

    def test_frontend_renders_handoff_card_without_raw_conversation(self):
        html = (ROOT / 'app/static/index.html').read_text(encoding='utf-8')
        js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
        self.assertIn('handoffCardPanel', html)
        self.assertIn('handoff_card', js)
        self.assertIn('redacted_context', js)
        self.assertIn('办理路径', html + js)


if __name__ == '__main__':
    unittest.main()

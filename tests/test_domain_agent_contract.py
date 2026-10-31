import unittest

from domain_agent_contract import DomainContext, DomainFinding, DomainProposal, DOMAIN_NAMES


def context(**changes):
    values = dict(trace_id='trace', session_id='session', actor_id='alice', subject_id='alice',
                  device_id='device', channel='web', user_input='给妈妈转1元',
                  entities={'amount_cents': 100, 'beneficiary': '妈妈'})
    return DomainContext(**dict(values, **changes))


class DomainContractTests(unittest.TestCase):
    def test_exact_six_internal_domains(self):
        self.assertEqual(DOMAIN_NAMES, {'payment', 'antifraud', 'finance_health', 'family', 'subscription', 'card'})
        with self.assertRaises(ValueError):
            DomainFinding('unknown', [], [], [], [], [])

    def test_context_deep_copy_immutable_and_json_roundtrip(self):
        source = {'amount_cents': 100, 'items': [{'name': 'original'}]}
        ctx = context(entities=source)
        source['items'][0]['name'] = 'changed'
        self.assertEqual(ctx.entities['items'][0]['name'], 'original')
        with self.assertRaises(TypeError):
            ctx.entities['items'][0]['name'] = 'injected'
        data = ctx.to_dict()
        data['entities']['items'][0]['name'] = 'caller'
        self.assertEqual(ctx.entities['items'][0]['name'], 'original')

    def test_floats_and_boolean_string_money_rejected_recursively(self):
        for value in (1.0, True, '100'):
            with self.assertRaises(ValueError):
                context(entities={'nested': [{'amount_cents': value}]})
        with self.assertRaises(ValueError):
            DomainFinding('payment', [{'value': .5}], [], [], [], ['source'])

    def test_no_authority_fields_even_when_nested_or_obfuscated(self):
        for name in ('allow', 'authorized', 'approved', 'is_authorized', 'authz_allowed',
                     'ALLOW', 'policy_override', 'skip-auth', 'permission_granted', 'action_token',
                     'isAuthorized', 'authzAllowed', 'AUTHZAllowed', 'policyOverride'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                DomainProposal([{'tool': 'create_transfer', 'params': {'nested': [{name: True}]}}], [], [], [], 'Mock only')

    def test_unknown_fields_and_unsupported_objects_rejected(self):
        with self.assertRaises(TypeError):
            DomainProposal([], [], [], [], 'Mock', approved=True)
        with self.assertRaises(ValueError):
            context(entities={'object': object()})
        with self.assertRaises(ValueError):
            DomainProposal([{'tool': 'create_transfer', 'params': {}, 'retry': 2}], [], [], [], 'Mock')

    def test_bounds_and_required_identity_fields(self):
        for change in ({'actor_id': ''}, {'user_input': 'x' * 2001}, {'entities': {'a': ['x'] * 129}}):
            with self.assertRaises(ValueError):
                context(**change)
        deep = {}
        for _ in range(20):
            deep = {'a': deep}
        with self.assertRaises(ValueError):
            context(entities=deep)

    def test_finding_requires_evidence_and_proposal_only_contains_suggestions(self):
        with self.assertRaises(ValueError):
            DomainFinding('payment', [{'amount_cents': 100}], [], [], [], [])
        finding = DomainFinding('payment', [], ['beneficiary'], [], ['请补充收款人'], [])
        self.assertEqual(finding.missing_fields, ('beneficiary',))
        proposal = DomainProposal([], ['explicit_user_confirmation'], ['strong_auth'], [], 'Replace Mock adapter')
        self.assertEqual(proposal.to_dict()['required_auth'], ['strong_auth'])


if __name__ == '__main__':
    unittest.main()

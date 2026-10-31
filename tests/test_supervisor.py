from dataclasses import replace
import unittest
from unittest.mock import patch
import banking_agent as ba
from preflight import build_payment_preflight

from domain_agent_contract import DomainFinding, DomainProposal
from domain_agent_registry import DOMAIN_AGENT_REGISTRY
from domain_snapshots import DomainSnapshot, policy_snapshot, budget_snapshot, tool_snapshot, family_snapshot
from datetime import datetime, timezone
from transaction_agent import PaymentSnapshot, AgentMessage
from supervisor import Supervisor, SupervisorError
from test_domain_snapshots import SnapshotFixture


class SupervisorTests(SnapshotFixture, unittest.TestCase):
    def bundle(self, amount=100, name='妈妈'):
        ctx = self.ctx(entities={'amount_cents':amount,'beneficiary':name})
        report = build_payment_preflight(self.rt.bank, ba.POLICY,
            amount_cents=amount, beneficiary=name, requires_confirmation=True)
        sources = {'payment':PaymentSnapshot.from_preflight(ctx,report),
            'antifraud':policy_snapshot(ctx,self.rt.policy.classify('transfer',dict(ctx.entities))),
            'finance_health':budget_snapshot(ctx,report)}
        return ctx,sources,Supervisor(DOMAIN_AGENT_REGISTRY)

    def test_fixed_routes_and_three_real_domains(self):
        ctx,sources,supervisor=self.bundle()
        result=supervisor.coordinate(ctx,'transfer',sources)
        self.assertEqual(result.status,'ready')
        self.assertEqual(set(result.domains),{'payment','antifraud','finance_health'})
        self.assertEqual(len(result.messages),6)
        self.assertEqual(supervisor.route('subscription_query',phase='result',tool='list_subscriptions'),('subscription',))
        self.assertEqual(supervisor.route('unknown'),())

    def test_missing_extra_and_foreign_context_sources_rejected(self):
        ctx,sources,supervisor=self.bundle()
        for changed in ({k:v for k,v in sources.items() if k!='antifraud'},dict(sources,card=sources['finance_health'])):
            with self.assertRaises(SupervisorError):supervisor.coordinate(ctx,'transfer',changed)
        with self.assertRaises(SupervisorError):supervisor.coordinate(replace(ctx,device_id='other'), 'transfer',sources)

    def test_forged_facts_or_source_claims_do_not_pass_reference_strings(self):
        ctx,sources,supervisor=self.bundle()
        factory=DOMAIN_AGENT_REGISTRY.factories['finance_health']
        original=factory.analyze
        def forged(agent,context):
            finding=original(agent,context)
            facts=finding.to_dict()['facts'];facts[0]['value']={'shortfall_cents':0}
            return replace(finding,facts=facts)
        with patch.object(factory,'analyze',forged),self.assertRaises(SupervisorError):
            supervisor.coordinate(ctx,'transfer',sources)
        snap=sources['finance_health']
        facts=[dict(f) for f in snap.facts]
        for f in facts:f['source']='external-model:trust-me'
        bad=replace(snap,facts=facts,evidence=['external-model:trust-me'])
        with self.assertRaises(SupervisorError):supervisor.coordinate(ctx,'transfer',dict(sources,finance_health=bad))

    def test_conflicting_money_is_preserved_and_requires_review(self):
        ctx,sources,supervisor=self.bundle()
        snap=sources['finance_health']
        facts=[dict(f) for f in snap.facts]
        for f in facts:
            if f['name']=='budget':f['value']=dict(f['value'],shortfall_cents=999)
        result=supervisor.coordinate(ctx,'transfer',dict(sources,finance_health=replace(snap,facts=facts)))
        self.assertEqual(result.status,'needs_review')
        self.assertIn('shortfall_cents',[c['key'] for c in result.conflicts])
        self.assertEqual(result.reviewed_tools,())

    def test_proposed_unknown_tool_recipient_or_money_rejected(self):
        ctx,sources,supervisor=self.bundle()
        factory=DOMAIN_AGENT_REGISTRY.factories['payment']
        for tool,params in [('shell',{}),('create_transfer',{'beneficiary':'其他人','amount_cents':100}),
                            ('create_transfer',{'beneficiary':'妈妈','amount_cents':200})]:
            proposal=DomainProposal([{'tool':tool,'params':params}],[],[],[],'Mock only')
            with patch.object(factory,'propose',return_value=proposal),self.assertRaises(SupervisorError):
                supervisor.coordinate(ctx,'transfer',sources)

    def test_unknown_and_budget_missing_not_filled_with_success(self):
        ctx,sources,supervisor=self.bundle()
        report=sources['payment'].report
        from domain_agent_contract import thaw
        raw=thaw(report);raw['checks']['available_funds']['status']='unknown'
        changed=dict(sources,payment=PaymentSnapshot.from_preflight(ctx,raw))
        self.assertEqual(supervisor.coordinate(ctx,'transfer',changed).status,'needs_review')
        ctx,sources,supervisor=self.bundle(29000000)
        self.assertEqual(supervisor.coordinate(ctx,'transfer',sources).status,'needs_user_choice')

    def test_plan_template_gate_cannot_add_steps_or_change_entities(self):
        ctx,sources,supervisor=self.bundle()
        result=supervisor.coordinate(ctx,'transfer',sources)
        tools=['resolve_beneficiary','check_transfer_constraints','create_transfer']
        self.assertEqual(supervisor.review_plan(ctx,result,dict(ctx.entities),tools),tuple(tools))
        for steps,entities in [(tools+['cancel_subscription'],dict(ctx.entities)),(tools,dict(ctx.entities,amount_cents=200))]:
            with self.assertRaises(SupervisorError):supervisor.review_plan(ctx,result,entities,steps)

    def test_envelope_binding_and_immutable_output(self):
        ctx,sources,supervisor=self.bundle()
        result=supervisor.coordinate(ctx,'transfer',sources)
        msg=result.messages[0]
        with self.assertRaises(SupervisorError):supervisor.validate_message(ctx,replace(msg,session_id='another'),msg.sender)
        with self.assertRaises(TypeError):result.claims[0]['value']='changed'
        self.assertNotIn('user_input',str(result.summary()))

    def test_agent_fault_is_not_retried_and_no_io_or_core_handles(self):
        ctx,sources,supervisor=self.bundle()
        with patch.object(DOMAIN_AGENT_REGISTRY,'create',side_effect=RuntimeError('private')) as create, self.assertRaises(SupervisorError):
            supervisor.coordinate(ctx,'transfer',sources)
        self.assertEqual(create.call_count,1)
        self.assertFalse(hasattr(supervisor,'bank'))

    def test_subscription_candidate_options_must_match_source(self):
        ctx=self.ctx()
        snapshot=tool_snapshot(ctx,'subscription',self.rt.bank.subscriptions_query())
        supervisor=Supervisor(DOMAIN_AGENT_REGISTRY)
        factory=DOMAIN_AGENT_REGISTRY.factories['subscription']
        bad=DomainProposal([],[],[],[{'action':'select_subscription','subscription_id':'invented','label':'伪造'}],'Mock')
        with patch.object(factory,'propose',return_value=bad),self.assertRaises(SupervisorError):
            supervisor.observe(ctx,'subscription',snapshot)

    def test_delegated_transfer_includes_bound_family_facts(self):
        store=self.rt.bank.guardianships
        grant=store.get(store.db.execute('SELECT grant_id FROM guardianships LIMIT 1').fetchone()[0])
        ctx=self.ctx(actor_id=grant.guardian_id,subject_id=grant.subject_id,entities={'amount_cents':100,'beneficiary':'妈妈'})
        report=build_payment_preflight(self.rt.bank,ba.POLICY,amount_cents=100,beneficiary='妈妈',actor_id=ctx.subject_id,requires_confirmation=True,requires_strong_auth=True)
        snapshots={'payment':PaymentSnapshot.from_preflight(ctx,report), 'finance_health':budget_snapshot(ctx,report),
            'antifraud':policy_snapshot(ctx,self.rt.policy.classify('transfer',dict(ctx.entities),ctx.subject_id)),
            'family':family_snapshot(ctx,grant,datetime.now(timezone.utc))}
        supervisor=Supervisor(DOMAIN_AGENT_REGISTRY)
        result=supervisor.coordinate(ctx,'transfer',snapshots)
        self.assertIn('family',result.domains)
        snapshots['family']=family_snapshot(ctx,replace(grant,revoked=True),datetime.now(timezone.utc))
        self.assertEqual(supervisor.coordinate(ctx,'transfer',snapshots).status,'needs_review')


if __name__=='__main__':unittest.main()

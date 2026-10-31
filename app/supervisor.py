"""Deterministic routing and evidence review, never a bank executor or policy.

Only platform-built snapshots are accepted. Provenance means comparison with
this request's trusted snapshots, not a signature from a real bank, nor a
sandbox for malicious Python. Conflict review never resolves facts by voting.
"""
from collections.abc import Mapping
from dataclasses import dataclass

from domain_agent_contract import DomainFinding, DomainProposal, freeze_json, thaw
from domain_snapshots import DomainSnapshot
from transaction_agent import PaymentSnapshot, AgentMessage, context_digest


class SupervisorError(ValueError):
    pass


_SOURCES = {
    'antifraud': {'risk_controls.py:assess_transfer_risk', 'banking_agent.py:PolicyEngine.classify',
                  'banking_agent.py:MockBankCore.detect_fraud_signals'},
    'finance_health': {'ledger_views.py:build_ledger_views', 'preflight.py:build_payment_preflight#budget',
                       'banking_agent.py:MockBankCore.financial_health_report'},
    'family': {'guardianship.py:GuardianshipStore.get', 'banking_agent.py:MockBankCore.list_family_relationships'},
    'subscription': {'banking_agent.py:MockBankCore.subscriptions_query'},
    'card': {'banking_agent.py:MockBankCore.cards_query', 'banking_agent.py:MockBankCore.card_status_query'},
}
_TOOL_DOMAINS = {'financial_health_report':'finance_health', 'list_subscriptions':'subscription',
                 'list_cards':'card', 'verify_card_status':'card',
                 'list_family_relationships':'family', 'detect_fraud_signals':'antifraud'}
_TRANSFER = ('resolve_beneficiary', 'check_transfer_constraints', 'create_transfer')
_OPTIONS = {'review_risk_factors','human_handoff','review_budget','review_owner_grant','select_subscription',
            'clarify_card_or_operation','adjust_amount','adjust_budget','defer','clarify','recheck_facts'}


@dataclass(frozen=True)
class Coordination:
    context_key: str
    domains: tuple
    messages: tuple
    claims: tuple
    conflicts: tuple
    status: str
    reviewed_tools: tuple

    def summary(self):
        # No raw dialogue, source facts, OTP, token, or free-form agent prose.
        return {'status':self.status, 'domains':list(self.domains),
                'conflict_keys':list(dict.fromkeys(c['key'] for c in self.conflicts)),
                'reviewed_tools':list(self.reviewed_tools), 'mode':'REVIEW_ONLY',
                'message': {'ready':'已核对本次办理资料，仍需按计划确认或验证。',
                    'needs_review':'资料存在冲突或尚未核实，请先核对或联系人工。',
                    'needs_user_choice':'预算条件未满足，请先选择调整方式。',
                    'needs_clarification':'请先补齐并核验办理信息。'}.get(self.status,'资料待核对。')}


class Supervisor:
    @staticmethod
    def review_dynamic_dag(plan,proposal,registry,allowed_reads):
        from dag_planner import review,DagProposalError
        if plan.intent=='transfer' and plan.coordination.get('status')!='ready':raise SupervisorError('trusted domain review is not ready')
        try:return review(plan,proposal,registry,allowed_reads)
        except DagProposalError as exc:raise SupervisorError(str(exc)) from exc

    def __init__(self, registry):
        self.registry = registry

    @staticmethod
    def route(intent, *, phase='planning', tool=None, delegated=False):
        if phase == 'planning' and intent == 'transfer':
            return ('payment','antifraud','finance_health') + (('family',) if delegated else ())
        if phase == 'result' and tool in _TOOL_DOMAINS:
            return (_TOOL_DOMAINS[tool],)
        return ()

    @staticmethod
    def validate_message(ctx, message, domain):
        if (type(message) is not AgentMessage or message.trace_id != ctx.trace_id or
                message.session_id != ctx.session_id or message.context_key != context_digest(ctx) or
                message.sender != domain or message.recipient != 'supervisor'):
            raise SupervisorError('Message identity or routing mismatch')

    @staticmethod
    def _expected(ctx, domain, snapshot):
        if snapshot.context_key != context_digest(ctx):
            raise SupervisorError('Snapshot context mismatch')
        if domain == 'payment':
            if type(snapshot) is not PaymentSnapshot:
                raise SupervisorError('Invalid payment source')
            PaymentSnapshot.from_preflight(ctx, snapshot.report)
            facts, references = [], []
            for key, check in snapshot.report['checks'].items():
                source = 'preflight.py:build_payment_preflight#' + key
                references.append(source)
                facts.append({'check':key, 'status':check['status'], 'evidence':thaw(check['evidence']),
                              'source':source, 'source_kind':'platform_read_only_report'})
            return freeze_json(facts), tuple(references)
        if type(snapshot) is not DomainSnapshot or snapshot.domain != domain:
            raise SupervisorError('Invalid domain source')
        if (not snapshot.evidence or any(ref not in _SOURCES.get(domain, set()) for ref in snapshot.evidence)
                or any(f.get('source') not in snapshot.evidence for f in snapshot.facts)):
            raise SupervisorError('Unrecognized evidence source')
        global_sources = {'ledger_views.py:build_ledger_views', 'banking_agent.py:MockBankCore.financial_health_report',
            'banking_agent.py:MockBankCore.list_family_relationships', 'banking_agent.py:MockBankCore.subscriptions_query',
            'banking_agent.py:MockBankCore.cards_query', 'banking_agent.py:MockBankCore.card_status_query'}
        if any(source in global_sources for source in snapshot.evidence) and (ctx.actor_id != 'demo-user' or ctx.subject_id != 'demo-user'):
            raise SupervisorError('Legacy global source belongs to demo-user only')
        for fact in snapshot.facts:
            if fact.get('name') == 'participants' and (fact['value'].get('actor_id') != ctx.actor_id or fact['value'].get('subject_id') != ctx.subject_id):
                raise SupervisorError('Family fact participants mismatch')
        return snapshot.facts, snapshot.evidence

    @staticmethod
    def _proposal(ctx, domain, proposal, snapshot):
        if type(proposal) is not DomainProposal:
            raise SupervisorError('Invalid proposal type')
        # Revalidate the actual payload, not only its class label.
        DomainProposal(**proposal.to_dict())
        if set(proposal.required_auth) - {'strong_auth','owner_verification'} or set(proposal.required_confirmations) - {'explicit_user_confirmation'}:
            raise SupervisorError('Unknown authentication suggestion')
        for option in proposal.user_options:
            if option.get('action') not in _OPTIONS:
                raise SupervisorError('Unknown user option')
            if option['action'] == 'select_subscription':
                items = next((f['value'] for f in snapshot.facts if f.get('name') == 'subscriptions'), ()) if type(snapshot) is DomainSnapshot else ()
                target = next((item for item in items if item['id'] == option.get('subscription_id')), None)
                if set(option) != {'action','subscription_id','label'} or target is None or option['label'] != (target['merchant'] or '待核验订阅'):
                    raise SupervisorError('Candidate option does not match verified source')
            elif option['action'] == 'clarify':
                if set(option) != {'action','fields'} or not isinstance(option['fields'], (tuple,list)) or set(option['fields']) - {'amount_cents','beneficiary','beneficiary_account'}:
                    raise SupervisorError('Unreviewed clarification fields')
            elif set(option) != {'action'}:
                raise SupervisorError('Unreviewed user option parameters')
        for step in proposal.suggested_steps:
            tool, params = step['tool'], step['params']
            if domain == 'payment':
                schema = {'resolve_beneficiary':{'beneficiary'}, 'check_transfer_constraints':{'amount_cents'},
                          'create_transfer':{'beneficiary','amount_cents'}}
            elif domain == 'subscription' and ctx.entities.get('operation') == 'cancel':
                schema = {'cancel_subscription':{'subscription_id'}}
            elif domain == 'card':
                action = {'report_loss':'report_card_loss', 'unreport_loss':'unreport_card_loss'}.get(ctx.entities.get('card_operation'))
                schema = {name:{'card_id'} for name in ('list_cards','verify_card_status', action) if name}
            else:
                schema = {}
            if tool not in schema or set(params) != schema[tool]:
                raise SupervisorError('Tool or parameter outside reviewed domain')
            if any(value != ctx.entities.get(key) for key,value in params.items()):
                raise SupervisorError('Proposal changes bound transaction target or amount')

    def _collect(self, ctx, domains, snapshots):
        if set(snapshots) != set(domains):
            raise SupervisorError('Required domain sources missing or unexpected')
        messages = []
        for domain in domains:
            snapshot = snapshots[domain]
            expected, references = self._expected(ctx, domain, snapshot)
            agent = self.registry.create(domain, snapshot)
            finding = agent.analyze(ctx)
            if (type(finding) is not DomainFinding or finding.domain != domain or
                    finding.facts != expected or finding.evidence != references):
                raise SupervisorError('Finding does not match platform evidence')
            # Free-form recommendations/signals do not determine authorization.
            proposal = agent.propose(ctx, finding)
            self._proposal(ctx, domain, proposal, snapshot)
            for payload in (finding, proposal):
                message = AgentMessage.from_context(ctx, domain, 'supervisor', payload)
                self.validate_message(ctx, message, domain)
                messages.append(message)
        return tuple(messages)

    @staticmethod
    def _claims(messages):
        claims = []
        def add(key, value, domain, source):
            if value is not None:
                claims.append({'key':key,'value':value,'domain':domain,'source':source})
        for message in messages:
            if type(message.payload) is not DomainFinding:
                continue
            for fact in message.payload.facts:
                domain, source = message.sender, fact['source']
                if domain == 'payment':
                    for key in ('amount_cents','available_cents','shortfall_cents','projected_disposable_cents'):
                        if key in fact['evidence']:add(key,fact['evidence'][key],domain,source)
                else:
                    name, value = fact['name'], fact['value']
                    if name in {'budget','funds'} and isinstance(value,Mapping):
                        for key in ('amount_cents','available_cents','shortfall_cents','projected_disposable_cents'):
                            if key in value:add(key,value[key],domain,source)
                    if name == 'cards':
                        for card in value:add('card_status:' + str(card['id']),card['status'],domain,source)
        conflicts = []
        for key in dict.fromkeys(c['key'] for c in claims):
            entries = [c for c in claims if c['key'] == key]
            values = []
            for entry in entries:
                if not any(type(entry['value']) is type(v) and entry['value'] == v for v in values):values.append(entry['value'])
            if len(values) > 1:
                conflicts.append({'key':key,'values':values,'sources':[{'domain':e['domain'],'source':e['source']} for e in entries]})
        return freeze_json(claims), freeze_json(conflicts)

    def coordinate(self, ctx, intent, snapshots, *, phase='planning', tool=None):
        try:
            domains = self.route(intent, phase=phase, tool=tool, delegated=ctx.actor_id != ctx.subject_id)
            if not domains:
                raise SupervisorError('No reviewed route for this request')
            messages = self._collect(ctx, domains, snapshots)
            claims, conflicts = self._claims(messages)
            status = 'needs_review' if conflicts else 'ready'
            if any(type(s) is DomainSnapshot and s.status == 'unknown' for s in snapshots.values()):status='needs_review'
            if 'family' in snapshots:
                grants=[f['value'] for f in snapshots['family'].facts if f.get('name')=='grant']
                if grants and any(g.get('active_at_capture') is not True for g in grants):status='needs_review'
            if 'payment' in snapshots and not conflicts:
                checks = snapshots['payment'].report['checks']
                if checks['budget']['status'] == 'missing':status='needs_user_choice'
                elif any(checks[k]['status'] != 'satisfied' for k in ('available_funds','daily_limit','card_status','budget')):status='needs_review'
                elif checks['beneficiary']['status'] != 'satisfied':status='needs_clarification'
            tools = _TRANSFER if status == 'ready' and intent == 'transfer' and phase == 'planning' else ()
            return Coordination(context_digest(ctx), domains, messages, claims, conflicts, status, tools)
        except SupervisorError:
            raise
        except Exception as error:
            raise SupervisorError('Supervisor evidence review failed') from error

    def observe(self, ctx, domain, snapshot):
        """One already-authorized evidence node, not a new tool/query route."""
        try:
            messages = self._collect(ctx, (domain,), {domain:snapshot})
            claims, conflicts = self._claims(messages)
            status = 'needs_review' if conflicts or (type(snapshot) is DomainSnapshot and snapshot.status == 'unknown') else 'ready'
            return Coordination(context_digest(ctx),(domain,),messages,claims,conflicts,status,())
        except SupervisorError:
            raise
        except Exception as error:
            raise SupervisorError('Supervisor evidence observation failed') from error

    @staticmethod
    def review_plan(ctx, result, entities, tools):
        if (type(result) is not Coordination or result.context_key != context_digest(ctx) or result.status != 'ready'
                or result.reviewed_tools != _TRANSFER or tuple(tools) != _TRANSFER or
                any(entities.get(k) != ctx.entities.get(k) for k in ('amount_cents','beneficiary'))):
            raise SupervisorError('Original plan differs from the reviewed transfer template')
        return _TRANSFER

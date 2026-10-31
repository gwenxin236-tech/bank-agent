"""Trusted projections of ALREADY available platform facts. No core reads.

No HTTP/LLM input may be used to construct these as if it were a ToolResult.
Source ownership/authorization is enforced by the caller's existing execution
plane; the demo's globally scoped subscription/card/family tools are additionally
restricted here to their actual owner. These are not signed attestations.
"""
from dataclasses import dataclass
from collections.abc import Mapping

from domain_agent_contract import DOMAIN_NAMES, DomainFinding, DomainProposal, thaw
from transaction_agent import context_digest, PaymentSnapshot


@dataclass(frozen=True)
class DomainSnapshot:
    domain: str
    context_key: str
    facts: tuple
    evidence: tuple
    status: str = 'known'

    def __post_init__(self):
        if self.domain not in DOMAIN_NAMES - {'payment'} or self.status not in {'known', 'unknown'}:
            raise ValueError('Invalid domain snapshot')
        finding = DomainFinding(self.domain, self.facts, [], [], [], self.evidence)
        object.__setattr__(self, 'facts', finding.facts)
        object.__setattr__(self, 'evidence', finding.evidence)


def _snapshot(ctx, domain, values, source, status='known'):
    facts = [{'name': k, 'value': v, 'source': source} for k, v in values.items()]
    return DomainSnapshot(domain, context_digest(ctx), facts, [source], status)


def policy_snapshot(ctx, decision):
    # Existing risk_controls features only; never re-implement PolicyEngine.
    values = {k: decision[k] for k in ('risk_score', 'risk_factors', 'aml_suspect', 'aml_count', 'recent_transfer_count') if k in decision}
    source = 'risk_controls.py:assess_transfer_risk' if 'assessed_at' in decision else 'banking_agent.py:PolicyEngine.classify'
    if 'risk_score' not in values or 'risk_factors' not in values:
        return _snapshot(ctx, 'antifraud', {}, source, 'unknown')
    return _snapshot(ctx, 'antifraud', values, source)


def family_snapshot(ctx, grant, now):
    if grant is None:
        return _snapshot(ctx, 'family', {}, 'guardianship.py:GuardianshipStore.get', 'unknown')
    if grant.guardian_id != ctx.actor_id or grant.subject_id != ctx.subject_id:
        raise PermissionError('Grant belongs to another actor or subject')
    values = {'grant': {'grant_id': grant.grant_id, 'grant_type': grant.authorization_type,
        'daily_limit_cents': grant.daily_limit_cents, 'valid_from': grant.valid_from,
        'expires_at': grant.expires_at, 'revoked': grant.revoked, 'active_at_capture': grant.active(now)},
        'participants': {'actor_id': ctx.actor_id, 'subject_id': ctx.subject_id}}
    return _snapshot(ctx, 'family', values, 'guardianship.py:GuardianshipStore.get')


def budget_snapshot(ctx, preflight):
    """Project this subject's already-read payment budget; no ledger reread."""
    verified = PaymentSnapshot.from_preflight(ctx, preflight)
    check = verified.report['checks']['budget']
    values = {'budget': thaw(check['evidence']), 'budget_status': check['status'],
              'coverage': {'scope': '本次支付预算预检', 'complete': False}}
    return _snapshot(ctx, 'finance_health', values, 'preflight.py:build_payment_preflight#budget',
                     'unknown' if check['status'] == 'unknown' else 'known')


_TOOLS = {'finance_health': {'financial_health_report'}, 'subscription': {'list_subscriptions'},
          'family': {'list_family_relationships'}, 'card': {'list_cards', 'verify_card_status'},
          'antifraud': {'detect_fraud_signals'}}


def tool_snapshot(ctx, domain, result):
    tool = getattr(result, 'tool', '')
    if domain not in _TOOLS or tool not in _TOOLS[domain]:
        raise ValueError('Tool cannot supply facts for this domain')
    if domain in {'finance_health', 'subscription', 'card', 'family'} and (ctx.actor_id != 'demo-user' or ctx.subject_id != 'demo-user'):
        raise PermissionError('This legacy tool contains demo-user-only global data')
    source = 'banking_agent.py:MockBankCore.' + {'list_subscriptions':'subscriptions_query', 'list_cards':'cards_query',
        'verify_card_status':'card_status_query', 'list_family_relationships':'list_family_relationships'}.get(tool, tool)
    if getattr(result, 'ok', None) is not True:
        return _snapshot(ctx, domain, {}, source, 'unknown')
    data = result.data
    if not isinstance(data, dict):
        raise ValueError('Tool result must contain structured data')
    if domain == 'finance_health':
        # Project ledger aggregates; do not read/forward the legacy global
        # subscription_total or pretend this is a household-wide balance sheet.
        source = 'ledger_views.py:build_ledger_views'
        consumer, cash = data.get('consumer_view'), data.get('cash_flow_view')
        if not isinstance(consumer, dict) or not isinstance(cash, dict) or type(consumer.get('total_cents')) is not int or type(cash.get('outflow_cents')) is not int:
            return _snapshot(ctx, domain, {}, source, 'unknown')
        values = {'consumer': {k: consumer[k] for k in ('total_cents','by_category') if k in consumer},
                  'cash_flow': {k: cash[k] for k in ('inflow_cents','outflow_cents','net_outflow_cents','label') if k in cash},
                  'funds': data.get('funds_status', {}), 'coverage': data.get('coverage', {})}
    elif domain == 'subscription':
        items = data.get('items')
        if not isinstance(items, list):
            return _snapshot(ctx, domain, {}, source, 'unknown')
        normalized = []
        for item in items:
            debit, service = item.get('authorization_layer', {}), item.get('service_layer', {})
            normalized.append({'id': item.get('id'), 'merchant': item.get('merchant'), 'amount_cents': item.get('amount_cents'),
                'history': item.get('transaction_layer', {}).get('history', []),
                'debit_state': debit.get('debit_permission', 'unknown'), 'terminable': debit.get('terminable') is True,
                'service_state': service.get('member_status', 'unknown'), 'valid_until': service.get('valid_until'),
                'refund_state': service.get('refund_status', 'unknown'), 'refund_amount_cents': service.get('refund_amount_cents')})
        values = {'subscriptions': normalized}
    elif domain == 'card':
        items = data.get('items') if tool == 'list_cards' else [dict(data, id=ctx.entities.get('card_id'))]
        if not isinstance(items, list):
            return _snapshot(ctx, domain, {}, source, 'unknown')
        values = {'cards': [{'id': item.get('id'), 'name': item.get('name'), 'status': item.get('status', 'unknown'),
                            'last4': item.get('last4') if isinstance(item.get('last4'), str) and
                            len(item['last4']) == 4 and item['last4'].isascii() and item['last4'].isdigit() else None}
                            for item in items]}
    elif domain == 'family':
        members = data.get('members')
        if not isinstance(members, dict):
            return _snapshot(ctx, domain, {}, source, 'unknown')
        values = {'members': [{'name': name, 'relationship': item.get('relationship', '')} for name, item in members.items()]}
    else:
        values = {k: data[k] for k in ('detected', 'signals', 'is_new_recipient') if k in data}
    return _snapshot(ctx, domain, values, source)


class SnapshotAgent:
    """Pure common mechanics; each reviewed domain owns its interpretation."""
    domain = ''

    def __init__(self, snapshot):
        if type(snapshot) is not DomainSnapshot or snapshot.domain != self.domain:
            raise ValueError('Wrong snapshot domain')
        self.snapshot = snapshot

    def bind(self, ctx):
        if context_digest(ctx) != self.snapshot.context_key:
            raise ValueError('Snapshot context changed')

    def fact(self, name, default=None):
        return next((f['value'] for f in self.snapshot.facts if f['name'] == name), default)

    def finding(self, ctx, missing=(), signals=(), recommendations=()):
        self.bind(ctx)
        if self.snapshot.status == 'unknown':
            missing = tuple(missing) + ('verified_' + self.domain + '_facts',)
        return DomainFinding(self.domain, self.snapshot.facts, missing, signals, recommendations, self.snapshot.evidence)

    def validate_finding(self, ctx, finding):
        self.bind(ctx)
        if type(finding) is not DomainFinding or finding != self.analyze(ctx):
            raise ValueError('Unbound or substituted finding')

"""Payment domain sample: immutable platform preflight -> facts -> suggestions.

No bank/runtime/model/DB/tool callable is passed to this agent. Snapshot creation
belongs to the trusted platform AFTER its original policy/preflight checks.
"""
from dataclasses import dataclass
import hashlib
import json
import re

from domain_agent_contract import (DOMAIN_NAMES, DomainContext, DomainFinding,
    DomainProposal, freeze_json, thaw)

_CHECKS = {'available_funds', 'daily_limit', 'beneficiary', 'card_status', 'authentication', 'budget'}
_SOURCE = 'preflight.py:build_payment_preflight'


def context_digest(ctx):
    if type(ctx) is not DomainContext:
        raise ValueError('Expected a trusted DomainContext')
    return hashlib.sha256(json.dumps(ctx.to_dict(), ensure_ascii=True, sort_keys=True).encode('ascii')).hexdigest()


@dataclass(frozen=True)
class PaymentSnapshot:
    context_key: str
    report: object

    def __post_init__(self):
        object.__setattr__(self, 'report', freeze_json(self.report))
        report = self.report
        if (not isinstance(self.context_key, str) or len(self.context_key) != 64 or
                not hasattr(report, 'get') or report.get('read_only') is not True or
                not hasattr(report.get('checks'), 'keys') or set(report['checks']) != _CHECKS):
            raise ValueError('Expected a complete read-only platform preflight snapshot')
        for check in report['checks'].values():
            if (not hasattr(check, 'get') or check.get('status') not in {'satisfied', 'missing', 'unknown'}
                    or not hasattr(check.get('evidence'), 'keys')):
                raise ValueError('Invalid preflight dimension')

    @classmethod
    def from_preflight(cls, ctx, report):
        key = context_digest(ctx)
        raw = report.to_dict() if callable(getattr(report, 'to_dict', None)) else thaw(report)
        if (not isinstance(raw, dict) or raw.get('actor_id') != ctx.subject_id or
                raw.get('amount_cents') != ctx.entities.get('amount_cents') or
                raw.get('beneficiary') != ctx.entities.get('beneficiary')):
            raise ValueError('Preflight subject or payment entities do not match context')
        return cls(key, raw)


class TransactionAgent:
    domain = 'payment'

    def __init__(self, snapshot):
        if type(snapshot) is not PaymentSnapshot:
            raise ValueError('Only platform-prepared immutable snapshots are accepted')
        self.snapshot = snapshot

    def _bind(self, ctx):
        if context_digest(ctx) != self.snapshot.context_key:
            raise ValueError('Snapshot cannot be reused across context or identity')

    def analyze(self, ctx):
        self._bind(ctx)
        facts, missing, signals, references = [], [], [], []
        amount, beneficiary = ctx.entities.get('amount_cents'), ctx.entities.get('beneficiary')
        if type(amount) is not int or amount <= 0:
            missing.append('amount_cents')
        if not beneficiary:
            missing.append('beneficiary')
        for key, check in self.snapshot.report['checks'].items():
            reference = _SOURCE + '#' + key
            facts.append({'check': key, 'status': check['status'], 'evidence': thaw(check['evidence']),
                          'source': reference, 'source_kind': 'platform_read_only_report'})
            references.append(reference)
            if key != 'authentication' and check['status'] != 'satisfied':
                signals.append('payment_check_' + check['status'] + ':' + key)
            if key == 'beneficiary' and beneficiary and check['status'] != 'satisfied':
                missing.append('beneficiary_account')
        return DomainFinding(self.domain, facts, missing, signals,
            ['预检为当前只读快照；请求金额为请求回显，派生金额可追溯到预检，不代表已执行或已获授权。'], references)

    def propose(self, ctx, finding):
        self._bind(ctx)
        # Never trust a caller-substituted finding to change this reviewed sample.
        if type(finding) is not DomainFinding or finding != self.analyze(ctx):
            raise ValueError('Finding is not the result of this bound payment snapshot')
        checks = self.snapshot.report['checks']
        required = checks['authentication']['evidence'].get('required')
        confirmations = ['explicit_user_confirmation'] if required in {'confirmation', 'strong_auth'} else []
        authentication = ['strong_auth'] if required == 'strong_auth' else []
        options, steps = [], []
        if checks['budget']['status'] == 'missing':
            options = [{'action': name} for name in ('adjust_amount', 'adjust_budget', 'defer', 'human_handoff')]
        elif finding.missing_fields:
            options = [{'action': 'clarify', 'fields': list(finding.missing_fields)}]
        elif any(checks[k]['status'] != 'satisfied' for k in _CHECKS - {'authentication'}):
            options = [{'action': 'recheck_facts'}, {'action': 'human_handoff'}]
        else:
            amount, name = ctx.entities['amount_cents'], ctx.entities['beneficiary']
            steps = [{'tool': 'resolve_beneficiary', 'params': {'beneficiary': name}},
                     {'tool': 'check_transfer_constraints', 'params': {'amount_cents': amount}},
                     {'tool': 'create_transfer', 'params': {'beneficiary': name, 'amount_cents': amount}}]
        return DomainProposal(steps, confirmations, authentication, options,
            '平台将 Mock 只读预检替换为真实银行 Adapter；权限、用户确认、MFA、幂等、执行及对账仍由安全执行平面负责。')


@dataclass(frozen=True)
class AgentMessage:
    trace_id: str
    session_id: str
    sender: str
    recipient: str
    context_key: str
    payload: object

    def __post_init__(self):
        if any(type(v) is not str or not v.strip() or len(v) > 128 for v in (self.trace_id, self.session_id)) or not isinstance(self.context_key, str) or not re.fullmatch('[0-9a-f]{64}', self.context_key):
            raise ValueError('Agent envelope requires bounded identifiers and context digest')
        if self.sender not in DOMAIN_NAMES | {'supervisor'} or self.recipient not in DOMAIN_NAMES | {'supervisor'}:
            raise ValueError('Unknown internal agent endpoint')
        if type(self.payload) not in (DomainFinding, DomainProposal):
            raise ValueError('Agent envelopes only carry validated facts or suggestions')
        if type(self.payload) is DomainFinding and self.payload.domain != self.sender:
            raise ValueError('Finding sender must match domain')

    @classmethod
    def from_context(cls, ctx, sender, recipient, payload):
        return cls(ctx.trace_id, ctx.session_id, sender, recipient, context_digest(ctx), payload)

    def to_dict(self):
        return dict(trace_id=self.trace_id, session_id=self.session_id, sender=self.sender,
                    recipient=self.recipient, context_key=self.context_key, payload=self.payload.to_dict())


@dataclass(frozen=True)
class AgentHandoff:
    trace_id: str
    session_id: str
    sender: str
    recipient: str
    reason: str
    required_fields: tuple

    def __post_init__(self):
        if any(type(v) is not str or not v.strip() or len(v) > 128 for v in (self.trace_id, self.session_id)):
            raise ValueError('Handoff requires bounded trace and session identifiers')
        if self.sender not in DOMAIN_NAMES | {'supervisor'} or self.recipient not in DOMAIN_NAMES | {'supervisor'}:
            raise ValueError('Unknown internal agent endpoint')
        if self.reason not in {'missing_information', 'domain_boundary', 'unknown_result'}:
            raise ValueError('Unknown domain handoff reason')
        if type(self.required_fields) not in (tuple, list) or any(type(k) is not str or not k.strip() for k in self.required_fields):
            raise ValueError('Expected missing field identifiers')
        object.__setattr__(self, 'required_fields', freeze_json(self.required_fields))

    @classmethod
    def from_context(cls, ctx, sender, recipient, reason, required_fields):
        context_digest(ctx)
        return cls(ctx.trace_id, ctx.session_id, sender, recipient, reason, required_fields)

    def to_dict(self):
        return dict(trace_id=self.trace_id, session_id=self.session_id, sender=self.sender,
                    recipient=self.recipient, reason=self.reason, required_fields=list(self.required_fields))

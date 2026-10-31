"""Read-only domain ABI. Data validation grants no execution permission.

This is a collaboration boundary for reviewed local Python, not a sandbox for
hostile modules. The platform must still bind identities, verify provenance,
and re-evaluate PolicyEngine before every execution.
"""
from collections.abc import Mapping
from dataclasses import dataclass, fields
import json
import re
from types import MappingProxyType
from typing import Protocol, runtime_checkable

DOMAIN_NAMES = frozenset({'payment', 'antifraud', 'finance_health', 'family', 'subscription', 'card'})
_AUTHORITY = {'allow', 'allowed', 'authorized', 'approved', 'authorization', 'authz', 'permission',
              'policy_override', 'skip_auth', 'execute_now', 'action_token', 'consent_token',
              'credential', 'credential_id', 'password', 'otp', 'verification_code'}


def thaw(value):
    if isinstance(value, Mapping):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [thaw(v) for v in value]
    return value


def freeze_json(value, *, deny_authority=False):
    count = 0
    def visit(item, key='', depth=0):
        nonlocal count
        count += 1
        if count > 2048 or depth > 12:
            raise ValueError('Domain payload exceeds structural bounds')
        if key.endswith('_cents') and item is not None and type(item) is not int:
            raise ValueError('Money must be integer cents, or null for unknown')
        if isinstance(item, Mapping):
            if len(item) > 128:
                raise ValueError('Too many structured fields')
            result = {}
            for name, child in item.items():
                if type(name) is not str or not name or len(name) > 128:
                    raise ValueError('Invalid structured field name')
                split_name = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
                split_name = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', split_name)
                normalized = re.sub(r'[^a-z0-9]+', '_', split_name.casefold()).strip('_')
                if deny_authority and (normalized in _AUTHORITY or
                        any(normalized.startswith(p + '_') or normalized.endswith('_' + p)
                            for p in ('allow', 'allowed', 'authorized', 'approved', 'authz', 'permission'))):
                    raise ValueError('Domain output cannot claim authority or carry credentials')
                result[name] = visit(child, name, depth + 1)
            return MappingProxyType(result)
        if type(item) in (list, tuple):
            if len(item) > 128:
                raise ValueError('Too many domain items')
            return tuple(visit(child, '', depth + 1) for child in item)
        if type(item) not in (str, int, bool, type(None)):
            raise ValueError('JSON-only domain data; floats are forbidden')
        if type(item) is str and len(item) > 4096:
            raise ValueError('Domain string exceeds bound')
        if type(item) is int and abs(item) > 2**63 - 1:
            raise ValueError('Integer exceeds bound')
        return item
    frozen = visit(value)
    if len(json.dumps(thaw(frozen), ensure_ascii=True).encode('ascii')) > 131072:
        raise ValueError('Domain payload exceeds byte bound')
    return frozen


def _text(value, limit=128):
    if type(value) is not str or not value.strip() or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError('Invalid domain text or identifier')


def _strings(value):
    if type(value) not in (list, tuple) or any(type(v) is not str or not v.strip() for v in value):
        raise ValueError('Expected a sequence of nonempty strings')


class _Contract:
    def to_dict(self):
        return {f.name: thaw(getattr(self, f.name)) for f in fields(self)}

    def _freeze(self):
        for f in fields(self):
            object.__setattr__(self, f.name, freeze_json(getattr(self, f.name), deny_authority=True))


@dataclass(frozen=True)
class DomainContext(_Contract):
    trace_id: str
    session_id: str
    actor_id: str
    subject_id: str
    device_id: str
    channel: str
    user_input: str
    entities: Mapping

    def __post_init__(self):
        for name in ('trace_id', 'session_id', 'actor_id', 'subject_id', 'device_id', 'channel'):
            _text(getattr(self, name))
        if type(self.user_input) is not str or len(self.user_input) > 2000 or not isinstance(self.entities, Mapping):
            raise ValueError('Invalid bounded domain input')
        self._freeze()


@dataclass(frozen=True)
class DomainFinding(_Contract):
    domain: str
    facts: tuple
    missing_fields: tuple
    risk_signals: tuple
    recommendations: tuple
    evidence: tuple

    def __post_init__(self):
        if self.domain not in DOMAIN_NAMES or type(self.facts) not in (list, tuple) or any(not isinstance(f, Mapping) for f in self.facts):
            raise ValueError('Invalid domain or facts')
        for name in ('missing_fields', 'risk_signals', 'recommendations', 'evidence'):
            _strings(getattr(self, name))
        if self.facts and not self.evidence:
            raise ValueError('Facts require provenance references; references still require platform verification')
        self._freeze()


@dataclass(frozen=True)
class DomainProposal(_Contract):
    suggested_steps: tuple
    required_confirmations: tuple
    required_auth: tuple
    user_options: tuple
    production_replacement: str

    def __post_init__(self):
        if type(self.suggested_steps) not in (tuple, list) or type(self.user_options) not in (tuple, list):
            raise ValueError('Invalid proposal sequences')
        for step in self.suggested_steps:
            if not isinstance(step, Mapping) or set(step) - {'tool', 'params', 'title'} or not {'tool', 'params'} <= set(step):
                raise ValueError('Steps only contain tool, params and optional title; no retry or authority')
            _text(step['tool'])
            if not isinstance(step['params'], Mapping):
                raise ValueError('Tool parameters must be structured data')
        for name in ('required_confirmations', 'required_auth'):
            _strings(getattr(self, name))
        if any(not isinstance(option, Mapping) for option in self.user_options):
            raise ValueError('User options must be structured data')
        _text(self.production_replacement, 4096)
        self._freeze()


@runtime_checkable
class DomainAgent(Protocol):
    domain: str

    def analyze(self, ctx: DomainContext) -> DomainFinding: ...

    def propose(self, ctx: DomainContext, finding: DomainFinding) -> DomainProposal: ...

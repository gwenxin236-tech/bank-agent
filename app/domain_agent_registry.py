"""Atomic registration of six reviewed local factories, not tool permissions."""
import importlib
from types import MappingProxyType

from domain_agent_contract import DOMAIN_NAMES, DomainAgent
from domain_snapshots import DomainSnapshot
from transaction_agent import PaymentSnapshot


class _ScopedRegistrar:
    def __init__(self, domain, staged):
        self._domain, self._staged = domain, staged

    def register(self, factory):
        if (not isinstance(factory, type) or getattr(factory, 'domain', None) != self._domain or
                factory.__module__ != 'agents.' + self._domain + '_agent' or
                not callable(getattr(factory, 'analyze', None)) or not callable(getattr(factory, 'propose', None))):
            raise ValueError('Factory must implement its reviewed local domain')
        if self._domain in self._staged:
            raise ValueError('Duplicate domain registration')
        self._staged[self._domain] = factory


class DomainAgentRegistry:
    def __init__(self, factories):
        if set(factories) != DOMAIN_NAMES:
            raise ValueError('All six internal domains must be registered before publication')
        self._factories = MappingProxyType(dict(factories))

    @property
    def factories(self):
        return self._factories

    @property
    def names(self):
        return tuple(sorted(self.factories))

    def create(self, domain, snapshot):
        if domain not in self.factories:
            raise ValueError('Unregistered domain')
        if domain == 'payment':
            if type(snapshot) is not PaymentSnapshot:
                raise ValueError('Payment requires the T10 preflight snapshot')
        elif type(snapshot) is not DomainSnapshot or snapshot.domain != domain:
            raise ValueError('Snapshot belongs to another domain')
        agent = self.factories[domain](snapshot)
        if not isinstance(agent, DomainAgent) or agent.domain != domain:
            raise ValueError('Factory returned an invalid domain agent')
        return agent


def load_domain_agents(import_module=importlib.import_module):
    staged = {}
    try:
        for domain in sorted(DOMAIN_NAMES):
            module = import_module('agents.' + domain + '_agent')
            module.register(_ScopedRegistrar(domain, staged))
        return DomainAgentRegistry(staged)
    except Exception as error:
        raise ValueError('Domain registration failed; no partial registry was published') from error


DOMAIN_AGENT_REGISTRY = load_domain_agents()

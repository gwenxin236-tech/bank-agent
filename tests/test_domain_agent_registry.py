import types
import unittest
from unittest.mock import patch

from domain_agent_contract import DOMAIN_NAMES
from domain_agent_registry import DOMAIN_AGENT_REGISTRY, load_domain_agents


class DomainRegistryTests(unittest.TestCase):
    def test_six_scoped_factories_and_immutable_publication(self):
        self.assertEqual(set(DOMAIN_AGENT_REGISTRY.names), DOMAIN_NAMES)
        with self.assertRaises(TypeError):
            DOMAIN_AGENT_REGISTRY.factories['payment'] = object

    def test_duplicate_foreign_domain_and_foreign_module_rejected(self):
        import importlib
        real_import = importlib.import_module
        for mode in ('duplicate', 'foreign_domain', 'foreign_module'):
            def importer(name):
                module = real_import(name)
                if name != 'agents.antifraud_agent':
                    return module
                def register(registry):
                    factory = module.AntifraudAgent
                    if mode == 'duplicate':
                        registry.register(factory); registry.register(factory)
                    elif mode == 'foreign_domain':
                        registry.register(type('Bad', (factory,), {'domain': 'card', '__module__': name}))
                    else:
                        registry.register(type('Bad', (factory,), {'__module__': 'external.plugin'}))
                return types.SimpleNamespace(register=register)
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                load_domain_agents(import_module=importer)
            self.assertEqual(set(DOMAIN_AGENT_REGISTRY.names), DOMAIN_NAMES)

    def test_missing_registration_and_partial_failure_not_published(self):
        with self.assertRaises(ValueError):
            load_domain_agents(import_module=lambda name: types.SimpleNamespace(register=lambda registry: None))
        def broken(name):
            raise RuntimeError('synthetic import failure')
        with self.assertRaises(ValueError):
            load_domain_agents(import_module=broken)
        self.assertEqual(set(DOMAIN_AGENT_REGISTRY.names), DOMAIN_NAMES)

    def test_user_selected_path_and_wrong_snapshot_refused(self):
        for name in ('../external', 'supervisor', 'payment'):
            with self.assertRaises(ValueError):
                DOMAIN_AGENT_REGISTRY.create(name, object())


if __name__ == '__main__':
    unittest.main()

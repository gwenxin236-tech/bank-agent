"""Offline distribution integrity and no-download startup contracts."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import io
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('offline_release', Path(__file__).resolve().parents[1] / 'scripts/offline_release.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class OfflineReleaseTests(unittest.TestCase):
    def test_optional_ocr_and_reranker_features_are_explicit_and_local(self):
        config=release.compose_config('bank-agent-offline:test',local_rag=True,local_reranker=True,local_ocr=True)
        env=config['services']['banking-agent']['environment']
        self.assertEqual(env['RAG_USE_RERANKER'],'1')
        self.assertEqual(env['OCR_ENABLED'],'1')
        self.assertEqual(env['OCR_MODEL_DIR'],'/app/models/ocr')
        with self.assertRaises(ValueError): release.compose_config('bank-agent-offline:test',local_reranker=True)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def bundle(self):
        for name in release.PAYLOAD_NAMES:
            (self.root / name).write_bytes(b'fixture')
        manifest = {'schema': 1, 'platform': 'linux/amd64',
                    'images': {'app': {'tag': 'bank-agent-offline:test', 'id': 'sha256:' + 'a' * 64},
                               'sandbox': {'tag': 'python:3.12-slim', 'id': 'sha256:' + 'b' * 64}},
                    'files': {n: release.sha256(self.root / n) for n in release.PAYLOAD_NAMES}}
        (self.root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        return manifest

    def test_valid_bundle_and_tampered_archive(self):
        self.bundle()
        self.assertEqual(release.verify_bundle(self.root)['schema'], 1)
        (self.root / 'images.tar').write_bytes(b'tampered')
        with self.assertRaises(ValueError): release.verify_bundle(self.root)

    def test_missing_file_is_not_accepted(self):
        self.bundle()
        (self.root / 'compose.json').unlink()
        with self.assertRaises(ValueError): release.verify_bundle(self.root)

    def test_traversal_and_incomplete_inventory_rejected(self):
        manifest = self.bundle()
        for files in ({'../outside': 'a' * 64}, {'images.tar': 'a' * 64}):
            manifest['files'] = files
            (self.root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaises(ValueError): release.verify_bundle(self.root)

    def test_wrong_platform_and_image_roles_rejected(self):
        manifest = self.bundle()
        for field, value in [('platform', 'linux/arm64'), ('images', {'app': manifest['images']['app']})]:
            changed = dict(manifest, **{field: value})
            (self.root / 'manifest.json').write_text(json.dumps(changed), encoding='utf-8')
            with self.assertRaises(ValueError): release.verify_bundle(self.root)

    def test_symlink_payload_rejected(self):
        self.bundle()
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaises(ValueError): release.verify_bundle(self.root)

    def test_compose_has_no_build_no_pull_or_external_network(self):
        config = release.compose_config('bank-agent-offline:test')
        service = config['services']['banking-agent']
        self.assertNotIn('build', service)
        self.assertEqual(service['pull_policy'], 'never')
        self.assertEqual(service['ports'][0]['host_ip'], '127.0.0.1')
        self.assertTrue(config['networks']['default']['internal'])
        self.assertEqual(service['environment']['BANK_AGENT_OFFLINE'], '1')
        self.assertNotIn('LLM_API_KEY', service['environment'])
        self.assertEqual(service['volumes'], ['demo-data:/app/data'])

    def test_staging_only_copies_approved_source_not_state_or_secrets(self):
        (self.root / 'app').mkdir()
        for name in ['main.py', 'state.sqlite3', '.env', 'secret.zip']:
            (self.root / 'app' / name).write_text('fixture', encoding='utf-8')
        stage = self.root / 'stage'
        stage.mkdir()
        entries = release.stage_source(self.root, stage)
        self.assertEqual([x['path'] for x in entries], ['app/main.py'])
        self.assertFalse((stage / 'app/.env').exists())

    def test_staging_rejects_symlink_without_following_it(self):
        (self.root / 'app').mkdir()
        (self.root / 'app/link.py').write_text('fixture', encoding='utf-8')
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaises(ValueError): release.stage_source(self.root, self.root / 'stage')

    def test_distribution_keeps_attack_fixtures_and_feature_documentation(self):
        (self.root / 'eval').mkdir()
        (self.root / 'eval/attack_matrix.json').write_text('[]', encoding='utf-8')
        (self.root / 'app/features').mkdir(parents=True)
        (self.root / 'app/features/README.md').write_text('feature contract', encoding='utf-8')
        entries = release.stage_source(self.root, self.root / 'stage')
        self.assertEqual({e['path'] for e in entries}, {'eval/attack_matrix.json', 'app/features/README.md'})

    def test_existing_release_is_never_overwritten_or_sent_to_docker(self):
        with patch.object(release, 'docker') as docker:
            with self.assertRaises(ValueError): release.build_bundle(self.root)
            docker.assert_not_called()

    def test_local_rag_bundle_enables_local_without_cloud_credentials(self):
        config=release.compose_config('bank-agent-offline:test', local_rag=True)
        service = config['services']['banking-agent']
        self.assertEqual(service['environment']['RAG_EMBEDDING_BACKEND'], 'local')
        self.assertEqual(service['environment']['RAG_USE_MODEL'], '0')
        self.assertEqual(service['environment']['BANK_AGENT_OFFLINE'], '1')
        self.assertNotIn('LLM_API_KEY', service['environment'])
        self.assertNotIn('ports',service)
        self.assertEqual(service['networks'],['default'])
        self.assertTrue(config['networks']['default']['internal'])
        self.assertEqual(config['services']['web']['ports'][0]['host_ip'],'127.0.0.1')
        self.assertEqual(config['services']['web']['image'],service['image'])

    def test_missing_local_assets_block_staging_instead_of_silent_lexical_release(self):
        with self.assertRaises((ValueError, OSError)):
            release.stage_source(self.root, self.root / 'stage', local_rag=True)

    def test_release_does_not_claim_rag_is_deferred(self):
        self.assertNotIn('RAG is deferred', release.START_HERE)
        self.assertIn('local semantic', release.START_HERE)

    def test_local_web_relay_has_fixed_upstream_and_rejects_absolute_urls(self):
        from unittest.mock import Mock
        with patch('http.server.ThreadingHTTPServer'):
            scope={}
            exec(release.LOCAL_WEB_PROXY,scope)
        handler=object.__new__(scope['Handler'])
        handler.path='/api/state';handler.command='GET';handler.headers={'Host':'outside.example'}
        handler.rfile=io.BytesIO();handler.wfile=io.BytesIO()
        for name in ('send_response','send_header','end_headers','send_error'):setattr(handler,name,Mock())
        with patch('http.client.HTTPConnection') as connection:
            response=connection.return_value.getresponse.return_value
            response.status=200;response.read.return_value=b'{}';response.getheaders.return_value=[]
            handler.relay()
            connection.assert_called_once_with('banking-agent',8090,timeout=30)
            self.assertNotIn('Host',connection.return_value.request.call_args.kwargs['headers'])
        for path in ('https://outside.example/secret','//outside.example/secret'):
            handler.path=path
            with patch('http.client.HTTPConnection') as connection:
                handler.relay();connection.assert_not_called()

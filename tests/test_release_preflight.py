"""Release checks must not expose values or read private/state material."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('release_preflight', Path(__file__).resolve().parents[1] / 'scripts/release_preflight.py')
release = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(release)


class ReleasePreflightTests(unittest.TestCase):
    def test_private_paths_and_traversal_never_become_candidates(self):
        for name in ['_internal/secret.md', 'docs/_internal/secret.md', '../app/a.py', '/app/a.py', 'C:/app/a.py', '.git/config']:
            self.assertEqual(release.classify_path(name), 'EXCLUDED_PRIVATE')

    def test_cache_and_database_are_excluded_at_every_depth(self):
        for name in ['data/verification/r/report.json', 'data/verification/r/a.sqlite3', '.qa-tools/releases/images.tar', '.env', '.env.local', 'app/__pycache__/a.pyc']:
            self.assertEqual(release.classify_path(name), 'EXCLUDED_STATE')
        self.assertEqual(release.classify_path('.env.example'), 'CANDIDATE_TEXT')

    def test_binary_and_unselected_root_material_need_manual_review(self):
        self.assertEqual(release.classify_path('docs/old.docx'), 'REVIEW_BINARY')
        self.assertEqual(release.classify_path('人工智能赛道原始要求.docx'), 'REVIEW_BINARY')
        self.assertEqual(release.classify_path('research.md'), 'REVIEW_UNSELECTED')
        self.assertEqual(release.classify_path('submission/video.mp4'), 'REVIEW_BINARY')

    def test_high_confidence_secret_is_reported_without_the_secret(self):
        secret = 'sk-' + 'Ab12' * 10
        findings = release.scan_text('app/x.py', 'key = ' + repr(secret))
        self.assertTrue(any(x['severity'] == 'BLOCK' for x in findings))
        serialized = json.dumps(findings)
        self.assertNotIn(secret, serialized)
        self.assertEqual(findings[0]['line'], 1)

    def test_demo_literal_and_unknown_credential_are_distinguished(self):
        demo = release.scan_text('tests/x.py', 'api_key = "test-secret"')
        self.assertEqual(demo[0]['severity'], 'FIXTURE_REVIEW')
        unknown = release.scan_text('app/x.py', 'api_key = "unconfirmed-value"')
        self.assertEqual(unknown[0]['severity'], 'REVIEW')
        self.assertNotIn('unconfirmed-value', json.dumps(unknown))

    def test_phone_and_email_values_are_not_echoed(self):
        phone = '138' + '0013' + '8000'
        email = 'member' + '@' + 'example.com'
        findings = release.scan_text('docs/x.md', phone + '\n' + email)
        self.assertEqual({x['line'] for x in findings}, {1, 2})
        self.assertNotIn(phone, json.dumps(findings))
        self.assertNotIn(email, json.dumps(findings))

    def test_symlink_is_rejected_before_file_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'app').mkdir(); (root/'app/x.py').write_text('fixture', encoding='utf-8')
            with patch.object(Path, 'is_symlink', return_value=True):
                with self.assertRaises(ValueError): release.read_candidate(root, 'app/x.py')

    def test_summary_rejects_skips_or_failed_run(self):
        valid = {'status': 'PASS', 'tests_run': 1, 'skipped': 0, 'resource_warnings': 0, 'original_database_unchanged': True}
        self.assertTrue(release.acceptable_test_report(valid))
        for field, value in [('status','FAIL'), ('skipped',1), ('resource_warnings',1), ('original_database_unchanged',False), ('tests_run',0)]:
            self.assertFalse(release.acceptable_test_report(dict(valid, **{field:value})))

    def test_private_and_binary_content_is_not_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(Path, 'read_bytes', side_effect=AssertionError('must not read')):
                for name in ['_internal/a.md', 'data/verification/a.json', 'docs/a.docx']:
                    with self.assertRaises(ValueError): release.read_candidate(Path(tmp), name)

    def test_public_summary_does_not_copy_raw_report_or_host_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root/'app').mkdir()
            (root/'app/main.py').write_text('fixture', encoding='utf-8')
            report = {'status': 'PASS', 'tests_run': 1, 'skipped': 0, 'resource_warnings': 0,
                      'original_database_unchanged': True, 'test_output': 'PRIVATE_RAW_OUTPUT', 'node_binary': str(root)}
            summary = release.public_test_summary(root, report)
            self.assertEqual(summary['application_files'][0]['path'], 'app/main.py')
            self.assertNotIn('PRIVATE_RAW_OUTPUT', json.dumps(summary))
            self.assertNotIn(str(root), json.dumps(summary))

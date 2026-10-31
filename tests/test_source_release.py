"""Release integrity and strict CI gates; no model, bank or paid API calls."""
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import source_release as release
import ci_verify as ci


class SourceReleaseTests(unittest.TestCase):
    def fixture(self, root):
        (root / 'app').mkdir()
        (root / 'app/main.py').write_text('print("current untracked source")\n', encoding='utf-8')
        (root / 'README.md').write_text('demo\n', encoding='utf-8')
        (root / '.env').write_text('PRIVATE=never pack\n', encoding='utf-8')
        (root / 'data').mkdir()
        (root / 'data/state.sqlite3').write_bytes(b'private state')

    def test_current_tree_deterministic_and_private_state_excluded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'source'; root.mkdir(); self.fixture(root)
            a, b = Path(tmp) / 'a.zip', Path(tmp) / 'b.zip'
            x = release.build(root, a); y = release.build(root, b)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            self.assertEqual(x['snapshot_id'], y['snapshot_id'])
            self.assertEqual(set(x['files']), {'README.md', 'app/main.py'})
            self.assertEqual(release.verify(a)['snapshot_id'], x['snapshot_id'])

    def test_copy_extract_and_tamper_detected_before_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'source'; root.mkdir(); self.fixture(root)
            archive = Path(tmp) / 'source.zip'; release.build(root, archive)
            target = Path(tmp) / '迁移 source'
            release.extract(archive, target)
            self.assertEqual((target / 'app/main.py').read_bytes(), (root / 'app/main.py').read_bytes())
            with self.assertRaises(ValueError): release.extract(archive, target)
            bad = Path(tmp) / 'tamper.zip'
            with zipfile.ZipFile(archive) as z, zipfile.ZipFile(bad, 'w') as out:
                for name in z.namelist(): out.writestr(name, b'changed' if name == 'app/main.py' else z.read(name))
            with self.assertRaises(ValueError): release.extract(bad, Path(tmp) / 'must-not-exist')
            self.assertFalse((Path(tmp) / 'must-not-exist').exists())

    def test_do_not_overwrite_existing_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'old.zip'; path.write_bytes(b'keep')
            with self.assertRaises(ValueError): release.build(Path(tmp), path)
            self.assertEqual(path.read_bytes(), b'keep')

    def test_private_scope_rejected_without_traversal(self):
        for path in ('_internal/private.md', '.git/config', 'data/a.json', '.qa-tools/key.py', '.env', 'app/.env', '../app/a.py', '/app/a.py', 'C:/a.py', 'app/__pycache__/a.py', 'app/link.sqlite3'):
            self.assertFalse(release.allowed(path), path)
        self.assertTrue(release.allowed('requirements-rag-local.txt'))
        self.assertTrue(release.allowed('.github/workflows/ci.yml'))

    def test_symlink_and_secret_block_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root)
            with patch.object(Path, 'is_symlink', return_value=True):
                with self.assertRaises(ValueError): release.build(root, root / 'link.zip')
            (root / 'app/main.py').write_text('key="' + 'sk' + '-' + 'x' * 32 + '"', encoding='utf-8')
            with self.assertRaises(ValueError): release.build(root, root / 'secret.zip')
            self.assertFalse((root / 'secret.zip').exists())

    def test_unsafe_archive_names_and_extra_members_rejected(self):
        for name in ('../escape', '/absolute', 'app\\x.py', 'app/x:stream', '.env', 'app/a.py', 'APP/A.PY'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'bad.zip'
                with zipfile.ZipFile(path, 'w') as out:
                    out.writestr('SOURCE_MANIFEST.json', json.dumps({'schema': 1, 'files': {}, 'snapshot_id': 'bad'}))
                    out.writestr(name, b'extra')
                with self.assertRaises(ValueError): release.verify(path)

    def test_missing_manifest_is_not_a_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bad.zip'
            with zipfile.ZipFile(path, 'w') as out: out.writestr('README.md', b'x')
            with self.assertRaises(ValueError): release.verify(path)

    def test_review_findings_are_retained_and_not_publication_clearance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.fixture(root)
            (root / 'README.md').write_text('contact user@example.com\n', encoding='utf-8')
            result = release.build(root, root / 'source.zip')
            self.assertTrue(result['review_findings'])
            self.assertFalse(result['publication_authorized'])


class StrictCITests(unittest.TestCase):
    def test_browser_selection_is_explicit_and_partitioned(self):
        class Plain(unittest.TestCase):
            def test_one(self): pass
            def test_browser(self):
                marker = 'C7_PLAYWRIGHT_MODULE'
                self.assertTrue(marker)
        tests = list(ci.leaves(unittest.defaultTestLoader.loadTestsFromTestCase(Plain)))
        core, excluded = ci.select(tests, 'core')
        browser, _ = ci.select(tests, 'browser')
        self.assertEqual(len(core), 1); self.assertEqual(len(browser), 1)
        self.assertEqual(excluded[0]['reason'], 'separate browser job')
        self.assertEqual(set(t.id() for t in core + browser), set(t.id() for t in tests))

    def test_skip_warning_empty_and_failure_are_not_pass(self):
        self.assertTrue(ci.acceptable(True, 1, 0, 0, True))
        for args in ((True, 0, 0, 0, True), (True, 1, 1, 0, True), (True, 1, 0, 1, True), (False, 1, 0, 0, True), (True, 1, 0, 0, False)):
            self.assertFalse(ci.acceptable(*args))

    def test_offline_environment_drops_cloud_secrets_and_uses_temp_db(self):
        env = ci.safe_environment({'LLM_API_KEY': 'not-real', 'OPENAI_API_KEY': 'not-real', 'PATH': 'keep'}, '/tmp/isolated.sqlite3')
        self.assertNotIn('LLM_API_KEY', env); self.assertNotIn('OPENAI_API_KEY', env)
        self.assertEqual(env['BANK_AGENT_OFFLINE'], '1')
        self.assertEqual(env['C8_RUN_DRILL'], '1')
        self.assertEqual(env['BANK_AGENT_DB_PATH'], '/tmp/isolated.sqlite3')
        self.assertEqual(env['PATH'], 'keep')

    def test_unknown_scope_rejected(self):
        with self.assertRaises(ValueError): ci.select([], 'silent-skip')

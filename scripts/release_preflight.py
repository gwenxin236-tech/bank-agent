#!/usr/bin/env python3
"""Read-only local release inventory, redacted text checks and optional Git blob scan.

Never reads private/state/binary payloads, changes Git, accesses a remote, or
declares publication approved. Reports need human review; regexes are not a
complete credential/privacy audit. All paths in public output are relative.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = {'_internal', '.git', '.codex', '.agents'}
STATE = {'data', '.qa-tools', '__pycache__', 'node_modules'}
DIRS = {'app', 'config', 'scripts', 'tests', 'docs', 'eval', 'prompts', '.github'}
ROOT_FILES = {'README.md', 'AGENTS.md', 'CHANGELOG.md', 'Dockerfile', 'docker-compose.yml',
              '.gitignore', '.dockerignore', '.env.example', 'run.sh', 'requirements.txt',
              'requirements-production.txt', '领域Agent开发手册.md', 'PUSH与发布操作清单.md', '.github/CODEOWNERS'}
TEXT = {'.py', '.mjs', '.js', '.json', '.md', '.txt', '.yml', '.yaml', '.html', '.css', '.svg', '.sh'}
BINARY = {'.docx', '.pptx', '.pdf', '.zip', '.tar', '.mp4', '.webm', '.sqlite3', '.db', '.png', '.jpg'}
DEMO = {'test-secret', 'test-key', 'demo-secret', 'demo-token', 'demo-user-session',
        'demo-only', 'change-me', 'agent_dev_only', '123456', 'test-token', 'secret-for-test'}


def classify_path(name):
    name = name.replace('\\', '/')
    parts = PurePosixPath(name).parts
    lowered = {p.lower() for p in parts}
    if not parts or name.startswith('/') or ':' in name or '..' in parts or lowered & PRIVATE:
        return 'EXCLUDED_PRIVATE'
    if lowered & STATE or (parts[-1].startswith('.env') and name != '.env.example') or parts[-1].endswith(('.pyc', '.sqlite3-wal', '.sqlite3-shm')):
        return 'EXCLUDED_STATE'
    if PurePosixPath(name).suffix.lower() in BINARY:
        return 'REVIEW_BINARY'
    if name in ROOT_FILES or parts[0] in DIRS and PurePosixPath(name).suffix.lower() in TEXT:
        return 'CANDIDATE_TEXT'
    return 'REVIEW_UNSELECTED'


def read_candidate(root, name):
    if classify_path(name) != 'CANDIDATE_TEXT': raise ValueError('not an approved text scope')
    path = root / name
    for item in [path, *path.parents]:
        if item == root.parent: break
        if item.is_symlink(): raise ValueError('symlink rejected before read')
    if root.resolve() not in path.resolve().parents: raise ValueError('path escaped root')
    if path.stat().st_size > 2 * 1024 * 1024: raise ValueError('text size requires manual review')
    return path.read_bytes()


def scan_text(name, text):
    findings = []
    for line_number, line in enumerate(text.splitlines(), 1):
        def add(rule, severity):
            finding = {'path': name, 'line': line_number, 'rule': rule, 'severity': severity}
            if finding not in findings: findings.append(finding)
        if re.search(r'\b(?:sk-[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{24,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[A-Z0-9]{16})\b', line):
            add('credential_prefix', 'BLOCK')
        if re.search(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----', line):
            add('private_key_header', 'BLOCK')
        literals = re.findall(r'''(?i)\b(?:api_key|api-key|password|secret|token|LLM_API_KEY|OPENAI_API_KEY)\b["']?\s*[:=]\s*["']([^"'\r\n]{8,})["']''', line)
        literals += re.findall(r'^\s*(?:LLM_API_KEY|OPENAI_API_KEY|BANK_AGENT_DEMO_ADMIN_TOKEN)\s*=\s*([^\s#]+)', line)
        for value in literals:
            value = value.strip('"\' ')
            if not value: continue
            demo = value in DEMO or value.startswith(('<', '${', 'os.getenv', 'process.env'))
            add('credential_literal', 'FIXTURE_REVIEW' if demo else 'REVIEW')
        if re.search(r'(?<!\d)1[3-9]\d{9}(?!\d)', line): add('phone_literal', 'REVIEW')
        if re.search(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', line): add('email_literal', 'REVIEW')
    return findings


def git(root, *args, data=None):
    result = subprocess.run(['git', '-c', 'core.quotepath=false', '--no-optional-locks', *args],
                            cwd=root, input=data, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if result.returncode: raise RuntimeError('read-only git command failed: ' + args[0])
    return result.stdout


def acceptable_test_report(report):
    return (report.get('status') == 'PASS' and type(report.get('tests_run')) is int and report['tests_run'] > 0
            and report.get('skipped') == 0 and report.get('resource_warnings') == 0
            and report.get('original_database_unchanged') is True)


def history_text_scan(root):
    # Paths are obtained from Git metadata. Excluded content is never requested.
    entries = []
    skipped = Counter()
    for line in git(root, 'rev-list', '--objects', '--all').decode('utf-8').splitlines():
        oid, separator, name = line.partition(' ')
        if not separator: continue
        kind = classify_path(name)
        if kind != 'CANDIDATE_TEXT': skipped[kind] += 1; continue
        entries.append((oid, name))
    sizes = git(root, 'cat-file', '--batch-check=%(objectname) %(objecttype) %(objectsize)',
                data=('\n'.join(oid for oid, _ in entries) + '\n').encode('ascii')).decode('ascii').splitlines()
    selected = []
    for (oid, name), descriptor in zip(entries, sizes):
        fields = descriptor.split()
        if len(fields) == 3 and fields[1] == 'blob' and int(fields[2]) <= 2 * 1024 * 1024:
            selected.append((oid, name))
        else: skipped['NON_TEXT_OR_SIZE'] += 1
    findings, scanned, undecodable = [], 0, []
    if selected:
        raw = git(root, 'cat-file', '--batch', data=('\n'.join(oid for oid, _ in selected) + '\n').encode('ascii'))
        cursor = 0
        for oid, name in selected:
            end = raw.index(b'\n', cursor)
            size = int(raw[cursor:end].split()[2]); cursor = end + 1
            body = raw[cursor:cursor+size]; cursor += size + 1
            try: text = body.decode('utf-8')
            except UnicodeDecodeError: undecodable.append({'blob': oid, 'path': name}); continue
            scanned += 1
            findings.extend(dict(item, blob=oid) for item in scan_text(name, text))
    return {'scanned_text_blobs': scanned, 'findings': findings, 'excluded_object_counts': dict(skipped),
            'undecodable': undecodable, 'scope': 'reachable allowlisted UTF-8 blobs only; no commit/tag metadata, binary contents, private/state material, unreachable objects or other copies inspected'}


def inspect_workspace(root, include_history=False):
    tracked = set(git(root, 'ls-files', '-z').decode('utf-8').rstrip('\0').split('\0')) - {''}
    untracked = set(git(root, 'ls-files', '--others', '--exclude-standard', '-z').decode('utf-8').rstrip('\0').split('\0')) - {''}
    inventory, findings = [], []
    for name in sorted(tracked | untracked):
        kind = classify_path(name)
        item = {'path': name, 'git_state': 'TRACKED' if name in tracked else 'UNTRACKED', 'classification': kind}
        if kind.startswith('EXCLUDED'):
            # Only metadata; no existence/read/stat under the private/state path.
            item['content_read'] = False
        elif not (root / name).exists():
            item['classification'] = 'MISSING_TRACKED_REVIEW'; item['content_read'] = False
        elif kind == 'CANDIDATE_TEXT':
            try:
                data = read_candidate(root, name)
                text = data.decode('utf-8')
                item.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest(), content_read=True)
                findings.extend(scan_text(name, text))
            except (ValueError, OSError, UnicodeDecodeError):
                item['classification'] = 'UNREADABLE_REVIEW'; item['content_read'] = False
        else:
            item['bytes'] = (root / name).stat().st_size; item['content_read'] = False
        inventory.append(item)
    report = {'status': 'REVIEW_REQUIRED_NOT_AUTHORIZATION',
              'branch': git(root, 'branch', '--show-current').decode().strip(),
              'head': git(root, 'rev-parse', 'HEAD').decode().strip(),
              'reachable_commits': int(git(root, 'rev-list', '--count', '--all')),
              'remote_names': git(root, 'remote').decode().splitlines(),
              'tracked_count': len(tracked), 'untracked_nonignored_count': len(untracked),
              'classification_counts': dict(Counter(x['classification'] for x in inventory)),
              'inventory': inventory, 'findings': findings,
              'finding_counts': dict(Counter(x['severity'] for x in findings)),
              'limits': ['Regex/manual-review cues, not complete credential/privacy clearance',
                         'Excluded, binary, archives and commit/tag metadata were not content-audited',
                         'Ignore rules do not remove already tracked files or history',
                         'No Git mutation, network access, upload or publishing approval']}
    if include_history: report['history'] = history_text_scan(root)
    return report


def public_test_summary(root, qa):
    if not acceptable_test_report(qa): raise ValueError('full passing no-skip report required')
    groups = {'application_files': [], 'test_files': [], 'qa_scripts': []}
    for directory in ('app', 'config', 'tests', 'scripts'):
        for path in sorted((root / directory).rglob('*')):
            name = path.relative_to(root).as_posix()
            if path.is_file() and classify_path(name) == 'CANDIDATE_TEXT':
                group = 'test_files' if directory == 'tests' else 'qa_scripts' if directory == 'scripts' else 'application_files'
                groups[group].append({'path': name, 'sha256': hashlib.sha256(read_candidate(root, name)).hexdigest()})
    return {'schema': 1, 'scope': 'current offline Mock application snapshot; not production, Docker bundle or platform certification',
            'command': 'python scripts/browser_qa.py --full',
            **{k: qa[k] for k in ('status', 'tests_run', 'skipped', 'resource_warnings', 'original_database_unchanged')},
            **groups, 'source_basis': 'working tree, including uncommitted files; HEAD alone is not this snapshot',
            'known_unverified': ['T14 latest image/offline bundle', 'actual human presentation timing', 'target device/platform playback', 'remote CI and publication'],
            'raw_records': 'private local data/verification records; not automatically included in source publication'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--history', action='store_true')
    parser.add_argument('--test-report', type=Path)
    parser.add_argument('--public-summary', type=Path)
    args = parser.parse_args()
    report = inspect_workspace(ROOT, args.history)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    if args.public_summary:
        if not args.test_report: parser.error('--public-summary requires --test-report')
        summary = public_test_summary(ROOT, json.loads(args.test_report.read_text(encoding='utf-8')))
        args.public_summary.parent.mkdir(parents=True, exist_ok=True)
        args.public_summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'status':report['status'],'finding_counts':report['finding_counts'],
                      'classification_counts':report['classification_counts']}, ensure_ascii=True))
    # Exit zero means the inspection completed, never permission to publish.
    return 0


if __name__ == '__main__': raise SystemExit(main())

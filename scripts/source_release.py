#!/usr/bin/env python3
"""Deterministic current-tree source snapshot. Never commits, pushes or packs state.

Source ZIP is for review/development. Offline AI runtime is the separately verified
offline_release bundle; checksums do not imply publisher identity or privacy clearance.
"""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import stat
import zipfile
from release_preflight import classify_path, scan_text, PRIVATE, STATE

ROOT = Path(__file__).resolve().parents[1]
DIRS = ('app', 'config', 'scripts', 'tests', 'eval', '.github', 'docs', 'prompts')
ROOTS = ('README.md', 'AGENTS.md', 'Dockerfile', 'docker-compose.yml', '.gitignore',
         '.dockerignore', '.env.example', 'run.sh', 'requirements.txt',
         'requirements-production.txt', 'requirements-rag-local.txt',
         'requirements-ocr-local.txt', '领域Agent开发手册.md', 'PUSH与发布操作清单.md')
PUBLIC_BINARY = {'docs/assets/P10合成OCR样本.png'}
MAX_FILE = 8 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MANIFEST = 'SOURCE_MANIFEST.json'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def sha(data): return hashlib.sha256(data).hexdigest()


def safe_name(name):
    parts = PurePosixPath(name).parts
    return (bool(parts) and not name.startswith('/') and '\\' not in name and ':' not in name
            and all(p not in ('', '.', '..') for p in name.split('/'))
            and not name.endswith('/') and not any(ord(c) < 32 for c in name))


def allowed(name):
    if not safe_name(name): return False
    lowered = {p.lower() for p in PurePosixPath(name).parts}
    if lowered & (PRIVATE | STATE): return False
    if name in ROOTS or name == '.github/CODEOWNERS' or name in PUBLIC_BINARY: return True
    return classify_path(name) == 'CANDIDATE_TEXT'


def candidates(root):
    # Only enumerate approved roots. Never descend into private/state directories.
    def walk(folder):
        if folder.is_symlink(): raise ValueError('symlink rejected')
        for path in sorted(folder.iterdir()):
            if path.name.lower() in PRIVATE | STATE: continue
            if path.is_symlink(): raise ValueError('symlink rejected')
            if path.is_dir(): yield from walk(path)
            elif allowed(path.relative_to(root).as_posix()): yield path
    for name in ROOTS:
        path = root / name
        if path.is_symlink(): raise ValueError('symlink rejected')
        if path.is_file(): yield path
    for name in DIRS:
        path = root / name
        if path.exists(): yield from walk(path)


def build(root, output):
    root, output = Path(root).resolve(), Path(output)
    if output.exists(): raise ValueError('never overwrite a release')
    payload, findings, total = {}, [], 0
    for path in candidates(root):
        name = path.relative_to(root).as_posix()
        if root not in path.resolve().parents: raise ValueError('source escaped root')
        if path.stat().st_size > MAX_FILE: raise ValueError('source file too large')
        data = path.read_bytes(); total += len(data)
        if total > MAX_TOTAL or len(payload) >= 10000: raise ValueError('source budget exceeded')
        if name not in PUBLIC_BINARY: findings.extend(scan_text(name, data.decode('utf-8-sig')))
        payload[name] = data
    if not payload: raise ValueError('empty source snapshot')
    if any(f['severity'] == 'BLOCK' for f in findings):
        raise ValueError('credential BLOCK detected; no archive written (values redacted)')
    files = {name: {'sha256': sha(data), 'bytes': len(data)} for name, data in sorted(payload.items())}
    manifest = {'schema': 1, 'snapshot_id': sha(canonical(files)), 'files': files,
                'source_basis': 'current working tree, including uncommitted/untracked source; not HEAD',
                'review_findings': findings, 'publication_authorized': False,
                'excludes': ['private/state/databases/secrets', 'Git history', 'models/wheels/runtime images',
                             'competition binary documents/media; only named synthetic OCR PNG included']}
    payload[MANIFEST] = canonical(manifest) + b'\n'
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as stream, zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_STORED) as z:
        for name, data in sorted(payload.items()):
            entry = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0)); entry.create_system = 3
            entry.external_attr = (stat.S_IFREG | (0o755 if name == 'run.sh' else 0o644)) << 16
            z.writestr(entry, data)
    verify(output)
    return manifest


def verify(archive):
    try:
        with zipfile.ZipFile(archive) as z:
            entries = z.infolist(); names = [x.filename for x in entries]
            if len(entries) > 10001 or len(set(n.casefold() for n in names)) != len(names): raise ValueError('duplicate/colliding members')
            if MANIFEST not in names: raise ValueError('missing manifest')
            if sum(x.file_size for x in entries) > MAX_TOTAL + MAX_FILE: raise ValueError('archive too large')
            for entry in entries:
                if (not safe_name(entry.filename) or entry.file_size > MAX_FILE or entry.flag_bits & 1
                        or stat.S_ISLNK(entry.external_attr >> 16)):
                    raise ValueError('unsafe archive member')
            manifest = json.loads(z.read(MANIFEST)); files = manifest.get('files')
            if manifest.get('schema') != 1 or not isinstance(files, dict) or not files: raise ValueError('invalid manifest')
            if set(names) != set(files) | {MANIFEST}: raise ValueError('extra or missing member')
            if sha(canonical(files)) != manifest.get('snapshot_id'): raise ValueError('manifest id mismatch')
            if manifest.get('publication_authorized') is not False: raise ValueError('snapshot is not publishing approval')
            for name, descriptor in files.items():
                if not allowed(name): raise ValueError('member outside source scope')
                data = z.read(name)
                if len(data) != descriptor['bytes'] or sha(data) != descriptor['sha256']: raise ValueError('content hash mismatch')
                if name not in PUBLIC_BINARY and any(f['severity'] == 'BLOCK' for f in scan_text(name, data.decode('utf-8-sig'))):
                    raise ValueError('credential BLOCK in source')
            return manifest
    except (zipfile.BadZipFile, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid source release') from exc


def extract(archive, destination):
    manifest = verify(archive)  # complete verification before any directory creation
    destination = Path(destination)
    if destination.exists() or destination.is_symlink(): raise ValueError('fresh destination required')
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive) as z:
        for name in [*manifest['files'], MANIFEST]:
            path = destination / name
            if destination.resolve() not in path.resolve().parents: raise ValueError('extraction escaped root')
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('xb') as out: out.write(z.read(name))
            if name == 'run.sh': path.chmod(0o755)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('build', 'verify', 'extract'))
    parser.add_argument('archive', type=Path)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    if args.action == 'build': result = build(args.root, args.archive)
    elif args.action == 'verify': result = verify(args.archive)
    else:
        if not args.destination: parser.error('--destination required')
        result = extract(args.archive, args.destination)
    print(json.dumps({'status': 'PASS', 'snapshot_id': result['snapshot_id'], 'files': len(result['files']),
                      'review_findings': len(result['review_findings']), 'publication_authorized': False}))


if __name__ == '__main__': main()

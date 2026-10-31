#!/usr/bin/env python3
"""Explicit online preparation ONLY; inference and delivered bundles never download.

python scripts/prepare_local_rag.py --download
python scripts/prepare_local_rag.py --verify
Optional --windows downloads wheels for this Windows CPython 3.13 as well.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import os
import subprocess
import sys
import urllib.request
import zipfile
from email.parser import Parser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from local_embeddings import verify_model, MODEL_FILES, MODEL_ID

REVISION = '75c43b069aac4d136ba6bc1122f995fedcfd2781'
REPO = 'Xenova/bge-small-zh-v1.5'
MODEL_SHA256 = '15b717c382bcb518ba457b93ea6850ede7f4f1cd8937454aa06972366cd19bcc'
ASSETS = ROOT / '.qa-tools/local-rag'
MODEL = ROOT / 'models/rag'


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def fetch(url, target, *, expected=None, limit=150_000_000):
    if target.exists():
        if expected and sha256(target) != expected: raise ValueError('existing asset digest mismatch')
        return
    print('DOWNLOAD:', target.name, flush=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Only immutable, public model/license files; no project/customer data sent.
    with urllib.request.urlopen(url, timeout=60) as response:
        data = response.read(limit + 1)
    if len(data) > limit: raise ValueError('asset too large')
    if expected and hashlib.sha256(data).hexdigest() != expected: raise ValueError('download digest mismatch')
    target.write_bytes(data)


def download_model():
    if (MODEL / 'manifest.json').exists():
        verify_model(MODEL)
        print('Existing model verified', flush=True)
        return
    names = {'model_quantized.onnx': 'onnx/model_quantized.onnx', 'tokenizer.json': 'tokenizer.json',
             'config.json': 'config.json', 'tokenizer_config.json': 'tokenizer_config.json',
             'MODEL_README.md': 'README.md'}
    for local, remote in names.items():
        fetch(f'https://huggingface.co/{REPO}/resolve/{REVISION}/{remote}', MODEL / local,
              expected=MODEL_SHA256 if local == 'model_quantized.onnx' else None)
    # License from original publisher, pinned to an immutable Git revision.
    with urllib.request.urlopen('https://api.github.com/repos/FlagOpen/FlagEmbedding/commits/master', timeout=30) as response:
        license_revision = json.load(response)['sha']
    if not re.fullmatch('[0-9a-f]{40}', license_revision): raise ValueError('invalid license revision')
    license_url = f'https://raw.githubusercontent.com/FlagOpen/FlagEmbedding/{license_revision}/LICENSE'
    fetch(license_url, MODEL / 'LICENSE.txt', limit=65536)
    manifest = {'schema': 1, 'model': MODEL_ID, 'revision': REVISION, 'dimension': 512, 'max_tokens': 512,
                'source': f'https://huggingface.co/{REPO}/tree/{REVISION}',
                'original_model': 'https://huggingface.co/BAAI/bge-small-zh-v1.5', 'license_source': license_url,
                'files': {name: {'size': (MODEL / name).stat().st_size, 'sha256': sha256(MODEL / name)} for name in MODEL_FILES}}
    (MODEL / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    verify_model(MODEL)


def download_reranker():
    from local_reranker import verify_reranker
    folder=ROOT/'models/reranker'
    if (folder/'manifest.json').exists():
        verify_reranker(folder)
        return
    revision='280bcc27a84e0b898c251e06fddb25171bd9b101'
    files={'model_quantized.onnx':('onnx/model_quantized.onnx','dd98f3e67837d23210a6b7550c08cced4f61845b940ac45be3565840a10f3244'),
           'tokenizer.json':('tokenizer.json','48564c5c7d3fa64d85d95e65414a542385f88b0f128fd8d4163fd7a57f2be05c'),
           'config.json':('config.json',None),'tokenizer_config.json':('tokenizer_config.json',None),
           'MODEL_README.md':('README.md',None)}
    for name,(remote,digest) in files.items():
        fetch(f'https://huggingface.co/Xenova/bge-reranker-base/resolve/{revision}/{remote}',folder/name,expected=digest,limit=400_000_000)
    license_url='https://raw.githubusercontent.com/FlagOpen/FlagEmbedding/fd1a2bdf69488ffebe0327999d4400d8c8058a0b/LICENSE'
    fetch(license_url,folder/'LICENSE.txt',limit=65536)
    manifest={'schema':1,'model':'BAAI/bge-reranker-base','revision':revision,'max_tokens':512,
        'source':f'https://huggingface.co/Xenova/bge-reranker-base/tree/{revision}','license_source':license_url,
        'files':{name:{'size':(folder/name).stat().st_size,'sha256':sha256(folder/name)} for name in [*files,'LICENSE.txt']}}
    (folder/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    verify_reranker(folder)


def wheel_lock(folder,requirements_path=None):
    requirements_path=requirements_path or ROOT/'requirements-rag-local.txt'
    rows, inventory = [], {}
    for path in sorted(folder.glob('*.whl')):
        with zipfile.ZipFile(path) as archive:
            names = [n for n in archive.namelist() if n.endswith('.dist-info/METADATA')]
            if len(names) != 1: raise ValueError('invalid wheel metadata')
            metadata = Parser().parsestr(archive.read(names[0]).decode('utf-8'))
        name, version = metadata['Name'], metadata['Version']
        if not re.fullmatch('[a-zA-Z0-9_.-]+', name) or not re.fullmatch('[a-zA-Z0-9_.+!-]+', version):
            raise ValueError('unsafe wheel metadata')
        digest = sha256(path)
        rows.append(f'{name}=={version} --hash=sha256:{digest}\n')
        inventory[path.name] = {'sha256': digest, 'size': path.stat().st_size}
    if not rows: raise ValueError('no wheels')
    (folder / 'requirements.lock').write_text(''.join(rows), encoding='ascii')
    (folder / 'manifest.json').write_text(json.dumps({'schema': 1, 'files': inventory,
        'requirements_sha256': sha256(requirements_path),
        'lock_sha256': sha256(folder / 'requirements.lock')}, indent=2) + '\n', encoding='utf-8')


def download_wheels(platform,requirements_path=None,assets_root=None,find_links=None):
    requirements_path=requirements_path or ROOT/'requirements-rag-local.txt'
    folder = (assets_root or ASSETS) / ('wheels-' + platform)
    if (folder / 'manifest.json').exists():
        current = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
        if current.get('requirements_sha256') == sha256(requirements_path):
            verify_wheels(folder,requirements_path)
            return
    folder.mkdir(parents=True, exist_ok=True)
    target = ['--platform', 'win_amd64', '--python-version', '313', '--abi', 'cp313'] if platform == 'windows' else [
        '--platform', 'manylinux_2_28_x86_64', '--platform', 'manylinux_2_27_x86_64',
        '--platform', 'manylinux2014_x86_64', '--python-version', '312', '--abi', 'cp312']
    subprocess.run([sys.executable, '-m', 'pip', 'download', '--disable-pip-version-check', '--retries', '0',
                    '--timeout', '60', '--only-binary=:all:', '--implementation', 'cp', *target,
                    '--dest', str(folder), '-r', str(requirements_path),
                    *(['--find-links',str(find_links)] if find_links else [])], check=True)
    wheel_lock(folder,requirements_path)
    verify_wheels(folder,requirements_path)


def verify_wheels(folder,requirements_path=None):
    requirements_path=requirements_path or ROOT/'requirements-rag-local.txt'
    manifest = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    if (manifest.get('schema') != 1 or not manifest.get('files') or
        manifest.get('requirements_sha256') != sha256(requirements_path)):
        raise ValueError('invalid or stale wheel manifest')
    for name, entry in manifest['files'].items():
        if not re.fullmatch('[a-zA-Z0-9_.+-]+[.]whl', name): raise ValueError('unsafe wheel filename')
        path = folder / name
        if path.is_symlink() or path.stat().st_size != entry['size'] or sha256(path) != entry['sha256']:
            raise ValueError('wheel integrity failed')
    if (folder / 'requirements.lock').is_symlink() or sha256(folder / 'requirements.lock') != manifest['lock_sha256']:
        raise ValueError('wheel lock mismatch')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--download', action='store_true')
    mode.add_argument('--verify', action='store_true')
    parser.add_argument('--windows', action='store_true')
    parser.add_argument('--reranker',action='store_true',help='Prepare/verify independent local BGE cross-encoder')
    parser.add_argument('--install-windows', action='store_true', help='Offline install to project-local windows-site, not the global Python environment')
    args = parser.parse_args()
    if args.download:
        download_model()
        if args.reranker: download_reranker()
        download_wheels('linux')
        if args.windows: download_wheels('windows')
    verify_model(MODEL)
    if args.reranker:
        from local_reranker import verify_reranker
        verify_reranker(ROOT/'models/reranker')
    verify_wheels(ASSETS / 'wheels-linux')
    if args.windows: verify_wheels(ASSETS / 'wheels-windows')
    if args.install_windows:
        if os.name != 'nt' or sys.version_info[:2] != (3,13):
            raise ValueError('prepared native wheels require Windows x64 CPython 3.13; use the Docker bundle otherwise')
        folder = ASSETS / 'wheels-windows'
        verify_wheels(folder)
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--disable-pip-version-check', '--no-index',
                        '--no-cache-dir', '--target', str(ASSETS / 'windows-site'), '--find-links', str(folder),
                        '--require-hashes', '-r', str(folder / 'requirements.lock')], check=True)
    print('Local RAG assets verified; no inference/customer data was sent', flush=True)


if __name__ == '__main__': main()

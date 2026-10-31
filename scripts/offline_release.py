#!/usr/bin/env python3
"""Build an allowlisted Linux/amd64 offline bundle; verify without Docker/network."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_NAMES = ('images.tar', 'compose.json', 'START_HERE.md', 'verify.ps1', 'verify.sh', 'offline_release.py')
SOURCE_DIRS = ('app', 'config', 'scripts', 'tests', 'eval')
SOURCE_SUFFIXES = {'.py', '.json', '.html', '.css', '.js', '.svg', '.webmanifest', '.md'}
SOURCE_ROOTS = ('Dockerfile', 'requirements.txt', 'requirements-production.txt', 'requirements-rag-local.txt', 'requirements-ocr-local.txt', 'run.sh', 'AGENTS.md', 'README.md')

# Docker 29 can omit published ports on internal-only bridges. This fixed
# upstream relay exposes localhost while the Agent stays internal-only.
# Same image, stdlib only, no arbitrary destination / CONNECT / redirects.
LOCAL_WEB_PROXY = r'''
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import http.client
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def relay(self):
  try:
   if not self.path.startswith('/') or self.path.startswith('//'): raise ValueError('invalid path')
   if self.headers.get('Transfer-Encoding'): raise ValueError('unsupported transfer encoding')
   length=int(self.headers.get('Content-Length','0'))
   if not 0<=length<=2097152: raise ValueError('request too large')
   body=self.rfile.read(length) if length else None
   headers={k:v for k,v in self.headers.items() if k.lower() not in {'host','connection','content-length','transfer-encoding','upgrade','proxy-authorization','proxy-connection'}}
   upstream=http.client.HTTPConnection('banking-agent',8090,timeout=30)
   try:
    upstream.request(self.command,self.path,body=body,headers=headers)
    result=upstream.getresponse();data=result.read(8388609)
    if len(data)>8388608: raise ValueError('response too large')
    self.send_response(result.status)
    for k,v in result.getheaders():
     if k.lower() not in {'connection','content-length','transfer-encoding','server','date','upgrade'}: self.send_header(k,v)
    self.send_header('Content-Length',str(len(data)));self.end_headers()
    if self.command!='HEAD': self.wfile.write(data)
   finally: upstream.close()
  except (ValueError,OSError,http.client.HTTPException): self.send_error(502,'Local demo upstream unavailable')
 do_GET=relay
 do_POST=relay
 do_HEAD=relay
ThreadingHTTPServer(('0.0.0.0',8090),Handler).serve_forever()
'''


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def stage_source(root, stage, *, local_rag=False, local_reranker=False, local_ocr=False):
    """Never enumerate process material, old archives, local state or credentials."""
    if local_reranker and not local_rag: raise ValueError('reranker requires local RAG')
    paths = []
    for name in SOURCE_ROOTS:
        if (root / name).is_file(): paths.append(root / name)
    for name in SOURCE_DIRS:
        base = root / name
        if base.is_symlink(): raise ValueError('symlink source directory')
        if not base.exists(): continue
        # walk without following directory symlinks; reject rather than package them.
        import os
        for directory, dirs, files in os.walk(base, followlinks=False):
            for item in dirs:
                if (Path(directory) / item).is_symlink(): raise ValueError('symlink source directory')
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
            for item in files:
                p = Path(directory) / item
                if p.suffix in SOURCE_SUFFIXES and not item.startswith('.'):
                    paths.append(p)
    entries = []
    for source in sorted(paths):
        if source.is_symlink(): raise ValueError('symlink source file')
        relative = source.relative_to(root).as_posix()
        target = stage / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        entries.append({'path': relative, 'sha256': sha256(target)})
    if local_rag:
        import sys
        sys.path.insert(0, str(ROOT / 'app'))
        from local_embeddings import verify_model, MODEL_FILES
        verify_model(root / 'models/rag')
        assets = [(root / 'models/rag' / name, 'models/rag/' + name) for name in ('manifest.json', *MODEL_FILES)]
        wheels = root / '.qa-tools/local-rag/wheels-linux'
        if wheels.is_symlink(): raise ValueError('linked wheel folder')
        manifest = json.loads((wheels / 'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('schema') != 1 or not manifest.get('files'): raise ValueError('invalid wheel manifest')
        for name, item in manifest['files'].items():
            if not re.fullmatch('[a-zA-Z0-9_.+-]+[.]whl', name): raise ValueError('unsafe wheel filename')
            path = wheels / name
            if path.is_symlink() or path.stat().st_size != item['size'] or sha256(path) != item['sha256']:
                raise ValueError('wheel digest mismatch')
            assets.append((path, 'rag-wheels/' + name))
        if sha256(wheels / 'requirements.lock') != manifest['lock_sha256']: raise ValueError('wheel lock mismatch')
        assets.extend((wheels / name, 'rag-wheels/' + name) for name in ('requirements.lock', 'manifest.json'))
        for source, relative in assets:
            if source.is_symlink(): raise ValueError('linked RAG asset')
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            entries.append({'path': relative, 'sha256': sha256(target)})
    extra_assets=[]
    if local_reranker:
        from local_reranker import verify_reranker
        verify_reranker(root / 'models/reranker')
        extra_assets.extend((root/'models/reranker'/n,'models/reranker/'+n) for n in ('manifest.json',*MODEL_FILES))
    if local_ocr:
        import sys
        sys.path.insert(0,str(ROOT/'app'))
        from ocr_service import verify_ocr_assets
        verified=verify_ocr_assets(root/'models/ocr')
        extra_assets.extend((root/'models/ocr'/n,'models/ocr/'+n) for n in ('manifest.json',*verified['files']))
        wheels=root/'.qa-tools/local-ocr/wheels-linux'
        manifest=json.loads((wheels/'manifest.json').read_text(encoding='utf-8'))
        if wheels.is_symlink() or manifest.get('schema')!=1 or not manifest.get('files'): raise ValueError('invalid OCR wheel manifest')
        for name,item in manifest['files'].items():
            if not re.fullmatch('[a-zA-Z0-9_.+-]+[.]whl',name): raise ValueError('unsafe OCR wheel name')
            path=wheels/name
            if path.is_symlink() or path.stat().st_size!=item['size'] or sha256(path)!=item['sha256']: raise ValueError('OCR wheel digest mismatch')
            extra_assets.append((path,'ocr-wheels/'+name))
        if sha256(wheels/'requirements.lock')!=manifest['lock_sha256']: raise ValueError('OCR wheel lock mismatch')
        extra_assets.extend((wheels/n,'ocr-wheels/'+n) for n in ('manifest.json','requirements.lock'))
    for source,relative in extra_assets:
        if source.is_symlink(): raise ValueError('linked optional asset')
        target=stage/relative
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(source,target)
        entries.append({'path':relative,'sha256':sha256(target)})
    return entries


def compose_config(image, *, local_rag=False, local_reranker=False, local_ocr=False):
    if local_reranker and not local_rag: raise ValueError('reranker requires local RAG')
    config = {'services': {'banking-agent': {
        'image': image, 'pull_policy': 'never',
        'ports': [{'target': 8090, 'published': '8090', 'host_ip': '127.0.0.1', 'protocol': 'tcp'}],
        'volumes': ['demo-data:/app/data'],
        'environment': {'BANK_AGENT_HOST': '0.0.0.0', 'BANK_AGENT_PORT': '8090',
                        'BANK_AGENT_OFFLINE': '1', 'VOICE_ENABLED': '0', 'APP_ENV': 'demo',
                        'ALLOW_DEMO_CORE': 'true', 'TASK_STORE_BACKEND': 'sqlite-demo',
                        'AUDIT_BACKEND': 'sqlite-hash-chain-demo'},
        'read_only': True, 'tmpfs': ['/tmp'], 'cap_drop': ['ALL'],
        'security_opt': ['no-new-privileges:true']}},
        'volumes': {'demo-data': {}}, 'networks': {'default': {'internal': True}}}
    if local_rag:
        app=config['services']['banking-agent']
        app['environment'].update(
            RAG_EMBEDDING_BACKEND='local', RAG_LOCAL_MODEL_DIR='/app/models/rag', RAG_USE_MODEL='0')
        ports=app.pop('ports')
        app['networks']=['default']
        config['services']['web']={'image':image,'pull_policy':'never','command':['python','-c',LOCAL_WEB_PROXY],
            'ports':ports,'networks':['default','web'],'depends_on':{'banking-agent':{'condition':'service_healthy'}},
            'read_only':True,'tmpfs':['/tmp'],'cap_drop':['ALL'],'security_opt':['no-new-privileges:true']}
        config['networks']['web']={}
    app=config['services']['banking-agent']
    app['environment'].update(RAG_USE_RERANKER='1' if local_reranker else '0',
        RAG_RERANKER_DIR='/app/models/reranker',OCR_ENABLED='1' if local_ocr else '0',OCR_MODEL_DIR='/app/models/ocr')
    return config


def verify_bundle(folder):
    manifest_path = folder / 'manifest.json'
    if manifest_path.is_symlink(): raise ValueError('symlink manifest')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('schema') != 1 or manifest.get('platform') != 'linux/amd64':
        raise ValueError('unsupported bundle schema/platform')
    if set(manifest.get('files', {})) != set(PAYLOAD_NAMES):
        raise ValueError('unexpected or incomplete bundle inventory')
    images = manifest.get('images', {})
    if set(images) != {'app', 'sandbox'}: raise ValueError('both images required')
    if images['sandbox'].get('tag') != 'python:3.12-slim': raise ValueError('sandbox tag mismatch')
    for image in images.values():
        if not re.fullmatch(r'sha256:[0-9a-f]{64}', image.get('id', '')): raise ValueError('invalid image ID')
    if not re.fullmatch(r'bank-agent-offline:[a-z0-9-]+', images['app'].get('tag', '')):
        raise ValueError('invalid application tag')
    for name, expected in manifest['files'].items():
        path = folder / name
        if path.is_symlink() or not path.is_file() or sha256(path) != expected:
            raise ValueError('bundle integrity failed: ' + name)
    return manifest


def docker(*args, timeout=120):
    print('DOCKER:', args[0], flush=True)
    result = subprocess.run(['docker', *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace')[-3000:])
    return result.stdout.decode('utf-8').strip()


def inspect_image(tag):
    return json.loads(docker('image', 'inspect', '--format', '{{json .}}', tag))


START_HERE = '''# Banking Agent offline demo (Linux/amd64)

Prerequisites: installed/running Docker Linux engine with Compose v2, amd64 CPU
or separately verified emulation. Docker itself is NOT bundled. No host Python
is required for the web demo. Windows/macOS require Docker's Linux VM.
This folder is a complete image distribution; copy ALL its files together.

1. Verify checksums: PowerShell 7 (if installed and local script policy permits):
   `pwsh -File ./verify.ps1`. Windows PowerShell with scripts disabled can use
   `Get-FileHash -Algorithm SHA256 -LiteralPath ./images.tar` and repeat for EVERY
   file named in SHA256SUMS, comparing each value with its entry. Do not change
   machine execution policy for this bundle. Linux/macOS: `sh ./verify.sh`.
   Do not load if any checksum is missing or differs.
   Checksums detect corruption, NOT a trusted publisher signature. Obtain the
   bundle/checksum through a trusted channel. SHA256SUMS also covers manifest.json.
2. Check `docker version` reports linux/amd64 and `docker compose version` works.
   `docker load --input images.tar` imports the app AND sandbox base; no registry.
   Loading python:3.12-slim sets that local tag to this bundle's version. If your
   existing tag differs, preserve it under another tag first, or use a dedicated engine.
3. `docker compose -p bank-agent-offline -f compose.json up -d --no-build --pull never`
4. Open http://127.0.0.1:8090 ; demo-only, no real bank. Fixed OTP: 123456.
5. `docker compose -p bank-agent-offline -f compose.json down` stops the demo;
   keep the named volume. Never use `down -v` unless intentionally deleting YOUR demo data.

No build or image pull occurs during these steps. The internal application network
blocks external routing; BANK_AGENT_OFFLINE=1 disables external LLM/ASR. Public bind
is loopback only. The named data volume survives container recreation. A new folder
does NOT reset a volume with the same project name. Select a new project name for
an independent demo; use the same name to retain data. Port 8090 must be free.

Application image contains current app/config/tests/scripts and bundled Python.
There is no production infra profile, real IM, real IAM/MFA or real operator service.
Local handoff records are not bank settlement. A local semantic RAG bundle is marked
features.local_rag=true in manifest.json. It includes the embedding model, tokenizer,
CPU runtime and knowledge sources. New questions and a fresh in-memory index work
without any key, registry, pip download or external model service. The local model
works with BANK_AGENT_OFFLINE=1. Plain bundles without this feature use lexical RAG.
Features.local_reranker adds the local BGE cross-encoder; overload explicitly falls
back to fusion retrieval. Features.local_ocr includes RapidOCR CPU models. Images
stay local/in memory; select a PNG/JPEG up to 2 MiB, consent, inspect fields and
confirm review before filling a draft. OCR NEVER submits a transfer automatically.
The normal platform permission/confirmation/authentication chain still applies.
OCR family-account import is currently unsupported. Human RAG evaluation remains
pending; synthetic regression/load tests are not a production capacity guarantee.
For local RAG bundles, Compose runs the same image twice: the Agent is on an
internal-only network, without published ports; a stdlib web relay publishes
localhost and forwards ONLY to banking-agent:8090. This works around Docker 29's
internal-only port publishing behavior. The relay has a frontend bridge but no
model/key/data volume or arbitrary destination/CONNECT support. Agent egress is
still blocked by its internal network; no Docker global settings are changed.
This is source-grounded quotation, not offline chat-LLM generation or bank access.
Sandbox is a separate
host-Docker demonstration; never mount Docker socket into the application container.
Optional terminal channel: `docker run --rm -i --pull never --network none APP_IMAGE
python scripts/channel_demo.py` (replace APP_IMAGE from manifest.json).
This uses temporary Mock data, not the web volume.

The maintainer QA uses host Python and scripts/offline_qa.py; it proves restricted
network execution on the tested engine, not a physical second computer or a
disconnected host. No native screen-reader/ASR/production certification is implied.
See manifest.json source hashes and the repository T14 report for exact evidence.
'''

VERIFY_PS = '''$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$seenFiles = @()
Get-Content -LiteralPath './SHA256SUMS' | ForEach-Object {
    if ($_ -notmatch '^([0-9a-f]{64})  ([A-Za-z0-9_.-]+)$') { throw 'Invalid checksum entry' }
    $expectedDigest = $Matches[1]
    $bundleFile = $Matches[2]
    if ($seenFiles -contains $bundleFile) { throw 'Duplicate checksum entry' }
    $seenFiles += $bundleFile
    $entry = Get-Item -LiteralPath $bundleFile
    if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Link payload rejected' }
    if ((Get-FileHash -Algorithm SHA256 -LiteralPath $bundleFile).Hash.ToLowerInvariant() -ne $expectedDigest) { throw "Checksum failed: $bundleFile" }
}
$requiredFiles = @('images.tar', 'compose.json', 'START_HERE.md', 'verify.ps1', 'verify.sh', 'offline_release.py', 'manifest.json')
if (Compare-Object $requiredFiles $seenFiles) { throw 'Incomplete checksum inventory' }
Write-Output 'Bundle checksums PASS (not a publisher signature)'
'''
VERIFY_SH = '''#!/bin/sh
set -eu
cd "$(dirname "$0")"
if command -v sha256sum >/dev/null 2>&1; then sha256sum -c SHA256SUMS
else shasum -a 256 -c SHA256SUMS; fi
'''


def build_bundle(output, *, local_rag=False, local_reranker=False, local_ocr=False):
    if output.exists(): raise ValueError('output already exists; never overwrite a release')
    engine = json.loads(docker('version', '--format', '{{json .Server}}'))
    if engine['Os'] != 'linux' or engine['Arch'] != 'amd64': raise ValueError('linux/amd64 engine required')
    base = inspect_image('python:3.12-slim')  # cached only, never pull
    if (base['Os'], base['Architecture']) != ('linux', 'amd64'): raise ValueError('base platform mismatch')
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S-%f')
    image = 'bank-agent-offline:t14-' + stamp
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='t14-build-', dir=output.parent) as tmp:
        stage = Path(tmp)
        entries = stage_source(ROOT, stage, local_rag=local_rag,local_reranker=local_reranker,local_ocr=local_ocr)
        docker('build', '--pull=false', '--network', 'none', '--build-arg',
               'INSTALL_LOCAL_RAG=' + ('true' if local_rag else 'false'), '--build-arg',
               'INSTALL_LOCAL_OCR=' + ('true' if local_ocr else 'false'), '--tag', image, str(stage), timeout=600)
    app = inspect_image(image)
    output.mkdir()
    # Failure leaves an incomplete folder for diagnosis; no success manifest is issued.
    docker('image', 'save', '--output', str(output / 'images.tar'), image, 'python:3.12-slim', timeout=600)
    (output / 'compose.json').write_text(json.dumps(compose_config(image, local_rag=local_rag,local_reranker=local_reranker,local_ocr=local_ocr), indent=2) + '\n', encoding='utf-8')
    (output / 'START_HERE.md').write_text(START_HERE, encoding='utf-8')
    (output / 'verify.ps1').write_text(VERIFY_PS, encoding='utf-8')
    (output / 'verify.sh').write_text(VERIFY_SH, encoding='utf-8', newline='\n')
    shutil.copyfile(Path(__file__), output / 'offline_release.py')
    manifest = {'schema': 1, 'platform': 'linux/amd64', 'created_at': stamp,
                'scope': 'offline Mock competition demo; not production or universal platform support',
                'images': {'app': {'tag': image, 'id': app['Id']},
                           'sandbox': {'tag': 'python:3.12-slim', 'id': base['Id']}},
                'features': {'local_rag': local_rag,'local_reranker':local_reranker,'local_ocr':local_ocr},
                'source_files': entries, 'files': {n: sha256(output / n) for n in PAYLOAD_NAMES}}
    (output / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    names = [*PAYLOAD_NAMES, 'manifest.json']
    (output / 'SHA256SUMS').write_text(''.join(sha256(output / n) + '  ' + n + '\n' for n in names), encoding='ascii')
    verify_bundle(output)
    print('BUNDLE PASS:', str(output), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['build', 'verify'])
    parser.add_argument('folder', type=Path)
    parser.add_argument('--local-rag', action='store_true', help='Require and embed verified offline model + CPU wheels')
    parser.add_argument('--local-reranker',action='store_true')
    parser.add_argument('--local-ocr',action='store_true')
    args = parser.parse_args()
    if args.action == 'build': build_bundle(args.folder.resolve(), local_rag=args.local_rag,local_reranker=args.local_reranker,local_ocr=args.local_ocr)
    else:
        verify_bundle(args.folder.resolve())
        print('Bundle integrity PASS (not a publisher signature)')


if __name__ == '__main__': main()

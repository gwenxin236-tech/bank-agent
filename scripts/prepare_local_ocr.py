#!/usr/bin/env python3
"""Explicit public OCR asset preparation; no image or customer data outbound."""
import argparse,json,os,subprocess,sys,zipfile
from pathlib import Path,PurePosixPath
from prepare_local_rag import ROOT,fetch,sha256,download_wheels,verify_wheels
ASSETS=ROOT/'.qa-tools/local-ocr'
REQUIREMENTS=ROOT/'requirements-ocr-local.txt'
MODEL=ROOT/'models/ocr'
WHEEL_URL='https://files.pythonhosted.org/packages/59/07/203065283bba0c5e9c10af3602dc25ab3e59ee54dc2d1d22c8faeabf5ed8/rapidocr-3.10.0-py3-none-any.whl'
WHEEL_SHA='2fc34e26cd0f48514804a94f8832283039c7334076bff60768b1fed9ad068050'

def download_assets():
    wheel=ASSETS/'upstream/rapidocr-3.10.0-py3-none-any.whl'
    fetch(WHEEL_URL,wheel,expected=WHEEL_SHA)
    if not (MODEL/'manifest.json').exists():
        MODEL.mkdir(parents=True,exist_ok=True);files={}
        with zipfile.ZipFile(wheel) as archive:
            selected=[n for n in archive.namelist() if n.startswith('rapidocr/') and
                      (n.endswith(('.onnx','.txt','.yaml','.yml')) or 'LICENSE' in n)]
            for name in selected:
                parts=PurePosixPath(name).parts
                if '..' in parts or name.startswith('/'):raise ValueError('unsafe archive member')
                leaf=Path(name).name
                if leaf in files:raise ValueError('duplicate OCR asset name')
                data=archive.read(name)
                if len(data)>100_000_000:raise ValueError('oversized OCR asset')
                target=MODEL/leaf;target.write_bytes(data)
                files[leaf]={'size':len(data),'sha256':sha256(target),'upstream_path':name}
        manifest={'schema':1,'engine':'rapidocr','version':'3.10.0','wheel_sha256':WHEEL_SHA,'files':files}
        (MODEL/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
        print('OCR assets:',list(files),flush=True)
    # OmegaConf's pure Python parser ships only an sdist at this pinned version.
    common=ASSETS/'wheels-common';common.mkdir(parents=True,exist_ok=True)
    if not list(common.glob('antlr4_python3_runtime-4.9.3-*.whl')):
        subprocess.run([sys.executable,'-m','pip','wheel','--no-cache-dir','--no-deps','--no-build-isolation',
                        '--wheel-dir',str(common),'antlr4-python3-runtime==4.9.3'],check=True)

def verify_assets():
    data=json.loads((MODEL/'manifest.json').read_text(encoding='utf-8'))
    if data.get('schema')!=1 or data.get('wheel_sha256')!=WHEEL_SHA:raise ValueError('invalid OCR manifest')
    for name,row in data['files'].items():
        if Path(name).name!=name or '/' in name or '\\' in name:raise ValueError('unsafe asset path')
        p=MODEL/name
        if p.is_symlink() or p.stat().st_size!=row['size'] or sha256(p)!=row['sha256']:raise ValueError('OCR asset integrity failed')
    if len([n for n in data['files'] if n.endswith('.onnx')])<3:raise ValueError('OCR pipeline models missing')
    return data

def main():
    p=argparse.ArgumentParser(description=__doc__);g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--download',action='store_true');g.add_argument('--verify',action='store_true')
    p.add_argument('--windows',action='store_true');p.add_argument('--install-windows',action='store_true');a=p.parse_args()
    if a.download:
        download_assets();download_wheels('linux',REQUIREMENTS,ASSETS,ASSETS/'wheels-common')
        if a.windows:download_wheels('windows',REQUIREMENTS,ASSETS,ASSETS/'wheels-common')
    manifest=verify_assets();verify_wheels(ASSETS/'wheels-linux',REQUIREMENTS)
    if a.windows:verify_wheels(ASSETS/'wheels-windows',REQUIREMENTS)
    if a.install_windows:
        if os.name!='nt' or sys.version_info[:2]!=(3,13):raise ValueError('native wheels require CPython3.13 Windows x64')
        folder=ASSETS/'wheels-windows';verify_wheels(folder,REQUIREMENTS)
        subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--no-index','--no-cache-dir',
            '--target',str(ASSETS/'windows-site'),'--find-links',str(folder),'--require-hashes','-r',str(folder/'requirements.lock')],check=True)
    print('OCR assets verified:',len(manifest['files']),flush=True)

if __name__=='__main__':main()

#!/usr/bin/env python3
"""Build the demo and verify isolated Docker startup and durable Mock transfers.

Requires an already running Linux Docker engine and Docker Compose. Never mounts
the existing data directory. Cleans up only this run's containers and network.
"""
from __future__ import annotations

import copy
import argparse
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import build_opener, ProxyHandler, Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def import_official_base(temporary, run, report):
    """Optional host download when the engine's registry connection is broken.

    Uses the same official source as FROM, with SHA-256 checks on manifests,
    config, compressed blobs and uncompressed rootfs layers. No stored secrets.
    """
    if (ROOT / 'Dockerfile').read_text(encoding='utf-8').splitlines()[0] != 'FROM python:3.12-slim':
        raise RuntimeError('--import-base supports only the current official python:3.12-slim FROM')
    metadata = {'source': 'https://registry-1.docker.io/v2/library/python',
                'tag': '3.12-slim', 'platform': 'linux/amd64', 'verified_layers': []}
    report['base_image_import'] = metadata
    token = None

    def fetch(path, expected=None):
        nonlocal token
        for attempt in range(3):
            try:
                if token is None:
                    with urlopen('https://auth.docker.io/token?service=registry.docker.io&scope=repository:library/python:pull', timeout=25) as response:
                        token = json.load(response)['token']
                request = Request(metadata['source'] + path, headers={'Authorization': 'Bearer ' + token,
                    'Accept': 'application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json'})
                with urlopen(request, timeout=60) as response:
                    content = response.read()
                digest = 'sha256:' + hashlib.sha256(content).hexdigest()
                if expected is not None and digest != expected:
                    raise ValueError('official image content digest mismatch')
                return content, digest
            except Exception:
                if attempt == 2:
                    raise
                time.sleep(2)

    index, metadata['index_digest'] = fetch('/manifests/3.12-slim')
    descriptor = next(item for item in json.loads(index)['manifests']
                      if item['platform'].get('os') == 'linux' and item['platform'].get('architecture') == 'amd64')
    metadata['manifest_digest'] = descriptor['digest']
    manifest = json.loads(fetch('/manifests/' + descriptor['digest'], descriptor['digest'])[0])
    config_raw = fetch('/blobs/' + manifest['config']['digest'], manifest['config']['digest'])[0]
    config = json.loads(config_raw)
    if config.get('os') != 'linux' or config.get('architecture') != 'amd64':
        raise ValueError('official base platform mismatch')
    archive = temporary / 'official-base.tar'
    with tarfile.open(archive, 'w') as output:
        def add(name, data):
            entry = tarfile.TarInfo(name)
            entry.size = len(data)
            output.addfile(entry, io.BytesIO(data))

        config_name = manifest['config']['digest'].split(':')[1] + '.json'
        metadata['config_digest'] = manifest['config']['digest']
        add(config_name, config_raw)
        layers = []
        for number, (layer, diff_id) in enumerate(zip(manifest['layers'], config['rootfs']['diff_ids'], strict=True), 1):
            print(f'STEP: official-base-layer-{number}', flush=True)
            blob = fetch('/blobs/' + layer['digest'], layer['digest'])[0]
            content = gzip.decompress(blob) if 'gzip' in layer['mediaType'] else blob
            if 'sha256:' + hashlib.sha256(content).hexdigest() != diff_id:
                raise ValueError('official base rootfs digest mismatch')
            name = layer['digest'].split(':')[1] + '/layer.tar'
            add(name, content)
            layers.append(name)
            metadata['verified_layers'].append({'digest': layer['digest'], 'diff_id': diff_id})
        add('manifest.json', json.dumps([{'Config': config_name, 'RepoTags': ['python:3.12-slim'], 'Layers': layers}]).encode())
    run('load-official-base', ['load', '--input', str(archive)], timeout=180)
    metadata['status'] = 'PASS'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--import-base', action='store_true',
                        help='Download and verify the same official Linux/amd64 base via the host, then docker load')
    args = parser.parse_args()
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S-%f')
    records = ROOT / 'data/verification' / f't04-{stamp}'
    records.mkdir(parents=True)
    report = {'status': 'FAIL', 'scope': 'isolated Mock Docker QA, not production acceptance',
              'checks': [], 'commands': []}
    original = ROOT / 'data/banking_agent.sqlite3'
    fingerprint = lambda: hashlib.sha256(original.read_bytes()).hexdigest() if original.exists() else None
    before = fingerprint()
    docker = shutil.which('docker')
    compose = None
    opener = build_opener(ProxyHandler({}))

    def run(name, arguments, timeout=120, check=True):
        print(f'STEP: {name}', flush=True)
        command = [docker] + arguments
        result = subprocess.run(command, cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout)
        stdout, stderr = (s.decode('utf-8', errors='replace') for s in (result.stdout, result.stderr))
        report['commands'].append({'name': name, 'command': command, 'returncode': result.returncode,
                                   'stdout': stdout, 'stderr': stderr})
        if check and result.returncode:
            raise RuntimeError(f'{name} exited {result.returncode}: {stderr[-2500:]}')
        return stdout.strip()

    def check(name, condition):
        report['checks'].append({'name': name, 'passed': bool(condition)})
        if not condition:
            raise RuntimeError(name)

    def endpoint():
        container = run('container-id', compose + ['ps', '-q', 'banking-agent'])
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            raw = run('container-health', ['inspect', '--format', '{{json .State}}', container])
            state = json.loads(raw)
            if state.get('Health', {}).get('Status') == 'healthy':
                port = run('container-port', compose + ['port', 'banking-agent', '8090'])
                check('loopback_port_only', port.startswith('127.0.0.1:'))
                return 'http://' + port, container
            if not state.get('Running') or state.get('Health', {}).get('Status') == 'unhealthy':
                raise RuntimeError('container failed its healthcheck')
            time.sleep(1)
        raise RuntimeError('container healthcheck timeout')

    def api(base, path, body=None, token='demo-user-session'):
        request = Request(base + path, headers={'Authorization': 'Bearer '+token,
                          'Content-Type': 'application/json'},
                          data=None if body is None else json.dumps(body).encode('utf-8'))
        with opener.open(request, timeout=10) as response:
            return json.load(response)

    try:
        if docker is None:
            raise RuntimeError('Docker CLI is not installed')
        engine = json.loads(run('docker-engine', ['version', '--format', '{{json .Server}}']))
        check('linux_engine', engine['Os'] == 'linux')
        report['engine_version'] = engine['Version']
        report['compose_version'] = run('compose-version', ['compose', 'version', '--short'])
        source = json.loads(run('source-compose-config', ['compose', 'config', '--format', 'json']))
        tools = ROOT / '.qa-tools/docker-runs'
        tools.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='t04-', dir=tools) as temporary:
            temp = Path(temporary).resolve()
            if tools.resolve() not in temp.parents:
                raise RuntimeError('temporary cleanup path escaped QA workspace')
            data = temp / 'data'
            data.mkdir()
            if args.import_base:
                import_official_base(temp, run, report)
            service = copy.deepcopy(source['services']['banking-agent'])
            image = 'bank-agent-qa:t04-' + stamp
            service.update(image=image, ports=[{'target': 8090, 'published': '0',
                           'host_ip': '127.0.0.1', 'protocol': 'tcp'}],
                           volumes=[{'type': 'bind', 'source': str(data), 'target': '/app/data'}])
            service['environment']['BANK_AGENT_OFFLINE'] = '1'
            config = temp / 'compose.json'
            config.write_text(json.dumps({'services': {'banking-agent': service}}, ensure_ascii=False), encoding='utf-8')
            compose = ['compose', '--progress', 'plain', '-p', 'bankagent-t04-' + stamp,
                       '-f', str(config)]
            try:
                run('qa-compose-config', compose + ['config', '--quiet'])
                run('build', compose + ['build', 'banking-agent'], timeout=600)
                report['image'] = image
                report['image_id'] = run('image-id', ['image', 'inspect', '--format', '{{.Id}}', image])
                audit = '''import importlib.util, json, os
from pathlib import Path
assert os.geteuid() != 0
for name in ('.git', '.qa-tools', '_internal', 'submission'):
    assert not (Path('/app') / name).exists(), name
assert not list(Path('/app/data').iterdir())
for name in ('redis', 'psycopg', 'playwright'):
    assert importlib.util.find_spec(name) is None, name
print(json.dumps({'non_root': True, 'empty_image_data': True, 'stdlib_demo': True}))
'''
                report['image_checks'] = json.loads(run('image-boundaries',
                    ['run', '--rm', '--entrypoint', 'python', image, '-c', audit]))
                run('start', compose + ['up', '-d', '--no-build'])
                base, first_id = endpoint()
                check('live', api(base, '/health/live')['status'] == 'LIVE')
                check('ready', api(base, '/health/ready')['ok'])
                check('preflight', api(base, '/api/preflight')['preflight']['status'] == 'READY_FOR_CONFIGURED_RUNTIME')
                check('production_remains_blocked', api(base, '/api/readiness')['readiness']['status'] == 'NOT_READY_FOR_PRODUCTION')
                with opener.open(base + '/', timeout=10) as response:
                    check('frontend', response.status == 200 and b'/static/app.js' in response.read())
                balance = api(base, '/api/state')['balance']['available_cents']
                knowledge=api(base,'/api/chat',{'message':'知识问答：什么是冷静期'})
                knowledge_data=knowledge['result']['results'][0]['data']
                check('rag_offline_cited',knowledge['plan']['intent']=='knowledge_query' and
                      knowledge_data['status']=='ANSWERED' and knowledge_data['mode']=='local_extractive' and
                      bool(knowledge_data['citations']) and not knowledge['plan_view']['requires_confirmation'])
                product=api(base,'/api/chat',{'message':'知识问答：中国银行整存整取'})
                check('rag_official_source',any(c.get('publisher')=='中国银行' and c.get('source_url','').startswith('https://www.boc.cn/')
                      for c in product['result']['results'][0]['data']['citations']))
                missing=api(base,'/api/chat',{'message':'知识问答：火星航班燃油费'})
                check('rag_no_evidence',missing['result']['results'][0]['data']['status']=='NO_EVIDENCE')
                family_knowledge=api(base,'/api/v1/family/chat',{'message':'知识问答：什么是冷静期',
                    'subject_id':'family-father-demo'},token='demo-daughter-session')
                check('rag_family_read',family_knowledge['result']['results'][0]['data']['status']=='ANSWERED')
                check('rag_no_debit',api(base,'/api/state')['balance']['available_cents']==balance)
                subscription_plan = api(base, '/api/chat', {'message': '查询自动续费订阅'})
                check('subscription_yellow_confirmation',
                      subscription_plan['plan']['intent'] == 'subscription_query'
                      and subscription_plan['plan_view']['risk_level'] == 'yellow'
                      and subscription_plan['plan_view']['requires_confirmation']
                      and not subscription_plan['plan_view']['requires_strong_auth']
                      and 'result' not in subscription_plan)
                subscription_confirmation = {'plan_id': subscription_plan['plan']['plan_id'],
                                             'action_token': subscription_plan['plan_view']['action_token']}
                subscriptions = api(base, '/api/confirm', subscription_confirmation)
                check('subscription_confirmed_query', subscriptions['result']['status'] == 'SUCCESS'
                      and any(x['tool'] == 'list_subscriptions' and x['data']['items']
                              for x in subscriptions['result']['results']))
                check('subscription_query_no_debit', api(base, '/api/state')['balance']['available_cents'] == balance)
                check('subscription_confirmation_not_replayed',
                      not api(base, '/api/confirm', subscription_confirmation)['ok'])
                planned = api(base, '/api/chat', {'message': '给妈妈转 1 元'})
                check('yellow_confirmation', planned['plan_view']['requires_confirmation'])
                confirmation = {'plan_id': planned['plan']['plan_id'],
                                'action_token': planned['plan_view']['action_token']}
                result = api(base, '/api/confirm', confirmation)
                check('transfer_succeeded', result['result']['status'] == 'SUCCESS')
                transaction = next(x['data']['transaction_id'] for x in result['result']['results'] if x['tool'] == 'create_transfer')
                check('single_debit', api(base, '/api/state')['balance']['available_cents'] == balance - 100)
                check('bind_database_created', (data / 'banking_agent.sqlite3').is_file())
                run('recreate', compose + ['up', '-d', '--no-build', '--force-recreate'])
                base, second_id = endpoint()
                check('new_container', second_id != first_id)
                check('balance_persisted', api(base, '/api/state')['balance']['available_cents'] == balance - 100)
                status = api(base, '/api/v1/transactions/status?' + urlencode({'transaction_id': transaction}))
                check('transaction_persisted', status['status'] == 'succeeded')
                audit_status = api(base, '/api/v1/audit/verify?' + urlencode({'trace_id': planned['trace_id']}))
                check('audit_persisted', audit_status['valid'] and audit_status['count'] > 0)
                check('confirmation_not_replayed', not api(base, '/api/confirm', confirmation)['ok'])
                check('subscription_confirmation_not_replayed_after_recreate',
                      not api(base, '/api/confirm', subscription_confirmation)['ok'])
                check('no_duplicate_debit', api(base, '/api/state')['balance']['available_cents'] == balance - 100)
                report['status'] = 'PASS'
            finally:
                run('logs', compose + ['logs', '--no-color', '--tail', '100'], check=False)
                run('cleanup', compose + ['down', '--volumes'], check=True)
                compose = None
    except Exception as error:
        report['status'] = 'FAIL'
        report['error'] = str(error)
    finally:
        report['original_database_unchanged'] = fingerprint() == before
        if not report['original_database_unchanged']:
            report['status'] = 'FAIL'
        destination = records / 'report.json'
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(f'Docker QA status: {report["status"]}; report: {destination}', flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())

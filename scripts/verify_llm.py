#!/usr/bin/env python3
"""Verify one demo query through Web -> parser -> model -> audit (stdlib only)."""
import argparse
import http.client
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from unittest.mock import patch
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

MESSAGE = '请查询我的账户余额'
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import llm_adapter

REASONS = {'accepted', 'disabled', 'invalid_config', 'timeout', 'network_error', 'http_error',
           'invalid_json', 'invalid_response', 'schema_rejected', 'response_too_large',
           'local_priority', 'feature_demo', 'not_requested', 'offline', 'privacy_blocked',
           'invalid_input', 'rate_limited', 'concurrency_limit', 'incomplete_response'}


def _read_user_variable(name):
    if sys.platform != 'win32':
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            value, kind = winreg.QueryValueEx(key, name)
        return value if kind in {winreg.REG_SZ, winreg.REG_EXPAND_SZ} and isinstance(value, str) else None
    except OSError:
        return None


def load_user_environment():
    # Explicit opt-in; never enumerate registry values or print credentials.
    for name in ('LLM_API_KEY', 'LLM_PROVIDER', 'LLM_MODEL', 'LLM_BASE_URL', 'LLM_TIMEOUT_SECONDS',
                 'LLM_MAX_OUTPUT_TOKENS', 'LLM_CALLS_PER_MINUTE', 'LLM_MAX_CONCURRENT', 'BANK_AGENT_OFFLINE'):
        value = _read_user_variable(name)
        if value is not None:
            os.environ[name] = value


def list_models(config):
    if config.offline or config.invalid or not config.api_key:
        return {'status': 'FAIL', 'reason': 'offline' if config.offline else 'missing_or_invalid_configuration'}
    req = urllib.request.Request(config.base_url + '/models?sub_type=chat',
                                  headers={'Authorization': 'Bearer ' + config.api_key}, method='GET')
    with llm_adapter._open_request(req, timeout=config.timeout_seconds) as response:
        raw = response.read(4194305)
    if len(raw) > 4194304:
        raise ValueError('oversize models response')
    body = json.loads(raw)
    ids = sorted({item['id'] for item in body['data'] if isinstance(item, dict) and
                  isinstance(item.get('id'), str) and len(item['id']) <= 160 and
                  re.fullmatch(r'[A-Za-z0-9_./:-]+', item['id']) and config.api_key not in item['id']})
    return {'status': 'PASS', 'model_ids': ids, 'configured_model_available': config.model in ids,
            'scope': 'authenticated model list only; no inference or quality measurement'}


def isolated_verify(config):
    if not config.enabled:
        return {'status': 'FAIL', 'reason': 'offline' if config.offline else 'missing_or_invalid_configuration'}
    imported_here = 'banking_agent' not in sys.modules
    with tempfile.TemporaryDirectory(prefix='bank-agent-live-llm-') as temporary, \
            patch.dict(os.environ, {'BANK_AGENT_DB_PATH': str(Path(temporary) / 'import.sqlite3'),
                                    'BANK_AGENT_OFFLINE': '1', 'VOICE_ENABLED': '0',
                                    'APP_ENV': 'demo', 'ALLOW_DEMO_CORE': 'true'}):
        import banking_agent as ba
        runtime = None
        try:
            runtime = ba.AgentRuntime(db_path=Path(temporary) / 'query.sqlite3', offline=True)
            runtime.parser.llm = llm_adapter.LLMIntentAdapter(config)
            def snapshot():
                # Comparison stays in memory; account values are not in the report.
                return json.dumps([runtime.bank.balance().data, runtime.bank.subscriptions,
                    [dict(row) for row in runtime.bank.db.execute('SELECT * FROM core_transactions')]], sort_keys=True)
            before = snapshot()
            class QuietHandler(ba.Handler):
                def log_message(self, *args):
                    pass
            with patch.object(ba, 'runtime', runtime), patch.object(ba, 'gateway', ba.AgentGateway(runtime)), \
                    ba.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    report = verify(f'http://127.0.0.1:{server.server_port}')
                finally:
                    server.shutdown()
                    thread.join(timeout=5)
            report.update(mode='real model / temporary Mock bank', model=config.model,
                          mock_bank_unchanged=snapshot() == before)
            report['audit_chain_valid'] = bool(report.get('trace_id') and
                                             runtime.audit.verify_trace(report['trace_id'])['valid'])
            if not report['mock_bank_unchanged'] or not report['audit_chain_valid']:
                report['status'] = 'FAIL'
            return report
        finally:
            for instance in [runtime, ba.runtime if imported_here else None]:
                if instance is not None:
                    instance.recovery_scheduler.stop()
                    instance.bank.close()
                    instance.audit.db.close()


def verify(base):
    parsed = urlsplit(base)
    if (parsed.scheme != 'http' or parsed.hostname not in {'127.0.0.1', 'localhost', '::1'} or
            parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
        raise ValueError('only local demo endpoints are allowed')
    base = base.rstrip('/')

    # Do not forward the public demo token to another endpoint via redirects.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(path, body=None):
        req = urllib.request.Request(base + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': 'Bearer demo-user-session', 'Content-Type': 'application/json'})
        with opener.open(req, timeout=70) as response:
            raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError('oversize response')
        return json.loads(raw)

    # Never confirm, verify credentials, cancel tasks or submit a transfer.
    response = request('/api/chat', {'message': MESSAGE, 'channel': 'web', 'user_id': 'demo-user',
                                     'conversation_id': 'd5-llm-verification'})
    trace = response.get('trace_id')
    if not isinstance(trace, str) or len(trace) > 128:
        raise ValueError('no request trace')
    events = request('/api/v1/audit/events?trace_id=' + quote(trace, safe='')).get('events', [])
    found = [e for e in events if e.get('event_type') == 'parser.result' and e.get('trace_id') == trace]
    if len(found) != 1:
        return {'status': 'FAIL', 'trace_id': trace, 'reason': 'missing_parser_evidence',
                'hint': '请确认运行的是包含 D5 的服务；配置启用不等于本次采用了模型结果。'}
    payload = found[0]['payload']
    evidence = {'source': 'llm' if payload.get('source') == 'llm' else 'rules',
                'attempted': payload.get('attempted') is True,
                'accepted': payload.get('accepted') is True,
                'reason': payload.get('reason') if payload.get('reason') in REASONS else 'invalid_response'}
    results = response.get('result', {}).get('results', [])
    query_ok = (response.get('plan', {}).get('intent') == 'balance_query' and
                payload.get('plan_id') == response.get('plan', {}).get('plan_id') and
                response.get('result', {}).get('status') == 'SUCCESS' and
                len(results) == 1 and results[0].get('tool') == 'get_balance')
    success = query_ok and evidence == {'source': 'llm', 'attempted': True, 'accepted': True, 'reason': 'accepted'}
    metrics = {key: payload[key] for key in ('elapsed_ms', 'http_status') if type(payload.get(key)) is int}
    if isinstance(payload.get('usage'), dict):
        metrics['usage'] = {k: v for k, v in payload['usage'].items() if k in
                            {'prompt_tokens', 'completion_tokens', 'total_tokens'} and type(v) is int and 0 <= v <= 100_000_000}
    return {'status': 'PASS' if success else 'FAIL', 'trace_id': trace, 'evidence': evidence, 'metrics': metrics,
            'query_completed': query_ok,
            'hint': '本次已采用模型结果并完成余额查询。' if success else
                    '未证明本次模型链路生效；规则查询成功也不算 PASS。检查服务进程环境、网络、额度及模型兼容性。'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8090', help='本地演示服务地址（不是模型 API 地址）')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--list-models', action='store_true', help='调用模型列表；不发送聊天请求')
    mode.add_argument('--isolated-model', action='store_true', help='真实模型 + 临时模拟银行全链路，只读余额')
    parser.add_argument('--load-user-env', action='store_true', help='显式读取 Windows 用户级白名单环境变量')
    parser.add_argument('--output', type=Path, help='仅允许保存到项目 data/verification/ 内')
    args = parser.parse_args(argv)
    if args.output:
        output = args.output.resolve()
        if not output.is_relative_to((ROOT / 'data/verification').resolve()):
            parser.error('--output must be within data/verification')
    try:
        if args.load_user_env:
            load_user_environment()
        config = llm_adapter.LLMConfig.from_env()
        report = list_models(config) if args.list_models else isolated_verify(config) if args.isolated_model else verify(args.url)
    except urllib.error.HTTPError as exc:
        report = {'status': 'FAIL', 'reason': 'http_error', 'http_status': exc.code}
        exc.close()
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError,
            RecursionError, http.client.HTTPException):
        report = {'status': 'FAIL', 'reason': 'local_service_unavailable_or_invalid_response',
                  'hint': '先在配置了模型环境变量的终端启动服务；不输出异常原文、响应体或密钥。'}
    if args.output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report['status'] == 'PASS' else 2


if __name__ == '__main__':
    sys.exit(main())

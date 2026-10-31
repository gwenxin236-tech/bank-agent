"""T07 Docker isolation demo. No host eval/exec, banking tools or host mounts.

Only reviewed integer calculation syntax is accepted. Fixed operator-only probes
exercise the container boundary; they are not a general code execution endpoint.
Docker is an external optional QA prerequisite, not a Python runtime dependency.
"""
import ast
import hashlib
import json
import shutil
import subprocess
import threading
import time
import uuid


def validate_code(source):
    if not isinstance(source, str) or len(source.encode('utf-8')) > 4096:
        raise ValueError('source_size')
    try:
        tree = ast.parse(source)
    except (SyntaxError, RecursionError, ValueError):
        raise ValueError('syntax') from None
    if len(list(ast.walk(tree))) > 256 or len(tree.body) != 1:
        raise ValueError('program_size')
    statement = tree.body[0]
    if not (isinstance(statement, ast.Assign) and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name) and statement.targets[0].id == 'result'):
        raise ValueError('result_assignment_required')

    def expression(node, depth=0):
        if depth > 16:
            raise ValueError('expression_depth')
        if isinstance(node, ast.Constant) and type(node.value) is int and abs(node.value) <= 1000000000:
            return 'integer'
        if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) <= 64:
            if all(expression(item, depth + 1) == 'integer' for item in node.elts):
                return 'sequence'
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod)):
            if expression(node.left, depth + 1) == expression(node.right, depth + 1) == 'integer':
                return 'integer'
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            if expression(node.operand, depth + 1) == 'integer':
                return 'integer'
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and len(node.args) == 1 and not node.keywords:
            wanted = 'integer' if node.func.id == 'abs' else 'sequence'
            if node.func.id in {'sum', 'min', 'max', 'len', 'abs'} and expression(node.args[0], depth + 1) == wanted:
                return 'integer'
        raise ValueError('syntax_not_allowed')

    if expression(statement.value) != 'integer':
        raise ValueError('integer_result_required')
    return ast.unparse(tree)


def isolation_checks(container):
    host, config = container.get('HostConfig', {}), container.get('Config', {})
    return {
        'non_root': config.get('User') == '65534:65534',
        'no_network': host.get('NetworkMode') == 'none',
        'read_only_root': host.get('ReadonlyRootfs') is True,
        'no_privilege': host.get('Privileged') is False and 'ALL' in (host.get('CapDrop') or []),
        'no_new_privileges': any(x.split(':')[0] == 'no-new-privileges' for x in (host.get('SecurityOpt') or [])),
        'memory_and_swap': host.get('Memory') == host.get('MemorySwap') == 64 * 1024 * 1024,
        'cpu': host.get('NanoCpus') == 500000000,
        'pids': host.get('PidsLimit') == 32,
        'no_host_mounts': not host.get('Binds') and not container.get('Mounts'),
        'no_published_ports': not host.get('PortBindings'),
        'no_unbounded_disk_logs': host.get('LogConfig', {}).get('Type') == 'none',
    }


# Trusted fixtures only: callers can select a name, never alter their source.
PROBES = {
    'environment': '''import json, os, socket
from pathlib import Path
checks = {'non_root': os.geteuid() != 0,
          'bank_data_absent': not Path('/app/data').exists(),
          'docker_socket_absent': not Path('/var/run/docker.sock').exists(),
          'host_secrets_absent': not any(k in os.environ for k in
              ('LLM_API_KEY','OPENAI_API_KEY','POSTGRES_DSN','REDIS_URL','T07_HOST_SENTINEL'))}
try:
    Path('/sandbox-write-probe').write_text('blocked')
    checks['root_write_blocked'] = False
except OSError:
    checks['root_write_blocked'] = True
try:
    with socket.create_connection(('198.51.100.1', 443), timeout=1):
        checks['outbound_blocked'] = False
except OSError:
    checks['outbound_blocked'] = True
status = dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
checks['no_new_privileges'] = status.get('NoNewPrivs','').strip() == '1'
checks['no_capabilities'] = int(status.get('CapEff','1').strip(),16) == 0
checks['seccomp_filter'] = status.get('Seccomp','').strip() == '2'
try:
    root = Path('/sys/fs/cgroup')
    quota, period = (root/'cpu.max').read_text().split()
    checks['cgroup_cpu_limit'] = quota != 'max' and int(quota)*2 == int(period)
    checks['cgroup_memory_limit'] = int((root/'memory.max').read_text()) == 67108864
    checks['cgroup_pid_limit'] = int((root/'pids.max').read_text()) == 32
except (OSError, ValueError):
    checks['cgroup_limits_readable'] = False
print(json.dumps({'checks':checks}))
''',
    'timeout': 'while True: pass',
    'output': "while True: print('x'*1024, flush=True)",
    'memory': 'allocation = bytearray(256*1024*1024)',
}


class SandboxRunner:
    def __init__(self, audit, breaker, timeout_seconds=3, max_output_bytes=8192):
        if not 1 <= timeout_seconds <= 10 or not 1024 <= max_output_bytes <= 65536:
            raise ValueError('invalid sandbox limits')
        self.audit, self.breaker = audit, breaker
        self.timeout_seconds, self.max_output_bytes = timeout_seconds, max_output_bytes
        self.docker = shutil.which('docker')
        self._lock = threading.Lock()

    def execute(self, source, actor_id='sandbox-operator'):
        with self._lock:
            trace = self._trace()
            if self.breaker.is_locked():
                return self._finish(trace, source, actor_id, {'status': 'LOCKED'})
            try:
                canonical = validate_code(source)
            except ValueError:
                return self._finish(trace, source, actor_id, {'status': 'REJECTED'})
            # Formatting, not evaluation: Python source runs only in the container.
            worker = 'import json\n' + canonical + "\nprint(json.dumps({'result': result}))\n"
            return self._run(trace, source, worker, actor_id, probe=False)

    def probe(self, name, actor_id='sandbox-operator'):
        with self._lock:
            trace = self._trace()
            if self.breaker.is_locked():
                return self._finish(trace, '', actor_id, {'status': 'LOCKED'})
            if name not in PROBES:
                return self._finish(trace, '', actor_id, {'status': 'REJECTED'})
            return self._run(trace, PROBES[name], PROBES[name], actor_id, probe=True)

    @staticmethod
    def _trace():
        return 'trace_' + uuid.uuid4().hex.translate(str.maketrans('0123456789abcdef', 'abcdefghijklmnop'))

    def _run(self, trace, source, worker, actor, probe):
        self.audit.write(trace, 'sandbox.started', {'operation': 'isolated_demo',
            'source_digest': list(hashlib.sha256(source.encode()).digest()), 'probe': probe}, actor=actor)
        return self._finish(trace, source, actor, self._launch(worker, probe))

    def _finish(self, trace, source, actor, outcome):
        result = dict(outcome, trace_id=trace, scope='LOCAL_ISOLATED_DEMO')
        result['ok'] = result['status'] == 'SUCCESS'
        if not result['ok'] and result['status'] != 'LOCKED':
            self.breaker.failure()
        source = source if isinstance(source, str) else ''
        image = result.get('image_id', '').removeprefix('sha256:')
        # Bytes survive the existing PII scrubber without hiding numeric hex runs.
        self.audit.write(trace, 'sandbox.finished', {'operation': 'isolated_demo',
            'source_digest': list(hashlib.sha256(source.encode('utf-8', errors='replace')).digest()),
            'image_digest': list(bytes.fromhex(image)) if len(image) == 64 else [],
            'output_digest': list(hashlib.sha256(json.dumps(result.get('data'), sort_keys=True).encode()).digest()),
            'status': result['status'], 'output_bytes': result.get('output_bytes', 0),
            'cleanup_ok': result.get('cleanup_ok', True)}, actor=actor)
        return result

    def _docker(self, arguments, check=True):
        result = subprocess.run([self.docker] + arguments, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=15)
        if check and result.returncode:
            raise RuntimeError('docker_command_failed')
        return result

    def _launch(self, worker, probe=False):
        if self.docker is None:
            return {'status': 'UNAVAILABLE', 'cleanup_ok': True}
        name = 'bank-sandbox-' + uuid.uuid4().hex
        attempted, result = False, {'status': 'UNAVAILABLE', 'cleanup_ok': True}
        try:
            engine = json.loads(self._docker(['version', '--format', '{{json .Server}}']).stdout)
            image = json.loads(self._docker(['image', 'inspect', '--format', '{{json .}}', 'python:3.12-slim']).stdout)
            if engine['Os'] != 'linux' or image['Os'] != 'linux' or image['Architecture'] != engine['Arch'] or image['Config'].get('Volumes'):
                raise ValueError('unsupported_base_image')
            result['image_id'] = image['Id']
            attempted = True
            self._docker(['create', '--name', name, '--pull', 'never', '--network', 'none',
                '--read-only', '--user', '65534:65534', '--cap-drop', 'ALL',
                '--security-opt', 'no-new-privileges', '--memory', '64m', '--memory-swap', '64m',
                '--cpus', '0.5', '--pids-limit', '32', '--shm-size', '64k', '--log-driver', 'none',
                '--entrypoint', 'python', image['Id'], '-I', '-S', '-B', '-c', worker])
            container = json.loads(self._docker(['inspect', '--format', '{{json .}}', name]).stdout)
            result['isolation_checks'] = isolation_checks(container)
            if not all(result['isolation_checks'].values()):
                result['status'] = 'ISOLATION_FAILED'
            else:
                status, output, exit_code = self._attached(name)
                result.update(status=status, output_bytes=len(output), exit_code=exit_code)
                state = json.loads(self._docker(['inspect', '--format', '{{json .State}}', name]).stdout)
                result['oom_killed'] = state.get('OOMKilled', False)
                if status == 'EXITED':
                    if state.get('OOMKilled'):
                        result['status'] = 'RESOURCE_LIMIT'
                    elif exit_code != 0:
                        result['status'] = 'FAILED'
                    else:
                        value = json.loads(output)
                        valid = (isinstance(value, dict) and set(value) == {'checks'}
                                 and isinstance(value['checks'], dict) and bool(value['checks'])
                                 and all(type(v) is bool and v for v in value['checks'].values())) if probe else (
                                     isinstance(value, dict) and set(value) == {'result'}
                                     and type(value['result']) is int and abs(value['result']) <= 9007199254740991)
                        result['status'] = 'SUCCESS' if valid else 'INVALID_OUTPUT'
                        if valid:
                            result['data'] = value
        except (OSError, RuntimeError, ValueError, KeyError, subprocess.SubprocessError) as error:
            result.update(status='FAILED' if attempted else 'UNAVAILABLE', error_kind=type(error).__name__)
        finally:
            if attempted:
                try:
                    cleanup = self._docker(['rm', '--force', name], check=False)
                    result['cleanup_ok'] = cleanup.returncode == 0 or b'No such container' in cleanup.stderr
                except (OSError, subprocess.SubprocessError):
                    result['cleanup_ok'] = False
                if not result['cleanup_ok']:
                    result['status'] = 'CLEANUP_FAILED'
        return result

    def _attached(self, name):
        process = subprocess.Popen([self.docker, 'start', '--attach', name], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        captured, exceeded = bytearray(), threading.Event()

        def read_output():
            while chunk := process.stdout.read1(1024):
                remaining = self.max_output_bytes - len(captured)
                captured.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    exceeded.set()
                    # Keep draining discarded bytes so docker attach can exit
                    # after the container is killed; retained memory stays bounded.

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        deadline, status = time.monotonic() + self.timeout_seconds, 'EXITED'
        try:
            while process.poll() is None:
                if exceeded.is_set() or time.monotonic() >= deadline:
                    status = 'OUTPUT_LIMIT' if exceeded.is_set() else 'TIMEOUT'
                    self._docker(['kill', name], check=False)
                    break
                time.sleep(.01)
            process.wait(timeout=5)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            reader.join(timeout=3)
            process.stdout.close()
        if exceeded.is_set():
            status = 'OUTPUT_LIMIT'
        return status, bytes(captured), process.returncode

"""Один networkless Docker worker; лимиты и журнал принадлежат приложению."""
from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid

from api_agent.workspace.execution.broker import WorkspaceError
from api_agent.workspace.execution.preflight import check_code
from api_agent.workspace.execution.diagnostics import public_keys, valid_detail
from api_agent.openapi.parser import canonical

DOCKER = os.environ.get('WORKSPACE_DOCKER', r'C:\Program Files\Docker\Docker\resources\bin\docker.exe' if os.name == 'nt' else 'docker')
DOCKER_HOST = os.environ.get('WORKSPACE_DOCKER_HOST', 'npipe:////./pipe/dockerDesktopLinuxEngine' if os.name == 'nt' else 'unix:///var/run/docker.sock')
ERRORS = {'SyntaxError', 'IndentationError', 'NameError', 'TypeError', 'KeyError', 'IndexError', 'ValueError',
    'AssertionError', 'MemoryError', 'ZeroDivisionError', 'ImportError', 'ModuleNotFoundError', 'AttributeError',
    'CallDenied', 'ApprovalRejected', 'BudgetExceeded', 'Cancelled', 'UncertainWrite', 'OSError', 'PermissionError'}


@dataclass(frozen=True)
class SandboxLimits:
    """Раздельные CPU quota, память/PIDs/tmpfs, wire/output и active block time."""
    seconds: float = 20
    memory_mb: int = 128
    pids: int = 32
    tmp_mb: int = 8
    max_blocks: int = 24
    max_no_progress_blocks: int = 8
    max_code_bytes: int = 16384
    max_wire_bytes: int = 4194304

    def __post_init__(self):
        if not 0 < self.seconds <= 60 or not 64 <= self.memory_mb <= 512 or not 8 <= self.pids <= 64 or not 1 <= self.tmp_mb <= 32:
            raise ValueError('invalid_sandbox_limits')
        if any(type(n) is not int or n <= 0 for n in (self.max_blocks, self.max_no_progress_blocks, self.max_code_bytes, self.max_wire_bytes)):
            raise ValueError('invalid_sandbox_limits')


class WorkspaceSandbox:
    """Raw code только в stdin/RAM/tmpfs; Docker logs выключены, crash без resume."""
    def __init__(self, broker, *, image, root, limits=None):
        if not isinstance(image, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image): raise ValueError('pinned_image_required')
        self.broker, self.image, self.root = broker, image, Path(root).resolve()
        self.limits = limits or SandboxLimits()
        self.name = 'api-agent-workspace-' + uuid.uuid4().hex
        self.process = None
        self.closed = False
        self.no_progress = 0
        self.frames = queue.Queue(maxsize=8)
        self.failure = None
        self.wire_bytes = 0
        self.directory = self.root / self.name

    def _docker(self, *args, check=True):
        result = subprocess.run([DOCKER, '--config', str(self.directory / 'docker-config'), '--host', DOCKER_HOST, *args], stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=30)
        if check and result.returncode: raise WorkspaceError('docker_unavailable')
        return result

    def inspect(self):
        return json.loads(self._docker('inspect', self.name).stdout)[0]

    def exists(self):
        # Ошибка engine не считается доказательством отсутствия контейнера.
        return bool(self._docker('container', 'ls', '-a', '--filter', 'name=^/' + self.name + '$', '--format', '{{.ID}}').stdout.strip())

    def __enter__(self):
        self.broker.reserve_active('sandbox', self.broker.limits.max_seconds)
        try:
            self.broker.event('sandbox_requested')
            info = json.loads(self._docker('image', 'inspect', self.image).stdout)[0]
            if info['Os'] != 'linux' or info['Config'].get('Labels', {}).get('org.api-agent.workspace') != 's09d2-python310-v1':
                raise WorkspaceError('sandbox_image_required')
            self.directory.mkdir(parents=True)
            runtime = self.directory / 'runtime' / 'api_agent'
            shutil.copytree(Path(__file__).parents[2], runtime, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            artifact = self.broker.artifact.save(self.directory / 'sdk')
            # Пустой config исключает автоматический перенос host proxy credentials в ENV.
            command = [DOCKER, '--config', str(self.directory / 'docker-config'), '--host', DOCKER_HOST, 'run', '--rm', '--pull=never', '-i', '--name', self.name,
                '--network=none', '--ipc=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user=65534:65534',
                '--pids-limit=' + str(self.limits.pids), '--memory=' + str(self.limits.memory_mb) + 'm',
                '--memory-swap=' + str(self.limits.memory_mb) + 'm', '--cpus=1', '--log-driver=none',
                '--ulimit=core=0:0', '--ulimit=nofile=64:64',
                '--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=' + str(self.limits.tmp_mb) + 'm,mode=1777',
                '--mount=type=bind,source=' + str(runtime.parent) + ',target=/runtime,readonly',
                '--mount=type=bind,source=' + str(artifact) + ',target=/artifact,readonly',
                '--workdir=/tmp', '--entrypoint=/usr/local/bin/python', self.image, '-I', '-u', '/runtime/api_agent/workspace/execution/worker.py']
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            self.reader = threading.Thread(target=self._read, daemon=True); self.reader.start()
            self._send({'artifact_hash': self.broker.artifact.hash, 'memory_bytes': self.limits.memory_mb * 1024 * 1024,
                'cpu_seconds': int(self.broker.limits.max_seconds) + 1,
                'provided_bodies': [self.broker.workflow.auth['login_operation']] if self.broker.workflow.auth else []})
            ready = self._next(time.monotonic() + 30)
            if ready != {'kind': 'ready', 'artifact_hash': self.broker.artifact.hash}: raise WorkspaceError('worker_start_failed')
            state = self.inspect()
            self.broker.settle_active('sandbox')
            self.broker.event('sandbox_started', {'image': self.image, 'limits': vars(self.limits),
                'network': state['HostConfig']['NetworkMode'], 'readonly': state['HostConfig']['ReadonlyRootfs'],
                'logging': state['HostConfig']['LogConfig']['Type']})
            return self
        except BaseException:
            self.broker.settle_active('sandbox')
            self.close()
            raise

    def _read(self):
        try:
            while True:
                data = self.process.stdout.readline(524289)
                if not data:
                    self.failure = 'WorkerExited'; return
                self.wire_bytes += len(data)
                if len(data) > 524288 or self.wire_bytes > self.limits.max_wire_bytes:
                    self.failure = 'ProtocolDenied'; return
                try: self.frames.put_nowait(data)
                except queue.Full: self.failure = 'ProtocolDenied'; return
        except (OSError, ValueError): self.failure = 'WorkerExited'

    def _next(self, deadline):
        while time.monotonic() < deadline:
            if self.failure: raise WorkspaceError(self.failure)
            try: data = self.frames.get(timeout=min(.1, max(.001, deadline - time.monotonic())))
            except queue.Empty: continue
            try: return json.loads(data)
            except (ValueError, UnicodeError, RecursionError): raise WorkspaceError('ProtocolDenied') from None
        raise WorkspaceError('SandboxTimeout')

    def _send(self, value):
        try:
            self.process.stdin.write(canonical(value) + b'\n'); self.process.stdin.flush()
        except (OSError, ValueError): raise WorkspaceError('WorkerExited') from None

    def execute(self, code, *, call_id=None):
        if self.closed: raise WorkspaceError('worker_closed')
        self.broker.guard()
        if type(code) is not str or not code.strip() or len(code.encode()) > self.limits.max_code_bytes:
            raise WorkspaceError('code_limit')
        if call_id is not None and (type(call_id) is not str or len(call_id) > 200): raise WorkspaceError('invalid_call_id')
        records = self.broker.run['code_executions']
        if len(records) >= self.limits.max_blocks or self.no_progress >= self.limits.max_no_progress_blocks:
            self.broker.stop('code_correction_limit'); raise WorkspaceError('code_correction_limit')
        execution = uuid.uuid4().hex
        self.broker.context = {'execution_id': execution, 'code_revision': len(records) + 1, 'call_id': call_id}
        # HMAC с несохраняемым ключом не позволяет перебрать пароль из code hash.
        private = bool(self.broker.workflow.secrets())
        record = {**self.broker.context, 'code_hmac': hmac.new(self.broker.audit_key, code.encode(), hashlib.sha256).hexdigest(),
            'code_bytes': len(code.encode()), 'code': {'omitted': 'private_run'} if private else code,
            'state': 'started', 'error': None}
        records.append(record)
        self.broker.reserve_active('code', self.limits.seconds)
        try: self.broker.event('code_started', {'code_hmac': record['code_hmac']})
        except BaseException:
            self.broker.settle_active('code')
            raise
        started = time.monotonic()
        deadline = min(started + self.limits.seconds, self.broker.session.deadline,
                       started + self.broker.remaining_active('code'))
        before = self.broker.session.requests
        sequence, output, error, error_line, error_object_type, variables, truncated = 0, '', None, None, None, [], False
        error_detail, diagnostic_keys = None, public_keys(self.broker.artifact)
        variable_types = {}
        http_halted = False
        try:
            supplied = (self.broker.workflow.auth or {}).get('login_operation')
            diagnostic = check_code(code, self.broker.artifact, provided_bodies={supplied} if supplied else set())
            if diagnostic:
                error = diagnostic['error']
                record['preflight'] = self.broker.safe(diagnostic)
                return {'execution_id': execution, 'error': error, 'preflight': record['preflight']}
            self._send({'execution_id': execution, 'code': code})
            while True:
                frame = self._next(deadline)
                if not isinstance(frame, dict) or frame.get('execution_id') != execution: raise WorkspaceError('ProtocolDenied')
                if frame.get('kind') == 'call':
                    if set(frame) != {'kind', 'execution_id', 'sequence', 'request'} or type(frame['sequence']) is not int or frame['sequence'] != sequence + 1:
                        raise WorkspaceError('ProtocolDenied')
                    sequence += 1
                    human = getattr(self.broker.workflow, 'human', None)
                    waited = human.waited_seconds if human else 0
                    try:
                        if http_halted and isinstance(frame['request'], dict) and frame['request'].get('kind') == 'operation':
                            self.broker.event('call_rejected', {'reason': 'unexpected_http_status'})
                            raise WorkspaceError('unexpected_http_status')
                        response = {'result': self.broker.dispatch(frame['request']), 'error': None}
                    except WorkspaceError as caught:
                        response = {'error': str(caught)}
                        http_halted |= str(caught) == 'unexpected_http_status'
                    if human: deadline += human.waited_seconds - waited
                    self._send(response)
                elif frame.get('kind') == 'done':
                    if (set(frame) != {'kind', 'execution_id', 'output', 'output_truncated', 'error', 'error_line', 'error_object_type', 'error_detail', 'variables', 'variable_types'}
                        or type(frame['output']) is not str or len(frame['output'].encode()) > 8192
                        or type(frame['output_truncated']) is not bool or type(frame['variables']) is not list
                        or (frame['error'] is not None and (type(frame['error']) is not str or len(frame['error']) > 80))
                        or (frame['error_line'] is not None and (type(frame['error_line']) is not int
                            or not 1 <= frame['error_line'] <= len(code.splitlines())))
                        or frame['error_object_type'] not in (None, 'dict', 'list', 'str', 'int', 'float', 'bool')
                        or not valid_detail(frame['error_detail'], diagnostic_keys)
                        or len(frame['variables']) > 32 or any(type(v) is not str or len(v) > 80 for v in frame['variables'])
                        or type(frame['variable_types']) is not dict or set(frame['variable_types']) != set(frame['variables'])
                        or any(type(v) is not str or v not in {'dict', 'list', 'str', 'int', 'float', 'bool',
                            'tuple', 'set', 'NoneType', 'other'} for v in frame['variable_types'].values())):
                        raise WorkspaceError('ProtocolDenied')
                    private = private or bool(self.broker.workflow.secrets())
                    if private: record['code'] = {'omitted': 'private_run'}
                    output = '[output omitted: private run]' if private else self.broker.safe(frame['output'])
                    error = frame['error'] if frame['error'] in ERRORS or frame['error'] is None else 'PythonError'
                    error_line = frame['error_line']
                    error_object_type = frame['error_object_type']
                    error_detail = self.broker.safe(frame['error_detail'])
                    variables = [] if private else self.broker.safe(frame['variables'])
                    variable_types = {} if private else self.broker.safe(frame['variable_types'])
                    truncated = frame['output_truncated']
                    break
                else: raise WorkspaceError('ProtocolDenied')
        except WorkspaceError as caught:
            error = str(caught) if str(caught) in {'WorkerExited', 'ProtocolDenied', 'SandboxTimeout'} else 'WorkerExited'
            self.broker.stop('sandbox_' + error.lower())
            self.close()
        finally:
            progress = self.broker.session.requests > before or self.broker.finished
            self.no_progress = 0 if progress else self.no_progress + 1
            record.update(state='returned' if not self.closed else 'interrupted', error=error, error_line=error_line, error_object_type=error_object_type, error_detail=error_detail,
                output=output, output_untrusted=True, output_bytes=len(output.encode()), output_truncated=truncated,
                variables=variables, variable_types=variable_types,
                wall_seconds=time.monotonic() - started, requests=self.broker.session.requests - before)
            self.broker.settle_active('code')
            self.broker.event('code_returned', {'error': error, 'progress': progress})
            self.broker.context = {}
        return {'execution_id': execution, 'output': output, 'output_truncated': truncated,
                'error': error, 'error_line': error_line, 'error_object_type': error_object_type, 'error_detail': error_detail,
                'variables': variables, 'variable_types': variable_types}

    def close(self):
        if self.closed: return
        self.closed = True
        try:
            if self.process:
                # При flood attach-клиент может блокировать daemon на полном pipe.
                # Сначала освобождаем attach, затем удаляем сам контейнер.
                if self.process.poll() is None: self.process.kill()
                try: self.process.wait(timeout=5)
                except subprocess.TimeoutExpired: self.process.kill(); self.process.wait(timeout=5)
                for stream in (self.process.stdin, self.process.stdout):
                    try: stream.close()
                    except OSError: pass
                self.reader.join(timeout=2)
            self._docker('rm', '-f', self.name, check=False)
            # --rm и daemon removal асинхронны: ошибка already removing не cleanup.
            deadline = time.monotonic() + 10
            while self.exists():
                if time.monotonic() >= deadline: raise WorkspaceError('sandbox_cleanup_failed')
                time.sleep(.1)
        finally:
            if self.directory.exists():
                # Только созданный нами child внутри указанного workspace root.
                if self.directory.parent != self.root or self.directory.is_symlink(): raise WorkspaceError('unsafe_cleanup_path')
                shutil.rmtree(self.directory)

    def __exit__(self, *args): self.close()

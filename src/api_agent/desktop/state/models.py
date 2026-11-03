"""Один собственный llama-server; identity основана на GGUF/runtime/template."""
from copy import deepcopy
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import threading
import time

from api_agent.agent.model import LocalModel, goal_sampling
from api_agent.transport.http import loopback_origin
from api_agent.transport.socket_deadline import SocketDeadline
from api_agent.validation import fingerprint
from api_agent.workspace.context.prompt import TOOLS


def file_hash(path, *, gguf=False):
    if str(path).startswith(('\\\\','//')):raise ValueError('model_identity_local_file_required')
    path = Path(path)
    if not path.is_file() or gguf and path.suffix.lower() != '.gguf':
        raise ValueError('model_identity_file_missing')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        first = stream.read(1048576)
        if gguf and first[:4] != b'GGUF': raise ValueError('model_identity_not_gguf')
        digest.update(first)
        while block := stream.read(8388608): digest.update(block)
    return digest.hexdigest()


def get_json(endpoint, path):
    _, port = loopback_origin(endpoint)
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=3)
    deadline=time.monotonic()+3
    try:
        connection.connect()
        with SocketDeadline(connection.sock,deadline) as guard:
            connection.request('GET', path); response = connection.getresponse()
            try:raw = response.read(1048577)
            finally:guard.check()
            if response.status != 200 or len(raw) > 1048576: raise ValueError('model_runtime_unavailable')
            value = json.loads(raw)
            if not isinstance(value, dict): raise ValueError('model_runtime_metadata')
            return value
    finally: connection.close()


class ModelManager:
    def __init__(self, *, launcher=None):
        self.process = None; self.process_profile = None; self.active = False
        self.launcher = launcher or subprocess.Popen
        self.lock = threading.RLock(); self.current = None

    def acquire(self, *, expected=None):
        with self.lock:
            if self.active: raise ValueError('model_in_use')
            if self.current is None: raise ValueError('model_not_prepared')
            if expected is not None and self.current != expected: raise ValueError('model_identity_changed')
            self.active = True

    def release(self):
        with self.lock: self.active = False

    def _stop(self):
        process = self.process
        if process is not None:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)
            self.process = None; self.process_profile = None
        self.current = None

    def close(self):
        with self.lock:
            if self.active: raise ValueError('model_in_use')
            self._stop()

    def _start(self, profile, endpoint, cancel):
        key = fingerprint(profile)
        if self.process is not None and self.process_profile == key and self.process.poll() is None: return
        self._stop()
        executable = str(Path(profile['executable']).resolve())
        file_hash(executable); file_hash(profile['gguf'], gguf=True)
        # Занятый endpoint не принадлежит нам; его нельзя усыновлять/останавливать.
        _, port = loopback_origin(endpoint)
        import socket
        with socket.socket() as probe:
            try: probe.bind(('127.0.0.1', port))
            except OSError: raise ValueError('model_port_in_use') from None
        command = [executable, '-m', str(Path(profile['gguf']).resolve()), '--host', '127.0.0.1',
            '--port', str(port), '-c', str(profile['context_tokens']), '-np', '1', '--jinja',
            '--alias', profile.get('model_name') or Path(profile['gguf']).stem,
            '-ngl', str(profile.get('gpu_layers', 0)), '--no-webui']
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        self.process = self.launcher(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, creationflags=flags)
        self.process_profile = key
        deadline = time.monotonic() + profile.get('startup_seconds', 120)
        while time.monotonic() < deadline:
            if cancel and cancel.is_set(): self._stop(); raise ValueError('model_loading_cancelled')
            if self.process.poll() is not None: self._stop(); raise ValueError('model_process_failed')
            try:
                if get_json(endpoint, '/health').get('status') == 'ok': return
            except (OSError, ValueError, http.client.HTTPException): pass
            time.sleep(0.1)
        self._stop(); raise ValueError('model_loading_timeout')

    def prepare(self, record, *, expected=None, cancel=None):
        with self.lock:
            if self.active: raise ValueError('model_in_use')
            profile = deepcopy(record['profile'])
            if profile.get('kind') not in {'gguf', 'server'}: raise ValueError('model_profile_kind')
            context, output = profile.get('context_tokens'), profile.get('output_tokens')
            if type(context) is not int or type(output) is not int or not 256 <= context <= 1048576 or not 1 <= output < context:
                raise ValueError('model_context_invalid')
            endpoint = profile.get('endpoint') if profile['kind'] == 'server' else 'http://127.0.0.1:' + str(profile.get('port', 18085))
            loopback_origin(endpoint)
            if profile['kind'] == 'gguf':
                if type(profile.get('gpu_layers', 0)) is not int or not 0 <= profile.get('gpu_layers', 0) <= 999:
                    raise ValueError('model_gpu_layers')
                if not 1 <= profile.get('startup_seconds', 120) <= 600: raise ValueError('model_loading_timeout')
                self._start(profile, endpoint, cancel)
            elif self.process is not None: self._stop()
            try:
                get_json(endpoint, '/health')
                props = get_json(endpoint, '/props'); models = get_json(endpoint, '/v1/models')['data']
                if len(models) != 1: raise ValueError('model_identity_ambiguous')
                loaded = models[0]; name = loaded['id']; aliases = [name] + loaded.get('aliases', [])
                if profile.get('model_name') and profile['model_name'] not in aliases:
                    raise ValueError('model_identity_alias_mismatch')
                available = props['default_generation_settings']['n_ctx']
                supported = (loaded.get('meta') or {}).get('n_ctx_train')
                if type(available) is not int or type(supported) is not int or context > min(available, supported):
                    raise ValueError('model_context_unavailable')
                path = props.get('model_path')
                if not isinstance(path, str) or not path: raise ValueError('model_identity_unproven')
                digest = file_hash(path, gguf=True)
                if profile['kind'] == 'gguf' and digest != file_hash(profile['gguf'], gguf=True):
                    raise ValueError('model_identity_changed')
                build, template = props.get('build_info'), props.get('chat_template')
                if not isinstance(build, str) or not build or not isinstance(template, str) or not template:
                    raise ValueError('model_identity_unproven')
                sampling = goal_sampling(Path(path).stem)
                identity = fingerprint({'gguf': digest, 'build': build, 'template': template,
                    'context': context, 'output': output, 'sampling': sampling})
                snapshot = {'schema_version': 'model-snapshot-v1', 'profile': deepcopy(record),
                    'identity': identity, 'gguf_sha256': digest, 'runtime_build': build,
                    'template_hash': fingerprint(template), 'model_name': name, 'endpoint': endpoint,
                    'context_tokens': context, 'output_tokens': output, 'sampling': sampling}
                if expected is not None and expected.get('identity') != identity:
                    raise ValueError('model_identity_changed')
                model = LocalModel(endpoint)
                model.count_tokens({'messages': [{'role': 'user', 'content': 'Проверка конфигурации.'}],
                    'tools': TOOLS, 'tool_choice': 'required', 'model': name})
                self.current = snapshot
                return deepcopy(snapshot)
            except (KeyError, TypeError, OSError, http.client.HTTPException) as error:
                raise ValueError('model_identity_unavailable') from error

"""Полная readiness локального исполнения; без business HTTP и Run."""
import importlib.metadata
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile

from api_agent.storage import SQLiteHistory
from api_agent.workspace.execution.sandbox import DOCKER, DOCKER_HOST


def inspect_storage(path):
    """Миграции проверяются на SQLite backup; исходная база не переписывается."""
    try:
        with tempfile.TemporaryDirectory(prefix='api-agent-doctor-') as directory:
            destination = Path(directory) / 'history.sqlite3'
            if path and Path(path).exists():
                with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as source, \
                        closing(sqlite3.connect(destination)) as copy:
                    source.backup(copy)
            with SQLiteHistory(destination) as history:
                version = history.db.execute('PRAGMA user_version').fetchone()[0]
                integrity = history.db.execute('PRAGMA integrity_check').fetchone()[0]
                tables = {r[0] for r in history.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if version != 5 or integrity != 'ok' or not {'runs', 'messages', 'events', 'reports'} <= tables:
                    raise ValueError('storage_doctor_failed')
        return {'name': 'storage', 'state': 'ready', 'schema_version': version}
    except (OSError, ValueError, sqlite3.Error):
        return {'name': 'storage', 'state': 'unavailable', 'reason': 'storage_doctor_failed'}


def inspect_sandbox(image, *, docker=None):
    try:
        if not isinstance(image, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', image):
            raise ValueError('pinned_image_required')
        with tempfile.TemporaryDirectory(prefix='api-agent-docker-') as directory:
            command = [str(docker or DOCKER), '--config', directory, '--host', DOCKER_HOST]
            def execute(args):
                result = subprocess.run(command + args, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, timeout=15)
                if result.returncode or len(result.stdout) > 8192: raise ValueError('docker_unavailable')
                return result.stdout
            info = json.loads(execute(['image', 'inspect', '--format',
                '{"os":{{json .Os}},"label":{{json (index .Config.Labels "org.api-agent.workspace")}}}', image]))
            if info != {'os': 'linux', 'label': 's09d2-python310-v1'}: raise ValueError('sandbox_image_required')
            output = execute(['run', '--rm', '--pull=never', '--network=none', '--ipc=none', '--read-only',
                '--cap-drop=ALL', '--security-opt=no-new-privileges', '--user=65534:65534', '--pids-limit=32',
                '--memory=128m', '--memory-swap=128m', '--cpus=1', '--log-driver=none',
                '--entrypoint=/usr/local/bin/python', image, '-I', '-c',
                'import os,sys,json;print(json.dumps({"python":list(sys.version_info[:2]),"uid":os.geteuid()}))'])
            if json.loads(output) != {'python': [3, 10], 'uid': 65534}: raise ValueError('sandbox_image_required')
        return {'name': 'sandbox', 'state': 'ready', 'image': image}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'name': 'sandbox', 'state': 'unavailable', 'reason': 'docker_unavailable'}


def inspect_environment(*, image, history_path=None, model_record=None, manager=None, docker=None):
    checks = []
    try:
        versions = {name: importlib.metadata.version(name) for name in ('textual', 'tree-sitter', 'tree-sitter-json')}
        if not (3, 11) <= sys.version_info[:2] <= (3, 12): raise ValueError('unsupported_host_python')
        checks.append({'name': 'dependencies', 'state': 'ready', 'versions': versions})
    except (importlib.metadata.PackageNotFoundError, ValueError):
        checks.append({'name': 'dependencies', 'state': 'unavailable', 'reason': 'setup_dependencies_missing'})
    checks.append(inspect_storage(history_path)); checks.append(inspect_sandbox(image, docker=docker))
    if model_record is None:
        checks.append({'name': 'model', 'state': 'not_configured'})
    else:
        from api_agent.desktop.state.models import ModelManager
        owned_manager = manager is None; manager = manager or ModelManager()
        try:
            snapshot = manager.prepare(model_record)
            checks.append({'name': 'model', 'state': 'ready', 'identity': snapshot['identity'],
                'context_tokens': snapshot['context_tokens'], 'output_tokens': snapshot['output_tokens']})
        except (OSError, ValueError):
            checks.append({'name': 'model', 'state': 'unavailable', 'reason': 'model_runtime_unavailable'})
        finally:
            if owned_manager: manager.close()
    return {'schema_version': 'environment-doctor-v1',
            'ready': all(c['state'] == 'ready' for c in checks), 'checks': checks}

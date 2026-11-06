"""Терминальная установка из checkout: stdlib → venv → pinned worker → TUI."""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import uuid
import venv

ROOT = Path(__file__).resolve().parents[1]


def docker_command(directory):
    default = r'C:\Program Files\Docker\Docker\resources\bin\docker.exe' if os.name == 'nt' else 'docker'
    executable = os.environ.get('WORKSPACE_DOCKER') or shutil.which('docker') or default
    host = os.environ.get('WORKSPACE_DOCKER_HOST') or ('npipe:////./pipe/dockerDesktopLinuxEngine' if os.name == 'nt' else 'unix:///var/run/docker.sock')
    return [executable, '--config', str(directory / 'docker-client'), '--host', host]


def prepare_worker(directory, docker):
    image_file = directory / 'worker-image.id'
    # Новый тег не переименовывает образ, закреплённый за прежним Project/Run.
    tag = 'api-agent-workspace:s14-' + uuid.uuid4().hex[:12]
    subprocess.run(docker + ['build', '--platform=linux/amd64', '--network=none',
        '--file', str(ROOT / 'docker/setup/Dockerfile'), '--tag', tag,
        '--iidfile', str(image_file), str(ROOT / 'docker/setup')], check=True)
    image = image_file.read_text(encoding='utf-8').strip()
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image): raise ValueError('pinned_image_required')
    return image


def prepare(state):
    if not (3, 11) <= sys.version_info[:2] <= (3, 12):
        print('Требуется Python3.11 или3.12.'); return 3
    if os.name != 'nt' or os.environ.get('PROCESSOR_ARCHITECTURE', '').lower() not in {'amd64', 'x86_64'}:
        print('Проверяемый профиль установки: Windows x64 и Docker Linux containers.'); return 3
    directory = state.parent; directory.mkdir(parents=True, exist_ok=True)
    docker = docker_command(directory)
    subprocess.run(docker + ['info', '--format', '{{.OSType}}'], check=True, timeout=20)
    environment = ROOT / '.venv'
    executable = environment / 'Scripts/python.exe'
    if not executable.exists(): venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run([str(executable), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements-lock.txt')], check=True)
    image = prepare_worker(directory, docker)
    code = ('import sys;sys.path.insert(0,sys.argv[1]);'
            'from api_agent.desktop.state.store import DesktopStore;'
            's=DesktopStore(sys.argv[2]);s.set_setting("worker_image",sys.argv[3]);s.close()')
    subprocess.run([str(executable), '-I', '-c', code, str(ROOT / 'src'), str(state), image], check=True)
    print('Подготовлено. Запуск:')
    print(str(executable) + ' main.py --tui --state ' + str(state))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Установка API Testing Agent через терминал; Docker устанавливается заранее.')
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument('--prepare', action='store_true', help='Создать .venv, установить зависимости и собрать worker')
    actions.add_argument('--doctor', action='store_true', help='Проверить зависимости, SQLite, worker и выбранную модель')
    parser.add_argument('--state', default=str(ROOT / '.api-agent/ui.sqlite3'))
    parser.add_argument('--model-id', help='ID уже настроенной модели для doctor')
    args = parser.parse_args(argv); state = Path(args.state).resolve()
    try:
        if args.prepare: return prepare(state)
        sys.path.insert(0, str(ROOT / 'src'))
        from api_agent.desktop.state.store import DesktopStore
        from api_agent.setup.environment import inspect_environment
        if not state.exists():
            print('Сначала выполните --prepare и настройте модель в TUI.'); return 3
        with DesktopStore(state) as store:
            record = store.model(args.model_id) if args.model_id else None
            result = inspect_environment(image=store.setting('worker_image'),
                history_path=state.parent / 'history.sqlite3', model_record=record)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['ready'] else 3
    except (OSError, ValueError, subprocess.SubprocessError, ImportError) as error:
        print('Подготовка не завершена: ' + type(error).__name__ + '. Проверьте Python, Docker и доступ к зависимостям.', file=sys.stderr)
        return 3


if __name__ == '__main__': raise SystemExit(main())

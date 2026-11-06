"""Независимый runner117 agent attempts +3 history actions; неизменная версия."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import traceback
import uuid
import zipfile

from api_agent.agent.model import LocalModel
from api_agent.desktop.state.models import ModelManager
from api_agent.desktop.state.store import DesktopStore
from api_agent.openapi import load_source
from api_agent.policy.config import ProjectConfig
from api_agent.storage import SQLiteHistory
from check_setup_stage import identities
from stand_v2.cases import build_case, PLAN
from stand_v2.client import Owner, ROOT, SPEC, TARGET, ENVIRONMENT, PreparationError
from stand_v2.fixtures import prepare_case, public_fixture
from stand_v2.observers import observe
from stand_v2.session import application, history_fixture
from stand_v2.history_action import run_history_action


def require_same_identity(expected, actual):
    if not expected or expected != actual: raise ValueError('frozen_identity_changed')


def require_same_epoch(epoch, state, calls):
    if state.get('epoch') != epoch or any(call.get('epoch') != epoch for call in calls):
        raise PreparationError('stand_epoch_changed')


def summarize(rows):
    keys = [(r['case'], r['attempt']) for r in rows]
    if len(set(keys)) != len(keys): raise ValueError('duplicate_trial')
    if any(c not in range(1, 41) or a not in range(1, 4) for c, a in keys): raise ValueError('invalid_trial')
    ready = [r for r in rows if r['preparation'] == 'ready']
    started = [r for r in ready if r['kind'] == 'agent_goal' and r.get('run_id')]
    unstarted = [r for r in ready if r['kind'] == 'agent_goal' and not r.get('run_id')]
    evaluated = [r for r in ready if r['kind'] == 'history_action' or r.get('run_id')]
    return {'planned': 120, 'agent_planned': 117, 'history_planned': 3, 'recorded': len(rows),
        'complete': len(rows) == 120 and len(ready) == 120 and len(started) == 117,
        'agent_started': len(started), 'agent_unstarted_errors': len(unstarted),
        'unexecuted': 120 - len(rows), 'preparation_errors': sum(r['preparation'] == 'error' for r in rows),
        'evaluator_errors': sum(bool(r.get('evaluator_error')) for r in rows),
        'preparation_incidents': sum(len(r.get('preparation_incidents', [])) for r in rows),
        'task_success': sum(bool(r.get('evaluation', {}).get('task_success')) for r in evaluated),
        'agent_task_not_confirmed': sum(r['kind'] == 'agent_goal' and 'evaluation' in r
            and not r['evaluation']['task_success'] for r in started),
        'execution_errors': sum(bool(r.get('execution_error')) for r in rows),
        'history_success': sum(r['kind'] == 'history_action' and r.get('evaluation', {}).get('task_success', False) for r in ready),
        'scope': 'S14 diagnostic baseline; not final S15 or independent ACCEPT'}


def source_identity():
    result = identities()
    paths = [PLAN, SPEC, ENVIRONMENT / 'compose.override.yml', ENVIRONMENT / 'Backend.Dockerfile',
             ROOT / 'docs/testing/evaluation-and-PMI.md', ROOT / 'docs/testing/stand-v2-migration.md']
    paths += [p for p in (ENVIRONMENT / 'backend').rglob('*')
              if p.is_file() and p.suffix in {'.go', '.mod', '.sum', '.sql', '.toml'}]
    for path in paths: result[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    # Внешние исходники/конфигурация фиксируются digest, без копирования credentials.
    frontend = Path('C:/RT5-51-Prokofiev-Ilya-RIP-Frontend')
    external = {'stand-base-compose': frontend / 'docker-compose.test.yml',
        'stand-mounted-config': frontend / 'backend/config/config.toml',
        'model-runtime-executable': ROOT / 'experiments/local_model/runtime/cuda/llama-server.exe'}
    external.update({'async-source/' + path.relative_to(frontend / 'async-server').as_posix(): path
                     for path in (frontend / 'async-server').rglob('*.py')})
    for name, path in external.items(): result['external/' + name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def model_snapshot(root):
    with DesktopStore(root / 'ui.sqlite3') as store:
        record = store.save_model('local', {'name': 'Qwen3.5-4B', 'kind': 'server',
            'endpoint': os.environ.get('WORKSPACE_TOKENIZER_URL', 'http://127.0.0.1:18081'),
            'context_tokens': 8192, 'output_tokens': 1024})
        manager = ModelManager()
        try: return manager.prepare(record)
        finally: manager.close()


def prepare_verified(owner, number, label, incidents=None):
    """До Run: максимум3 свежих reset; каждый отказ сохранён, goal не повторяется."""
    incidents = [] if incidents is None else incidents
    for index in range(3):
        try: return prepare_case(owner, number, label), incidents
        except PreparationError as error:
            incidents.append({'preparation_attempt': index + 1, 'error': str(error),
                              'diagnostic': getattr(error, 'diagnostic', None)})
            if str(error) not in {'stand_control_failed', 'stand_sql_failed'} or index == 2: raise


def prepare(owner, root, number, attempt, snapshot, incidents):
    fixture, _ = prepare_verified(owner, number, f'{root.name}_{number:02}_{attempt}', incidents)
    item = build_case(number, fixture); project = f's14-{number:02}-{attempt}'
    policy = ProjectConfig.create(project, TARGET, load_source(SPEC, target=TARGET),
        mode=item['policy_mode'], resettable=True, operations=item['operations'])
    policy.save(root / 'policy.json')
    return fixture, item, policy


def one(owner, directory, number, attempt, snapshot, *, readiness=False):
    directory.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    record = {'case': number, 'attempt': attempt, 'kind': 'history_action' if number == 35 else 'agent_goal',
              'preparation': 'error', 'preparation_incidents': [], 'directory': directory.relative_to(ROOT).as_posix()}
    try:
        fixture, item, policy = prepare(owner, directory, number, attempt, snapshot, record['preparation_incidents'])
        with SQLiteHistory(directory / 'history.sqlite3') as history:
            app = application(load_source(SPEC, target=TARGET), policy, history, snapshot, fixture, number)
            try:
                seed = None
                if number in {34, 35}:
                    seed = history_fixture(app, fixture, number)
                    owner.save(directory / 'history-fixture-run.json', seed)
                    fixture['journal_offset'] = owner.control('GET', '/journal')['total']
                    item = build_case(number, fixture)
                owner.save(directory / 'fixture.json', public_fixture(fixture))
                owner.save(directory / 'configuration.json', item)
                if number != 35:
                    preview = app.preview(item['document']); owner.save(directory / 'preview.json', preview)
                record['preparation'] = 'ready'
                if readiness:
                    record.update(executable_preview=True, capability_notes=item['capability_notes'])
                    return record
                try:
                    run = (run_history_action(history, policy.snapshot['project_id'], seed,
                        {'target': TARGET, 'spec_path': str(SPEC), 'policy_path': str(directory / 'policy.json'),
                         'history_path': str(directory / 'history.sqlite3'), 'image': os.environ['WORKSPACE_IMAGE']}, directory) if number == 35 else
                        app.start(policy.snapshot['project_id'], 'diagnostic', 'goal', item['document'],
                            LocalModel(snapshot['endpoint']), accepted_hash=preview['preview_hash']))
                except Exception as error:
                    run = history.lookup(policy.snapshot['project_id'], 'diagnostic', 'goal')
                    if run is None:
                        record.update(preparation='error', preparation_error='agent_start_failed',
                            preparation_diagnostic={'phase': 'before_run', 'error_type': type(error).__name__,
                                'frames': [{'file': Path(frame.filename).name, 'line': frame.lineno,
                                            'function': frame.name}
                                           for frame in traceback.extract_tb(error.__traceback__)]})
                        run = {'status': 'preparation_error', 'stop_reason': 'agent_start_failed'}
                    else:
                        record['execution_error'] = type(error).__name__
                owner.save(directory / 'run.json', run)
                journal = owner.control('GET', '/journal')
                owner.save(directory / 'journal-full.json', journal)
                if journal.get('dropped'): raise PreparationError('server_journal_dropped')
                calls = [r for r in journal['requests'] if r['sequence'] > fixture['journal_offset']]
                owner.save(directory / 'journal.json', calls)
                state = owner.state(); owner.save(directory / 'stand-state.json', state)
                require_same_epoch(fixture['epoch'], state, calls)
                after = owner.snapshot()
                require_same_epoch(fixture['epoch'], owner.state(), calls)
                if number == 10 and record['preparation'] == 'ready':
                    validation = {}
                    for name, password in [('old', fixture['password']), ('new', fixture['new_password'])]:
                        status, _ = owner._request('POST', '/api/users/login', body={'login': fixture['login'], 'password': password})
                        validation[name] = status
                    after['password_validation'] = validation
                owner.save(directory / 'after.json', after); owner.save(directory / 'journal.json', calls)
                if record['preparation'] == 'ready':
                    try: record['evaluation'] = observe(number, fixture, after, run, calls)
                    except Exception as error: record['evaluator_error'] = type(error).__name__
                record.update(run_id=run.get('run_id'), status=run.get('status'), stop_reason=run.get('stop_reason'),
                              checks=(run.get('report') or {}).get('checks_verdict'), model_calls=run.get('model_calls', 0),
                              http=run.get('requests', 0), resources=run.get('resources'))
            finally: app.human.close()
    except Exception as error:
        record['preparation_error' if record['preparation'] != 'ready' else 'evaluator_error'] = (
            str(error) if isinstance(error, ValueError) else type(error).__name__)
    finally:
        record['seconds'] = time.monotonic() - started
        owner.save(directory / 'result.json', record)
    return record


def execute(root, *, readiness=False):
    root.mkdir(parents=True, exist_ok=False); owner = Owner(); before = source_identity()
    snapshot = model_snapshot(root)
    images = owner.images()
    owner.save(root / 'source-manifest.json', before); owner.save(root / 'model.json', snapshot)
    with zipfile.ZipFile(root / 'source-snapshot.zip', 'x', zipfile.ZIP_DEFLATED) as archive:
        for relative in before:
            if not relative.startswith('external/'): archive.write(ROOT / relative, relative)
    freeze = {'schema_version': 'stand40-v2-s14-diagnostic-v1', 'source_manifest_sha256':
        hashlib.sha256(json.dumps(before, sort_keys=True).encode()).hexdigest(), 'model': snapshot,
        'worker_image': os.environ['WORKSPACE_IMAGE'], 'stand_images': images, 'target': TARGET, 'case_count': 40,
        'repeats': 1 if readiness else 3, 'agent_attempts': 39 if readiness else 117,
        'history_actions': 1 if readiness else 3, 'planned_source': 'docs/evals/stand-tasks-v2.plan.ru.json',
        'executable_mapping': 'scripts/stand_v2/cases.py', 'fixtures': 'scripts/stand_v2/fixtures.py',
        'oracle': 'scripts/stand_v2/observers.py', 'budgets': 'CatalogLimits defaults; context8192/output1024',
        'independent_accept': False, 'historical_outcomes_regraded': False}
    owner.save(root / 'freeze.json', freeze)
    rows = []
    for attempt in range(1, 2 if readiness else 4):
        for number in range(1, 41):
            require_same_identity(before, source_identity())
            if not readiness and number != 35:
                manager = ModelManager()
                try: manager.prepare(snapshot['profile'], expected=snapshot)
                finally: manager.close()
            row = one(owner, root / f'{number:02}-{attempt}', number, attempt, snapshot, readiness=readiness)
            rows.append(row); owner.save(root / 'rows.json', rows)
            summary = ({'case_count': len(rows), 'ready': len(rows) == 40 and all(r['preparation'] == 'ready' for r in rows),
                        'preparation_errors': [r for r in rows if r['preparation'] != 'ready']} if readiness else summarize(rows))
            owner.save(root / 'summary.json', summary)
            print(json.dumps({k: row.get(k) for k in ('case', 'attempt', 'preparation', 'status', 'stop_reason', 'evaluation', 'preparation_error', 'evaluator_error', 'seconds')}, ensure_ascii=False), flush=True)
            if row['preparation'] != 'ready' or row.get('evaluator_error'): return 3
    require_same_identity(before, source_identity())
    require_same_identity(images, owner.images())
    manager = ModelManager()
    try: manager.prepare(snapshot['profile'], expected=snapshot)
    finally: manager.close()
    return 0 if (summary['ready'] if readiness else summary['complete'] and not summary['evaluator_errors']) else 1


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--readiness', action='store_true'); parser.add_argument('--output')
    args = parser.parse_args()
    root = Path(args.output or ROOT / 'evidence/S14' / ('readiness-' if args.readiness else 'diagnostic-') / uuid.uuid4().hex[:8]).resolve()
    return execute(root, readiness=args.readiness)


if __name__ == '__main__': raise SystemExit(main())

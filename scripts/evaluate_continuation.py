"""S11 live: LocalModel продолжает подтверждённое создание без Python replay."""
from pathlib import Path
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent.model import LocalModel
from api_agent.goals.checks.compiler import AcceptedGoal
from api_agent.openapi.model import canonical
from api_agent.storage import SQLiteHistory
from api_agent.workspace.execution.broker import WorkspaceBroker
from recovery_support import RecoveryServer, application
from selected_support import card, configuration


def identity():
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / 'src').rglob('*') if p.is_file() and p.suffix in {'.py', '.sql'}}


def outcomes(mode, run, journal, devices, created, immutable, parent, health_requests):
    """State/text/checklist outcome отдельно от честного технического завершения."""
    checks = run['report']['checks_verdict']
    task = (run['status'] == 'completed' and devices[created]['value'] == 80
        and set(devices) == {42, created} and devices[42]['value'] == 57
        and any(method == 'GET' and path == '/devices/' + str(created) for method, path, _ in journal[1:])
        and checks == ('PASS' if mode == 'testing' else 'N/A'))
    answer = (run.get('final') or {}).get('summary', '')
    task = task and str(created) in answer and '80' in answer
    technical = (immutable and run.get('parent_run_id') == parent and health_requests == 1
        and run['checkpoint']['ledger']['http'] == len(journal)
        and checks in ({'N/A'} if mode == 'research' else {'PASS', 'FAIL', 'UNKNOWN'}))
    return task, technical


def main(output):
    output = Path(output); output.mkdir(parents=True, exist_ok=False)
    source = identity(); records = []
    for mode in ('testing', 'research'):
        directory = output / mode; directory.mkdir()
        with RecoveryServer() as server, SQLiteHistory(directory / 'history.sqlite3') as history:
            app = application(history, server)
            model = LocalModel('http://127.0.0.1:18081'); app.token_counter = model.count_tokens
            document = configuration([card()] if mode == 'testing' else [], mode=mode,
                goal='Создай новый прибор со значением57, затем у этого же прибора установи значение80. '
                     'Прочитай его и сообщи номер и фактическое значение. Другие приборы не изменяй.')
            preview = app.preview(document); snapshot = preview['configuration']
            snapshot['acceptance'] = {'status': 'accepted', 'preview_hash': preview['preview_hash']}
            # Fixture воспроизводит прерывание после подтверждённого создания.
            with WorkspaceBroker(app._workflow(AcceptedGoal(canonical(snapshot)), {}), history,
                    artifact=app.workspace.artifact, session_id='s', message_id='origin',
                    limits=app.limits, environment='default') as broker:
                app.workspace.artifact.bind(broker.dispatch).createDevice(body={'value': 57})
                broker.stop('interrupted')
            old = broker.safe_run(); old_hash = hashlib.sha256(canonical(old)).hexdigest()
            created = old['exchanges'][0]['response']['json']['id']
            continuation = app.preview_continuation('p', 's', old['run_id'])
            (directory / 'accepted-preview.json').write_text(json.dumps(continuation, ensure_ascii=False, indent=2), encoding='utf-8')
            new = app.continue_run('p', 's', old['run_id'], session_id='next', message_id='continue',
                model=model, accepted_hash=continuation['preview_hash'])
            immutable = hashlib.sha256(canonical(history.read_run('p', 's', old['run_id']))).hexdigest() == old_hash
            task, technical = outcomes(mode, new, server.journal, server.devices, created, immutable, old['run_id'], server.health_requests)
            record = {'mode': mode, 'task_success': task, 'technical_passing': technical,
                'status': new['status'], 'stop_reason': new['stop_reason'], 'checks': new['report']['checks_verdict'],
                'health_requests': server.health_requests, 'setup_http': 1, 'agent_http': len(new['exchanges']),
                'origin_immutable': immutable, 'ledger': new['checkpoint']['ledger']}
            for name, value in [('origin.json', old), ('run.json', new),
                    ('observer.json', {'record': record, 'journal': server.journal, 'devices': server.devices})]:
                (directory / name).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
            records.append(record); print(json.dumps(record, ensure_ascii=False), flush=True)
    summary = {'records': records, 'source_hashes': source, 'source_unchanged': identity() == source,
        'observer_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'task_successes': sum(r['task_success'] for r in records),
        'technical_passing': all(r['technical_passing'] for r in records),
        'scope': 'Two live linked continuations, not full STAND40 or model reliability qualification'}
    (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if summary['technical_passing'] and summary['source_unchanged'] else 1


if __name__ == '__main__': raise SystemExit(main(sys.argv[1]))

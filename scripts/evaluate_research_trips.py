"""Три адресных случая STAND-v2 на выделенной копии, с независимым SQL observer."""
import http.client
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

from evaluate_context import ROOT, identities, save
from api_agent.agent.model import LocalModel
from api_agent.agent.runtime import WorkspaceOptions
from api_agent.goals.entry.manual_application import ManualApplication
from api_agent.openapi import load_source
from api_agent.policy.config import ProjectConfig
from api_agent.storage import SQLiteHistory
from api_agent.workspace.sdk import build_sdk


PORT = 18182
TARGET = f'http://127.0.0.1:{PORT}'
DB = 'vkr-s13-v2-postgres-1'
PLAN = ROOT / 'docs/evals/stand-tasks-v2.plan.ru.json'
SPEC = ROOT / 'experiments/stand-v2/backend/docs/openapi.json'
CITY = 'Экономичная городская поездка'
COUNTRY = 'Загородная трасса'
AC = 'Кондиционер'
COMPOSITION = {38: [], 39: [[CITY, 75], [AC, 1.5]], 40: [[COUNTRY, 100], [AC, 1]]}
READS = ('get_api_trips_trip_id', 'get_api_trips_scenarioscart', 'get_api_scenarios', 'get_api_scenarios_id')
LOGIN = 'post_api_users_login'
NUMBER = r'[0-9]+(?:[.,][0-9]+)?'
TRIP_ANSWER = re.compile(
    r'(?:черновик заявки (?:успешно )?создан\. )?'
    r'(?:номер заявки:\s*|заявка\s*№\s*)([0-9]+),\s*'
    r'статус:\s*(?:черновик|«черновик»|"черновик"),\s*'
    rf'(?:начальная )?энергия:\s*({NUMBER})\s*квт[·⋅* ]?ч', re.I)
NOT_SUBMITTED = 'заявка не отправлена на рассмотрение'
UNITS = {CITY: r'км|километр(?:а|ов)?', COUNTRY: r'км|километр(?:а|ов)?', AC: r'час(?:а|ов)?|ч'}


def request(method, path, *, body=None, token=None, expected=200):
    connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=10)
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        connection.request(method, path, json.dumps(body).encode() if body is not None else None, headers)
        response = connection.getresponse()
        if response.status != expected:
            raise RuntimeError(f'fixture HTTP {method} {path}: {response.status}, expected {expected}')
        return json.loads(response.read())
    finally:
        connection.close()


def observe(user_id):
    if type(user_id) is not int or user_id <= 0:
        raise ValueError('fixture_actor')
    query = f"""SELECT COALESCE(json_agg(json_build_object(
        'id', t.id, 'charge', t.start_charge, 'status', t.status,
        'submitted', t.submitted_at IS NOT NULL, 'rows',
        COALESCE((SELECT json_agg(json_build_array(d.name, s.duration) ORDER BY d.name)
        FROM trip_scenarios s JOIN driving_scenarios d ON d.id = s.driving_scenario_id
        WHERE s.trip_application_id = t.id), '[]')) ORDER BY t.id), '[]')
        FROM trip_applications t WHERE t.creator_id = {user_id};"""
    result = subprocess.run(['docker', 'exec', '-i', DB, 'sh', '-c',
        'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1'],
        input=query.encode(), capture_output=True, check=True, timeout=20)
    return json.loads(result.stdout)


def scenario_id(name):
    results = request('GET', '/api/scenarios')
    identifiers = [item['id'] for item in results if item['name'].casefold() == name.casefold()]
    if len(identifiers) != 1:
        raise ValueError('fixture_scenario_ambiguous')
    return identifiers[0]


def state_ok(case, rows, prior_id=None):
    if len(rows) != 1:
        return False
    trip = rows[0]
    return (trip['charge'] == (80 if case == 39 else 57) and trip['status'] == 'черновик'
        and not trip['submitted'] and sorted(trip['rows'], key=lambda row: row[0]) == sorted(COMPOSITION[case])
        and (case != 39 or trip['id'] == prior_id))


def answer_ok(case, summary, trip_id):
    """Подтвердить только однозначный формат. Остальной текст требует сверки."""
    summary = ' '.join(summary.casefold().split()).removesuffix('.')
    claim = TRIP_ANSWER.match(summary)
    charge = 80 if case == 39 else 57
    if not claim or int(claim[1]) != trip_id or Decimal(claim[2].replace(',', '.')) != charge:
        return False
    rest = summary[claim.end():].removesuffix('. ' + NOT_SUBMITTED)
    if not COMPOSITION[case]:
        return not rest
    if not rest.startswith('. состав: '):
        return False
    rows = rest.removeprefix('. состав: ').split(';')
    expected = COMPOSITION[case]
    if len(rows) != len(expected):
        return False
    for name, quantity in expected:
        amount = rf'{NUMBER}|один' if name == AC else NUMBER
        pattern = rf'{re.escape(name.casefold())}\s*(?:[:—–-])\s*({amount})\s*(?:{UNITS[name]})'
        matching = [match for row in rows if (match := re.fullmatch(pattern, row.strip()))]
        if len(matching) != 1:
            return False
        value = matching[0][1]
        if (1 if value == 'один' else Decimal(value.replace(',', '.'))) != Decimal(str(quantity)):
            return False
    return True


def run_case(directory, case, goal):
    directory.mkdir()
    credentials = {'login': 's13_' + secrets.token_hex(6), 'password': secrets.token_urlsafe(18)}
    user_id = request('POST', '/api/users/register', body=credentials, expected=201)['id']
    prior_id = None
    if case == 39:
        token = request('POST', '/api/users/login', body=credentials)['token']
        request('POST', '/api/trips', body={'start_charge': 57}, token=token, expected=201)
        for name, duration in COMPOSITION[case]:
            request('POST', f'/api/scenarios/{scenario_id(name)}/add-to-trip', body={'duration': duration}, token=token)
        del token
        seeded = observe(user_id)
        if (len(seeded) != 1 or seeded[0]['charge'] != 57 or seeded[0]['status'] != 'черновик'
                or sorted(seeded[0]['rows']) != sorted(COMPOSITION[case])):
            raise ValueError('fixture_draft_mismatch')
        prior_id = seeded[0]['id']
        goal = goal.replace('{{номер_заявки}}', str(prior_id))
    before = observe(user_id)
    if (case in (38, 40) and before) or (case == 40 and scenario_id(COUNTRY) == scenario_id(AC)):
        raise ValueError('fixture_precondition')
    catalog = load_source(SPEC, target=TARGET)
    operations = {name: 'A' for name in READS}
    operations[LOGIN] = 'C'
    operations['put_api_trips_trip_id' if case == 39 else 'post_api_trips'] = 'C'
    if case == 40:
        operations['post_api_scenarios_id_add-to-trip'] = 'C'
    project = f's13-stand-{case}'
    config = ProjectConfig.create(project, TARGET, catalog, mode='TEST', resettable=True, operations=operations)
    document = {'schema_version': 'selected-checks-v1', 'goal': goal, 'mode': 'research', 'cards': []}
    model = LocalModel(os.environ['WORKSPACE_TOKENIZER_URL'])
    with SQLiteHistory(directory / 'history.sqlite3') as history:
        options = WorkspaceOptions(build_sdk(catalog, config), os.environ['WORKSPACE_IMAGE'])
        app = ManualApplication(catalog, config, history=history, workspace=options, token_counter=model.count_tokens,
            mappings={'auth': {'login_operation': LOGIN, 'token_path': '/token', 'body': credentials,
                'public_body_fields': ['login']},
                'extraction': {LOGIN: {'/token': {'type': 'string', 'secret': True}}}})
        preview = app.preview(document)
        save(directory / 'input.json', {'document': document, 'limits': vars(app.limits),
            'accepted_hash': preview['preview_hash'], 'spec_hash': catalog.spec_hash, 'policy': operations})
        result = app.start(project, 's13', 'run', document, model, accepted_hash=preview['preview_hash'])
    after = observe(user_id)
    state = state_ok(case, after, prior_id)
    answer = state and answer_ok(case, (result.get('final') or {}).get('summary', ''), after[0]['id'])
    answer_assessment = 'confirmed' if answer else ('inconclusive' if state else 'not_evaluated')
    checks = result['report']['checks_verdict']
    record = {'case': case, 'state': state, 'answer': answer, 'answer_assessment': answer_assessment, 'checks': checks,
        'status': result['status'], 'stop_reason': result['stop_reason'], 'requests': len(result['exchanges']),
        'task_success': bool(state and answer and checks == 'N/A' and result['status'] == 'completed')}
    save(directory / 'run.json', result)
    save(directory / 'observer.json', {'before': before, 'after': after, 'verdict': record})
    return record


def main(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    if request('GET', '/health') != {'ready': True}:
        raise ValueError('stand_v2_not_ready')
    cases = {row['id']: row['goal'] for row in json.loads(PLAN.read_text(encoding='utf-8'))['cases']}
    source = identities()
    records = []
    for number in (38, 39, 40):
        case = f'STAND-{number}'
        try:
            record = run_case(output / case, number, cases[case])
        except (RuntimeError, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
            record = {'case': number, 'task_success': False, 'status': 'fixture_or_execution_error',
                'error_type': type(error).__name__}
            if re.fullmatch(r'[a-z][a-z0-9_]+', str(error)):
                record['error_code'] = str(error)
        records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
    unchanged = identities() == source
    save(output / 'summary.json', {'records': records, 'source_unchanged': unchanged, 'source_and_tests': source})
    return 0 if unchanged and all(record['task_success'] for record in records) else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))

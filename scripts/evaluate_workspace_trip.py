"""Опорный комплексный pilot: live generator/model, HTTP стенда и отдельный oracle.

Это испытательная конфигурация, не бизнес-адаптер приложения и не полный STAND-37.
Тестовый пароль из исходной цели извлекает коллектор; в модель/диск идёт placeholder.
"""
import argparse
import ast
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import secrets
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from scripts.evaluate_stand import load_module
from api_agent.agent.catalog import CatalogLimits
from api_agent.agent.model import LocalModel
from api_agent.goals.application import GoalApplication
from api_agent.goals.interaction import HumanInteraction
from api_agent.openapi import load_source
from api_agent.policy import ProjectConfig
from api_agent.storage import SQLiteHistory
from api_agent.workspace.runtime import WorkspaceOptions
from api_agent.workspace.sdk import build_sdk

OPERATIONS = {'post_api_users_register': 'C', 'post_api_users_login': 'C', 'get_api_scenarios': 'A',
    'post_api_scenarios_id_add-to-trip': 'C', 'get_api_trips_scenarioscart': 'A',
    'put_api_trips_trip_id': 'C', 'put_api_trips_trip_id_scenarios_scenario_id': 'C',
    'put_api_trips_trip_id_submittrip': 'C', 'get_api_trips_trip_id': 'A'}


class DiagnosticModel(LocalModel):
    """Сохраняет только классификацию кода: без строк, переменных и credentials."""
    def __init__(self, operations):
        super().__init__('http://127.0.0.1:18081')
        self.operations, self.outlines = operations, []

    def __call__(self, payload):
        response = super().__call__(payload)
        for call in response['choices'][0]['message'].get('tool_calls', []):
            if call['function']['name'] != 'execute_code': continue
            try:
                tree = ast.parse(json.loads(call['function']['arguments'])['code'])
                calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
                self.outlines.append({'definitions': sum(isinstance(n, (ast.FunctionDef, ast.ClassDef)) for n in ast.walk(tree)),
                    'api_calls': [self.operations[n.func.attr] for n in calls if isinstance(n.func, ast.Attribute)
                        and isinstance(n.func.value, ast.Name) and n.func.value.id == 'api' and n.func.attr in self.operations],
                    'prints': sum(isinstance(n.func, ast.Name) and n.func.id == 'print' for n in calls)})
            except (KeyError, ValueError, SyntaxError): self.outlines.append({'parse_error': True})
        return response


def goal_text(login, password):
    return (f'Зарегистрируй новый обычный аккаунт: логин {login}, тестовый пароль {password}; войди в него. '
        'Посмотри условия езды. Выбери по точному названию «Загородная трасса» и «Кондиционер», добавь оба в корзину. '
        'Открой корзину. Укажи начальную энергию 57 кВт·ч, для «Кондиционера» 1 час, для «Загородной трассы» 100 километров. '
        'Отправь эту заявку на рассмотрение. Затем прочитай её и покажи номер, состав и фактический статус. '
        'Ожидаю ровно две выбранные услуги с указанными длительностями, начальную энергию 57 кВт·ч и статус «сформирован». '
        'Самостоятельно завершать заявку и вычислять остаток энергии не нужно.')


def accept_proposal(proposal, login):
    """Предварительная проверка публичного смысла до каких-либо изменений."""
    slots = proposal.get('slots', [])
    names = [s.get('operation_id') for s in slots]
    if not set(OPERATIONS) <= set(names) or not set(names) <= OPERATIONS.keys(): return False
    if names.count('post_api_users_register') != 1 or names.count('post_api_scenarios_id_add-to-trip') != 2: return False
    registration = next(s for s in slots if s['operation_id'] == 'post_api_users_register')
    if registration.get('body', {}).get('login') != login: return False
    filters = {s.get('parameters', {}).get('name') for s in slots if s['operation_id'] == 'get_api_scenarios'}
    if not {'Загородная трасса', 'Кондиционер'} <= filters: return False
    updates = [s.get('body', {}) for s in slots]
    if not all(value in updates for value in ({'start_charge': 57}, {'duration': 1}, {'duration': 100})): return False
    if not any(c.get('predicate') == 'json_value_equals' and c.get('parameters') == {'path': '/status', 'value': 'сформирован'}
               for c in proposal.get('conditions', [])): return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--preparation', choices=['live', 'controlled'], default='live',
        help='controlled изолирует primary/HTTP от ошибок generator; не считается полным NL-прогоном')
    args = parser.parse_args()
    root = args.output.resolve(); root.mkdir(parents=True, exist_ok=False)
    source_files = sorted((ROOT / 'src').rglob('*.py')) + [Path(__file__), ROOT / 'scripts/stand_trip_case.py']
    def hashes(): return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files}
    identity = hashes()
    owner = load_module('trip_pilot_owner', 'C:/RT5-51-Prokofiev-Ilya-RIP-Frontend/scripts/stand.py')
    password = secrets.token_urlsafe(18)
    login = 'trip_' + uuid.uuid4().hex[:12]
    # Все данные были в исходной цели. Только её безопасная проекция выходит из collector.
    goal = goal_text(login, password).replace(password, '[пароль передан приложению]')
    catalog = load_source('C:/RT5-51-Prokofiev-Ilya-RIP-Frontend/backend/docs/openapi.json')
    config = ProjectConfig.create('trip-' + uuid.uuid4().hex, 'http://127.0.0.1:18080', catalog,
        mode='TEST', resettable=True, operations=OPERATIONS)
    artifact = build_sdk(catalog, config); artifact.save(root / 'sdk')
    # Цепочка заведомо длиннее default10: отдельный явный профиль только испытания.
    limits = CatalogLimits(max_requests=16)
    decisions = []
    def reader(request):
        kind = request['kind']
        value = password if kind == 'private_input' else 'no'
        if kind == 'acceptance': value = 'yes' if accept_proposal(request['payload']['interpretation']['proposal'], login) else 'no'
        if kind == 'approval': value = 'yes'
        decisions.append({'kind': kind, 'decision': 'supplied' if kind == 'private_input' else value})
        return {'request_id': request['id'], 'value': value}
    human = HumanInteraction(reader)
    human.private_input('test_password')
    mappings = {'auth': {'login_operation': 'post_api_users_login', 'token_path': '/token',
        'body': {'login': login, 'password': password}, 'public_body_fields': ['login']},
        'extraction': {'post_api_users_login': {'/token': {'type': 'string', 'secret': True}}},
        'private_bodies': {'post_api_users_register': {'password': password}}}
    try:
        before = owner.reset()
        model = DiagnosticModel({op['python_name']: name for name, op in artifact.manifest['operations'].items()})
        generator = model
        if args.preparation == 'controlled':
            from scripts.stand_trip_case import proposal
            prepared = proposal(catalog, config, goal, login)
            def generator(_):
                return {'choices': [{'message': {'tool_calls': [{'id': 'prepared', 'type': 'function',
                    'function': {'name': 'propose_goal', 'arguments': json.dumps(prepared)}}]}}]}
        with SQLiteHistory(root / 'history.sqlite3') as history:
            run = GoalApplication(config.snapshot['target'], catalog, config, history=history,
                generator=generator, human=human, limits=limits, token_counter=model.count_tokens, mappings=mappings,
                workspace=WorkspaceOptions(artifact, os.environ['WORKSPACE_IMAGE'])).start(
                    config.snapshot['project_id'], 's', 'trip', goal, model, force_confirmation=True)
        journal = owner.control('GET', '/journal')
        state = owner.control('GET', '/state')
        exchanges = run.get('exchanges', [])
        reads = [e for e in exchanges if e['operation_id'] == 'get_api_trips_trip_id']
        observed = None
        if reads:
            trip_id = reads[-1]['request_parameters']['trip_id']
            credentials = owner.request(owner.API_PORT, 'POST', '/api/users/login', {'login': login, 'password': password})
            # Отдельный наблюдатель не использует текст финального ответа модели как oracle.
            token = credentials['value']['token']
            response = owner.request(owner.API_PORT, 'GET', f'/api/trips/{trip_id}', token=token)
            observed = response['value']
        composition = {r['scenario']['name']: r['duration'] for r in (observed or {}).get('scenarios', [])}
        success = (run['status'] == 'completed' and run['report'].get('goal_verdict') == 'PASS'
            and observed is not None and observed['start_charge'] == 57 and observed['status'] == 'сформирован'
            and bool(observed.get('submitted_at')) and composition == {'Кондиционер': 1, 'Загородная трасса': 100}
            and len(observed['scenarios']) == 2 and state['counts']['users'] == before['counts']['users'] + 1
            and state['counts']['trips'] == before['counts']['trips'] + 1)
        result = {'goal': goal, 'status': run['status'], 'stop_reason': run['stop_reason'],
            'http': len(exchanges), 'goal_verdict': run['report'].get('goal_verdict'), 'task_success': success,
            'limits': asdict(limits), 'decisions': decisions, 'observed': observed,
            'source_unchanged': hashes() == identity, 'source_hashes': identity,
            'preparation': args.preparation,
            'code_outlines': model.outlines,
            'scope': 'single reference scenario, live primary; controlled preparation is not full NL success'}
        for filename, data in [('run.json', run), ('journal.json', journal), ('summary.json', result)]:
            text = json.dumps(data, ensure_ascii=False, indent=2)
            if password in text or owner.initialize() in text: raise ValueError('private_output_denied')
            (root / filename).write_text(text, encoding='utf-8')
        print(json.dumps({k: result[k] for k in ('status', 'stop_reason', 'http', 'goal_verdict', 'task_success')}, ensure_ascii=False))
        return 0 if success and result['source_unchanged'] else 1
    finally:
        human.close()
        owner.reset()


if __name__ == '__main__': raise SystemExit(main())

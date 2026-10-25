"""Conditions-only reference: та же frozen цель и business oracle, свободный цикл.

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

from scripts.evaluate_workspace_trip import OPERATIONS, DiagnosticModel, goal_text
from scripts.condition_trip_fixture import proposal as condition_proposal, accept_conditions


def verify_frozen_oracle():
    """Business oracle AST дословно совпадает с frozen runner; исходник не меняется."""
    import ast
    from scripts.evaluate_condition_trip_series import frozen_changes
    original = ROOT / 'scripts/evaluate_workspace_trip.py'
    def expression(path):
        tree = ast.parse(path.read_text(encoding='utf-8'))
        return next(ast.dump(node.value, include_attributes=False) for node in ast.walk(tree)
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'success'
                for target in node.targets))
    if expression(original) != expression(Path(__file__)): raise ValueError('frozen_oracle_changed')
    if frozen_changes(): raise ValueError('frozen_tests_changed')


def main():
    verify_frozen_oracle()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--preparation', choices=['live', 'controlled'], default='live',
        help='controlled изолирует primary/HTTP от ошибок generator; не считается полным NL-прогоном')
    args = parser.parse_args()
    root = args.output.resolve(); root.mkdir(parents=True, exist_ok=False)
    source_files = sorted((ROOT / 'src').rglob('*.py')) + [Path(__file__), ROOT / 'scripts/stand_trip_case.py', ROOT / 'scripts/evaluate_workspace_trip.py', ROOT / 'scripts/condition_trip_fixture.py']
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
    expected_conditions = condition_proposal(catalog, config, goal, login)
    decisions = []
    def reader(request):
        kind = request['kind']
        value = password if kind == 'private_input' else 'no'
        if kind == 'acceptance': value = 'yes' if accept_conditions(request['payload']['interpretation']['proposal'], expected_conditions) else 'no'
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
            prepared = expected_conditions
            def generator(_):
                return {'choices': [{'message': {'content': json.dumps(prepared)}}]}
            generator.supports_condition_contract = True
            generator.supports_json_schema = True
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
            'preparation': args.preparation, 'goal_contract': 'conditions-v2',
            'code_outlines': model.outlines,
            'scope': 'conditions-v2; unchanged frozen business oracle; live primary; controlled is not NL success'}
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

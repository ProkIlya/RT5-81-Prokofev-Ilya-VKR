"""Application adapter серии; fixtures/history не подменяют решения LLM."""
from copy import deepcopy
import os

from api_agent.agent.model import LocalModel
from api_agent.agent.runtime import WorkspaceOptions
from api_agent.execution.contracts import CatalogLimits
from api_agent.goals.checks.compiler import AcceptedGoal
from api_agent.goals.entry.interaction import HumanInteraction
from api_agent.goals.entry.manual_application import ManualApplication
from api_agent.openapi.model import canonical
from api_agent.setup.credentials import compile_connection
from api_agent.workspace.sdk import build_sdk, MISSING
from api_agent.workspace.execution.broker import WorkspaceBroker
from stand_v2.cases import LOGIN, LOGOUT, REGISTER, PASSWORD, READ, DETAIL, card


def mappings_for(catalog, fixture, number):
    if number in {1, 2, 3, 4, 27, 30, 32, 35, 37}: return {}, {}
    mappings = compile_connection(catalog, LOGIN, '/token',
        {'login': fixture['login'], 'password': fixture['password']})
    mappings['auth'].update(public_body_fields=['login'], logout_operations=[LOGOUT] if number in {9, 10} else [])
    passwords = {}
    if number in {7, 8}:
        mappings['private_bodies'] = {REGISTER: {'password': fixture['password']}}
        passwords = {'registration': fixture['password']}
    if number == 10:
        mappings['private_bodies'] = {PASSWORD: {'password': fixture['new_password']}}
        passwords = {'changed': fixture['new_password']}
    return mappings, passwords


def application(catalog, policy, history, snapshot, fixture, number):
    mappings, passwords = mappings_for(catalog, fixture, number)
    def human(request):
        if request['kind'] in {'approval', 'acceptance'}:
            value = 'no' if number == 28 and request['kind'] == 'approval' else 'yes'
        else: value = 'Уточните доступные варианты; выбор пока не сделан.'
        return {'request_id': request['id'], 'value': value}
    return ManualApplication(catalog, policy, history=history, mappings=mappings,
        model_test_passwords=passwords, human=HumanInteraction(human, timeout=30),
        limits=CatalogLimits(max_context_tokens=snapshot['context_tokens'], max_output_tokens=snapshot['output_tokens']),
        token_counter=LocalModel(snapshot['endpoint']).count_tokens,
        workspace=WorkspaceOptions(build_sdk(catalog, policy), os.environ['WORKSPACE_IMAGE'],
            snapshot['model_name'], canonical(snapshot)), source='user_constructor')


def history_fixture(app, fixture, number):
    """Доверенный setup выполняет реальное чтение и сохраняет честный report без LLM."""
    document = {'schema_version': 'selected-checks-v1', 'goal': 'Сохранить выбранное чтение для испытания истории.',
                'mode': 'testing', 'cards': [card('history_read', READ if number == 34 else DETAIL,
                    assertions=[{'predicate': 'schema'}])]}
    preview = app.preview(document); snapshot = deepcopy(preview['configuration'])
    snapshot['acceptance'] = {'status': 'accepted', 'preview_hash': preview['preview_hash']}
    workflow = app._workflow(AcceptedGoal(canonical(snapshot)), {})
    with WorkspaceBroker(workflow, app.history, artifact=app.workspace.artifact,
                         session_id='fixture', message_id='fixture', limits=app.limits,
                         environment='default') as broker:
        api = app.workspace.artifact.bind(broker.dispatch)
        if number == 34:
            # Host fixture использует штатное private-body admission, без body
            # модели; исходная SDK signature требует явный sentinel пропуска.
            getattr(api, LOGIN)(body=MISSING)
            getattr(api, READ)(path__trip_id=fixture['trip_id'])
        else: getattr(api, DETAIL)(path__id=4)
        api.finish('Сохранено фактическое чтение для открытия истории.', claims=[], answer_fields={})
    run = broker.safe_run()
    if run['status'] != 'completed': raise ValueError('history_fixture_not_completed')
    if number == 34:
        event = next(e for e in run['exchanges'] if e['operation_id'] == READ)
        fixture['attachment'] = app.history.select_attachment(app.config.snapshot['project_id'], 'fixture',
            run['run_id'], event['id'], fields=['/id'])
    return run



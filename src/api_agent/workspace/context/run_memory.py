"""Безопасное текущее состояние одного Run; не долговременные Project facts."""
from copy import deepcopy

from api_agent.agent.response_views import response_page
from api_agent.goals.checks.compiler import check_goal
from api_agent.openapi.model import canonical
from api_agent.storage.checkpoints import retained_scalars


def _page(value, max_bytes):
    """Из большого ответа переносим ограниченный набор публичных полей."""
    page = response_page(value, max_bytes=max_bytes)
    if page['complete']:
        return {'value': page['value'], 'complete': True}
    scalars = retained_scalars(value)
    paths = sorted(scalars, key=_field_order)[:4]
    return {'fields': [{'path': path, 'value': scalars[path]} for path in paths], 'complete': False}


def _field_order(path):
    name = path.rsplit('/', 1)[-1]
    locator = name.lower() in {'id', 'uuid'} or name.endswith(('Id', 'ID', '_id', '_uuid'))
    return path not in {'/id', '/uuid'}, not locator, path.count('/'), path


def project_recovery(memory):
    """Компактное представление уже проверенного checkpoint для текущей модели."""
    projected = deepcopy(memory)
    observations = projected.get('observations', [])
    for observation in observations:
        body = observation.pop('body', None)
        scalars = observation.pop('retained_scalars', None)
        if scalars is None:
            # В старых наблюдениях null без complete мог означать отсутствие тела.
            scalars = retained_scalars(body) if observation['complete'] else {}
        paths = sorted(scalars, key=_field_order)
        source = observation['source']
        observation['fields'] = [{'path': path, 'value': scalars[path],
            'source': {**source, 'path': path}} for path in paths[:6]]
        view = response_page(body, max_bytes=512)
        observation['complete'] = observation['complete'] and view['complete']
        if observation['complete']:
            observation['body'] = view['value']
        observation['fields_omitted'] = len(paths) - len(observation['fields'])
        observation.pop('omitted_paths', None)
        observation.pop('next_offset', None)
    if observations:
        projected['observation_use'] = ('These saved response fields are historical observations. '
            'Use their source and path to identify the same object. Establish missing or current values '
            'with a permitted read when required by the goal. Report what remains unknown. '
            'Never repeat a completed or uncertain write to recover a response.')
    return projected


def _deduplicate_reads(calls, operations):
    """Равная публичная проекция чтения видна один раз в пределах auth scope."""
    seen = {}
    for call in reversed(calls):
        operation = call['operation_id']
        if (call['has_error'] or 'response' not in call
                or operations[operation]['method'] not in {'GET', 'HEAD'}):
            continue
        identity = canonical([operation, call['status'], call['parameters'],
            call['request_principal'], call['response']])
        if identity in seen:
            call['response_projection_ref'] = seen[identity]
            del call['response']
        else:
            seen[identity] = call['exchange_id']


def memory_snapshot(workflow, run, session, safe):
    """Приложение сообщает реальные scopes и handles; ничего не исполняет."""
    validations = {v['exchange_id']:v for v in run.get('validations', [])}
    calls = []
    for exchange in run['exchanges']:
        response = exchange.get('response') or {}
        request = exchange.get('request') or {}
        validation = {k:v for k,v in validations.get(exchange['id'], {}).items()
            if k in {'contract_verdict','intent_verdict','task_postcondition'}}
        validated = (validation.get('contract_verdict')=='pass'
            and validation.get('intent_verdict')=='pass' and validation.get('task_postcondition') is True)
        status = response.get('status')
        has_error = (exchange['outcome']!='received' or exchange.get('truncated',False)
            or type(status) is not int or status>=400
            or any(validation.get(k)=='fail' for k in ('contract_verdict','intent_verdict')))
        item = {'exchange_id':exchange['id'], 'operation_id':exchange['operation_id'],
            'parameters':deepcopy(exchange.get('request_parameters', {})),
            'request_principal':deepcopy(exchange.get('request_principal', {})),
            'status':status, 'has_error':bool(has_error), 'validation_passed':validated}
        if has_error or not validated:
            item.update(outcome=exchange['outcome'],validation=validation)
        if 'json' in request:
            item['request_body'] = _page(session.state.redact(request['json']), 256)
        if 'json' in response:
            item['response'] = _page(session.state.redact(response['json']), 512)
            if exchange.get('truncated'):
                item['response']['complete'] = False
        calls.append(item)
    _deduplicate_reads(calls, workflow.catalog.operations)
    check = check_goal(workflow.accepted, workflow.catalog, run)
    if 'conditions' in check:
        check['conditions'] = [{k:c[k] for k in
            (('condition_id','verdict') if c['verdict']=='PASS'
             else ('condition_id','verdict','expected','actual'))} for c in check['conditions']]
    errors = []
    for step in run.get('steps', []):
        observation = step.get('observation') or {}
        if observation.get('error') or observation.get('status') == 'error':
            errors.append({'step':step['index'], 'error':response_page(observation, max_bytes=256)})
    return safe({'auth_session':{'authenticated':session.state.principal is not None,
        'principal':session.state.principal, 'auth_epoch':session.state.auth_epoch},
        'runtime_values':session.state.public(), 'api_calls':calls, 'tool_errors':errors,
        'check':check, 'uncertain_write':bool(session.uncertain),
        'pending_mutation':bool(run.get('pending_mutation')),
        'resources':deepcopy(run.get('resources', {})),
        'response_read_hint':'Partial fields and response_projection_ref describe public projections, not full bodies. '
            'Inspect complete responses in assigned Python variables using execute_code. '
            'Exchange IDs identify host evidence. Keep completed writes; do not repeat them to recover output.'})

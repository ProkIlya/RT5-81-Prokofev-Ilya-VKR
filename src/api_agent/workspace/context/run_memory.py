"""Безопасное текущее состояние одного Run; не долговременные Project facts."""
from copy import deepcopy
from collections import defaultdict

from api_agent.agent.response_views import named_rows, response_page
from api_agent.goals.checks.compiler import check_goal
from api_agent.storage.checkpoints import retained_scalars
from api_agent.validation import fingerprint


def _page(value, max_bytes, *, preserve_named_rows=False):
    """Из большого ответа переносим ограниченный набор публичных полей."""
    page = response_page(value, max_bytes=max_bytes)
    if page['complete']:
        return {'value': page['value'], 'complete': True}
    if preserve_named_rows and (rows := named_rows(value, max_bytes=max_bytes)) is not None:
        return {'named_rows': rows, 'complete': False}
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


def read_request_identity(exchange):
    return fingerprint([exchange['operation_id'], exchange.get('request'),
                        exchange.get('request_parameters'), exchange.get('request_principal')])


def _deduplicate_reads(calls, identities):
    """Связывать только равные ответы, даже если проекции разных тел совпали."""
    seen = {}
    for call in reversed(calls):
        identity = identities.get(call['exchange_id'])
        if identity is None:
            continue
        if identity in seen:
            call['response_projection_ref'] = seen[identity]
            del call['response']
        else:
            seen[identity] = call['exchange_id']


def _project_principals(calls):
    groups = defaultdict(list)
    for call in calls:
        groups[fingerprint(call['request_principal'])].append(call)
    principals = {}
    for group in groups.values():
        if len(group) < 3:
            continue
        reference = len(principals) + 1
        principals[str(reference)] = group[0]['request_principal']
        for call in group:
            call.pop('request_principal')
            call['principal_ref'] = reference
    return {'request_principals': principals} if principals else {}


def tighten_working_memory(memory):
    """Плотная проекция под жёсткий предел окна. Сохраняет запросы, ошибки и источники."""
    result = deepcopy(memory)
    defaults = {'contract_verdict': 'pass', 'intent_verdict': 'not_checked', 'task_postcondition': False}
    for call in result.get('api_calls', []):
        if call.get('has_error'):
            continue
        validation = {name: value for name, value in call.get('validation', {}).items()
                      if name not in defaults or defaults[name] != value}
        if 'validation' in call:
            if validation:
                call['validation'] = validation
            else:
                call.pop('validation')
        if call.get('outcome') == 'received':
            call.pop('outcome')
        response = call.get('response') or {}
        if response.get('complete') and 'value' in response:
            call['response'] = _page(response['value'], 512, preserve_named_rows=True)
    return result


def memory_snapshot(workflow, run, session, safe):
    """Приложение сообщает реальные scopes и handles; ничего не исполняет."""
    validations = {v['exchange_id']:v for v in run.get('validations', [])}
    calls = []
    identities = {}
    exchanges = run['exchanges']
    for index, exchange in enumerate(exchanges):
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
            is_read = (not has_error and response.get('json_valid')
                       and workflow.catalog.operations[exchange['operation_id']]['method'] in {'GET', 'HEAD'})
            if is_read:
                identities[exchange['id']] = fingerprint([read_request_identity(exchange), status, response['json']])
            # При сжатии последняя небольшая выборка остаётся доступна целиком.
            limit = 2048 if is_read and index == len(exchanges) - 1 else 512
            item['response'] = _page(session.state.redact(response['json']), limit, preserve_named_rows=is_read)
            if exchange.get('truncated'):
                item['response']['complete'] = False
        calls.append(item)
    _deduplicate_reads(calls, identities)
    check = check_goal(workflow.accepted, workflow.catalog, run)
    if 'conditions' in check:
        check['conditions'] = [{k:c[k] for k in
            (('condition_id','verdict') if c['verdict']=='PASS'
             else ('condition_id','verdict','expected','actual'))} for c in check['conditions']]
    errors = {}
    for step in run.get('steps', []):
        observation = step.get('observation') or {}
        error = observation.get('error') or ('error' if observation.get('status') == 'error' else None)
        if error:
            diagnostic = {'error': error, **{key: observation[key] for key in
                ('error_line', 'error_detail', 'preflight', 'expected_request') if observation.get(key) is not None}}
            page = response_page(diagnostic, max_bytes=512)
            entry = errors.setdefault(fingerprint(diagnostic), {'steps': [], **page['value']})
            entry['steps'].append(step['index'])
    return safe({'auth_session':{'authenticated':session.state.principal is not None,
        'principal':session.state.principal, 'auth_epoch':session.state.auth_epoch},
        'runtime_values':session.state.public(), 'api_calls':calls, 'tool_errors':list(errors.values()),
        **_project_principals(calls),
        'check':check, 'uncertain_write':bool(session.uncertain),
        'pending_mutation':bool(run.get('pending_mutation')),
        'resources':{key: value for key, value in run.get('resources', {}).items() if key != 'provider_fee'},
        'response_read_hint':'Match named_rows IDs to their source operation. Read full bodies from saved Python variables. '
            'Never replay completed writes for output.'})

"""Безопасное текущее состояние одного Run; не долговременные Project facts."""
from copy import deepcopy

from ..agent.response_views import response_page
from ..goals.condition_contract import check_conditions
from ..goals.condition_visibility import model_observation


def _page(value, max_bytes):
    """Полное малое значение не требует служебного протокола пагинации."""
    page = response_page(value, max_bytes=max_bytes)
    if page['complete']: return {'value':page['value'],'complete':True}
    return page


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
        calls.append(item)
    check = check_conditions(workflow.accepted, workflow.catalog, run)
    check = model_observation(workflow, 'check_goal', {'data':check})['data']
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
        'response_read_hint':'Inspect complete responses in assigned Python variables using execute_code. '
            'Exchange IDs identify host evidence. Keep completed writes; do not repeat them to recover output.'})

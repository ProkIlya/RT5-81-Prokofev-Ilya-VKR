"""Подсказка о документированном переходе для уже наблюдённого объекта."""
import json

from api_agent.goals.checks.condition_scope import subset


def fresh_transition_hint(broker, checklist, before):
    run = broker.run
    if run.get('uncertain_write') or any(event['kind'] == 'fresh_transition_hint' for event in run['events']):
        return ''
    operations = broker.artifact.manifest['operations']
    writes = {'POST', 'PUT', 'PATCH'}
    auth = getattr(broker.workflow, 'auth', None) or {}
    excluded = {auth.get('login_operation'), *auth.get('logout_operations', [])}
    if not any(e['operation_id'] not in excluded and operations[e['operation_id']]['method'] in writes
               and 200 <= ((e.get('response') or {}).get('status') or 0) < 300 for e in run['exchanges']):
        return ''
    fresh = {exchange['id']: exchange for exchange in run['exchanges'][before:]}
    for condition in checklist['conditions']:
        exchange = fresh.get(condition.get('exchange_id'))
        expected = condition.get('expected')
        if (not exchange or condition['reason'] != 'expectation_mismatch' or not isinstance(expected, dict)
                or not isinstance(expected.get('value'), str) or not isinstance(condition.get('actual'), str)):
            continue
        read = operations[exchange['operation_id']]
        parameters = exchange.get('request_parameters', {})
        if read['method'] != 'GET' or '{' not in read['path'] or not parameters:
            continue
        attempted = {e['operation_id'] for e in run['exchanges'] if subset(parameters, e.get('request_parameters', {}))}
        prefix = read['path'].rstrip('/') + '/'
        candidates = [{'function': 'api.' + operations[name]['python_name'], 'path': operations[name]['path'],
                       'summary': operations[name].get('summary', '')[:96]} for name in broker.workflow.allowed
                      if name not in attempted and operations[name]['method'] in writes
                      and operations[name]['path'].startswith(prefix)]
        if not 1 <= len(candidates) <= 4:
            continue
        evidence = broker.safe({'observed_parameters': parameters, 'actual': condition['actual'],
            'expected': expected, 'unused_operations': candidates})
        encoded = json.dumps(evidence, ensure_ascii=False)
        if len(encoded.encode()) <= 768:
            broker.event('fresh_transition_hint', {'condition_id': condition['condition_id']})
            return ('The fresh read shows this selected mismatch: ' + encoded + '. If the original goal asks for a '
                    'transition of this same object, inspect the listed permitted operations using its observed ID. '
                    'Keep the different objects and their requested transitions separate. ')
    return ''

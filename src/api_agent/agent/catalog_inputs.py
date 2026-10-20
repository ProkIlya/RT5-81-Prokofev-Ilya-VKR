"""Точная native schema одного действия из OpenAPI и доверенных ссылок Run.

Выбор prerequisite не исполняет HTTP. Модель явно вызывает инструмент,
исполнитель повторно проверяет права, происхождение ссылок и все аргументы.
"""
from copy import deepcopy


def runtime_values(run):
    """Читать только выданные инструментами refs, не аргументы модели."""
    values = {}
    for step in run.get('steps', []):
        for value in ((step.get('observation') or {}).get('data') or {}).get('runtime_values', []):
            values[value['ref']] = value
    return list(values.values())


def execution_schema(workflow, run):
    """Сузить одну операцию с учётом явного inspect и ещё не выполненных условий."""
    values = runtime_values(run)
    checked = {e['operation_id'] for e in run.get('exchanges', []) if e.get('outcome', 'received') == 'received'}
    inspected = [s['action']['arguments']['operation_id'] for s in run.get('steps', [])
                 if (s.get('action') or {}).get('name') == 'inspect_operation'
                 and s['action']['arguments'].get('operation_id') in workflow.allowed]
    pending = [name for name in workflow.conditions if name not in checked]
    target = inspected[-1] if inspected and inspected[-1] not in checked else pending[0] if pending else None
    slot = workflow.next_occurrence(run.get('exchanges', []))
    if workflow.occurrences:
        target = slot['operation_id'] if slot else None
    retry_unfiltered = next((s['action']['arguments']['operation_id'] for s in reversed(run.get('steps', []))
        if ((s.get('observation') or {}).get('data') or {}).get('verify_unfiltered')), None)
    if retry_unfiltered:
        # Уже исполненный последующий полный запрос отменяет рекомендацию.
        latest = next((e for e in reversed(run.get('exchanges', [])) if e['operation_id'] == retry_unfiltered), None)
        if latest and '?' in latest.get('request', {}).get('url', ''):
            target = retry_unfiltered
        else:
            retry_unfiltered = None
    if target is None:
        return None

    def references(operation, path, secret, occurrence=None):
        return [v['ref'] for v in values if v['operation_id'] == operation and v['path'] == path and v['secret'] is secret
                and (occurrence is None or v.get('occurrence_id') == occurrence)
                and (not secret or 'auth_state' not in run or
                     (v.get('auth_epoch') == run['auth_state']['auth_epoch'] and
                      v.get('principal') == run['auth_state']['principal']))]

    def ready(name, seen):
        if name in seen or name not in workflow.allowed:
            return None
        seen = seen | {name}
        op = workflow.catalog.operations[name]
        if workflow.request_mode(name, slot if name == target else None) == 'authenticated':
            auth = workflow.auth
            if not auth:
                return None
            if not references(auth['login_operation'], auth['token_path'], True):
                return ready(auth['login_operation'], seen)
        for rule in workflow.binding_rules.get(name, {}).values():
            if not references(rule['source_operation'], rule['source_path'], False, rule.get('source_occurrence')):
                return ready(rule['source_operation'], seen)
        return name

    name = ready(target, set())
    if name is None:
        return None
    op = workflow.catalog.operations[name]
    properties = {'operation_id': {'type':'string', 'enum':[name]}}
    required = ['operation_id']
    if slot and name == slot['operation_id']:
        properties['occurrence_id'] = {'type': 'string', 'enum': [slot['id']]}
        required.append('occurrence_id')
    bindings = workflow.binding_rules.get(name, {})
    params = {p['name']:deepcopy(p['schema']) for p in op['parameters'] if p['name'] not in bindings and not (retry_unfiltered == name and p['in'] == 'query' and not p['required'])}
    if params:
        properties['parameters'] = {'type':'object','properties':params,'required':[p['name'] for p in op['parameters'] if p['required'] and p['name'] in params],'additionalProperties':False}
        if properties['parameters']['required']:
            required.append('parameters')
    if bindings:
        properties['bindings'] = {'type':'object','properties':{key:{'type':'string','enum':references(rule['source_operation'],rule['source_path'],False,rule.get('source_occurrence'))} for key,rule in bindings.items()},'required':list(bindings),'additionalProperties':False}
        required.append('bindings')
    private_body = workflow.auth and name == workflow.auth['login_operation']
    if 'requestBody' in op and not private_body:
        properties['body'] = deepcopy(op['requestBody']['content']['application/json']['schema'])
        if op['requestBody'].get('required'):
            required.append('body')
    if workflow.request_mode(name, slot if name == target else None) == 'authenticated':
        properties['credential_ref'] = {'type':'string','enum':references(workflow.auth['login_operation'],workflow.auth['token_path'],True)}
        required.append('credential_ref')
    return {'type':'object','properties':properties,'required':required,'additionalProperties':False}

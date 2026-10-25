"""Декларативный identity scope по saved response; не граф и не исполнитель HTTP."""
from copy import deepcopy

from ..openapi.parser import canonical, pointer_get
from ..validation import schema_issues
from .compiler import PRIVATE_NAMES, _closed, _schema, _path_schema


def public_scope(value):
    if isinstance(value, dict):
        if any(key.lower() in PRIVATE_NAMES for key in value):
            raise ValueError('goal_private_input_required')
        for item in value.values(): public_scope(item)
    elif isinstance(value, list):
        for item in value: public_scope(item)


def _compatible(source, destination):
    source_types = source['type'] if isinstance(source['type'], list) else [source['type']]
    target_types = destination['type'] if isinstance(destination['type'], list) else [destination['type']]
    scalar = {'string', 'integer', 'number', 'boolean'}
    return bool(set(source_types) & scalar) and all(t in target_types or t == 'integer' and 'number' in target_types
        for t in source_types if t != 'null')


def validate_where(catalog, allowed, operation_id, value, *, references=True):
    _closed(value, set(), {'parameters', 'body'})
    public_scope(value)
    operation = catalog.operations[operation_id]
    parameters = value.get('parameters', {})
    if not isinstance(parameters, dict): raise ValueError('goal_invalid_parameters')
    descriptors = {p['name']: p['schema'] for p in operation['parameters']}
    for name, literal in parameters.items():
        if name not in descriptors: raise ValueError('goal_invalid_parameter_type')
        if isinstance(literal, dict) and set(literal) == {'response'}:
            if not references: raise ValueError('goal_invalid_parameter_type')
            reference = literal['response']
            _closed(reference, {'operation_id', 'path'}, {'where', 'occurrence'})
            if 'occurrence' in reference and reference['occurrence'] not in ('first', 'last'):
                raise ValueError('goal_invalid_scope')
            source = reference['operation_id']
            if not isinstance(source, str) or source not in allowed or source not in catalog.operations: raise ValueError('goal_foreign_operation')
            validate_where(catalog, allowed, source, reference.get('where', {}), references=False)
            field = _path_schema(_schema(catalog, {'id': '_source', 'operation_id': source}), reference['path'])
            if not _compatible(field, descriptors[name]): raise ValueError('goal_invalid_parameter_type')
        elif schema_issues(literal, descriptors[name]):
            raise ValueError('goal_invalid_parameter_type')
    if 'body' in value:
        body = value['body']
        schema = operation.get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema', {})
        if not isinstance(body, dict): raise ValueError('goal_invalid_body')
        for key, literal in body.items():
            field = schema.get('properties', {}).get(key)
            if field is None or schema_issues(literal, field): raise ValueError('goal_invalid_body')
    return deepcopy(value)


def subset(expected, actual):
    return (isinstance(actual, dict) and all(key in actual and
        (subset(value, actual[key]) if isinstance(value, dict) else canonical(value) == canonical(actual[key]))
        for key, value in expected.items()))


def _unique_value(body, path):
    """Индекс в форме schema не доказывает единственность реального результата."""
    value = body
    for part in path[1:].split('/') if path else []:
        if isinstance(value, list) and (len(value) != 1 or part != '0'):
            raise ValueError('ambiguous_identity_array')
        value = pointer_get(value, '/' + part)
    return value


def _reference_values(reference, catalog, run):
    name = reference['operation_id']
    exchanges = run.get('exchanges', [])
    read = catalog.operations[name]['method'] in {'GET', 'HEAD', 'OPTIONS'}
    last_write = max((i for i, e in enumerate(exchanges)
        if catalog.operations.get(e.get('operation_id'), {}).get('method') not in {'GET', 'HEAD', 'OPTIONS', None}), default=-1)
    where = reference.get('where', {})
    occurrence = reference.get('occurrence')
    values = {}
    candidates = []
    for index, exchange in enumerate(exchanges):
        if exchange.get('operation_id') != name or read and occurrence is None and index <= last_write: continue
        response = exchange.get('response') or {}
        status = response.get('status')
        if (exchange.get('outcome') != 'received' or exchange.get('truncated') or not response.get('json_valid')
            or type(status) is not int or not 200 <= status < 300): continue
        schema = catalog.operations[name]['responses'].get(str(status), {}).get('content', {}).get('application/json', {}).get('schema')
        if schema is None or schema_issues(response.get('json'), schema): continue
        if not subset(where.get('parameters', {}), exchange.get('request_parameters', {})): continue
        if not subset(where.get('body', {}), exchange.get('request', {}).get('json', {})): continue
        actor, principal = exchange.get('request_principal') or {}, run.get('auth_state') or {}
        if actor.get('actor') is not None and principal.get('principal') is not None and actor['actor'] != principal['principal']:
            continue
        candidates.append(response['json'])
    if occurrence is not None:
        candidates = candidates[:1] if occurrence == 'first' else candidates[-1:]
    for body in candidates:
        try: value = _unique_value(body, reference['path'])
        except (KeyError, IndexError, TypeError, ValueError): continue
        values[canonical(value)] = value
    return list(values.values())


def resolve_where(observation, catalog, run):
    """Неизвестный/неоднозначный witness не заменяется expected value или индексом."""
    where = deepcopy(observation['where'])
    descriptors = {p['name']: p['schema'] for p in catalog.operations[observation['operation_id']]['parameters']}
    for name, literal in where.get('parameters', {}).items():
        if not isinstance(literal, dict) or set(literal) != {'response'}: continue
        values = _reference_values(literal['response'], catalog, run)
        if len(values) != 1 or schema_issues(values[0], descriptors[name]): return None
        where['parameters'][name] = deepcopy(values[0])
    return where


def validate_when(catalog, allowed, observed_operation, value):
    _closed(value, {'relation', 'operation_id'}, {'where'})
    name = value['operation_id']
    if not isinstance(name, str) or name not in allowed or name not in catalog.operations: raise ValueError('goal_foreign_operation')
    reads = {'GET', 'HEAD', 'OPTIONS'}
    if (not isinstance(value['relation'], str) or value['relation'] not in {'before', 'after'} or catalog.operations[name]['method'] in reads
        or catalog.operations[observed_operation]['method'] not in reads):
        raise ValueError('goal_invalid_scope')
    validate_where(catalog, allowed, name, value.get('where', {}))
    return deepcopy(value)


def observation_window(observation, catalog, run):
    """Окно saved evidence вокруг единственного события; ничего не исполняет."""
    exchanges = run.get('exchanges', [])
    writes = [i for i, e in enumerate(exchanges)
        if catalog.operations.get(e.get('operation_id'), {}).get('method') not in {'GET', 'HEAD', 'OPTIONS', None}]
    when = observation.get('when')
    if when is None: return (max(writes, default=-1), len(exchanges))
    where = resolve_where({'operation_id': when['operation_id'], 'where': when.get('where', {})}, catalog, run)
    if where is None: return None
    anchors = []
    for index, exchange in enumerate(exchanges):
        status = (exchange.get('response') or {}).get('status')
        if (exchange.get('operation_id') != when['operation_id']
            or exchange.get('outcome') not in {'received', 'invalid_json', 'invalid_content_type'}
            or exchange.get('truncated') or type(status) is not int or not 200 <= status < 300): continue
        if (subset(where.get('parameters', {}), exchange.get('request_parameters', {}))
            and subset(where.get('body', {}), exchange.get('request', {}).get('json', {}))): anchors.append(index)
    if len(anchors) != 1: return None
    anchor = anchors[0]
    if when['relation'] == 'before':
        return (max((i for i in writes if i < anchor), default=-1), anchor)
    return (anchor, min((i for i in writes if i > anchor), default=len(exchanges)))


def _scope_key(observation):
    where = {k: v for k, v in observation['where'].items() if v}
    when = deepcopy(observation.get('when'))
    if when is not None:
        when['where'] = {k: v for k, v in when.get('where', {}).items() if v}
    return canonical([observation['operation_id'], where, when, observation.get('auth_scope')])


def validate_comparison(condition, manifest, catalog):
    """Отдельные typed observations и create/read identity, без legacy depends_on."""
    observations = {o['id']: o for o in manifest['observations']}
    params = condition['parameters']
    left, right = observations[params['left']['slot']], observations[params['right']['slot']]
    fields = []
    for ref in (params['left'], params['right']):
        fields.append(_path_schema(_schema(catalog, {'id': ref['slot'],
            'operation_id': observations[ref['slot']]['operation_id']}, manifest['conditions']), ref['path']))
    types = [set(f['type'] if isinstance(f['type'], list) else [f['type']]) for f in fields]
    if not (types[0] & types[1] or types[0] <= {'integer', 'number'} and types[1] <= {'integer', 'number'}):
        raise ValueError('goal_invalid_condition')
    if _scope_key(left) == _scope_key(right) and params['left']['path'] == params['right']['path']:
        raise ValueError('goal_self_comparison')
    if catalog.operations[right['operation_id']]['method'] == 'POST' and catalog.operations[left['operation_id']]['method'] == 'GET':
        linked = False
        for value in left['where'].get('parameters', {}).values():
            if not isinstance(value, dict) or set(value) != {'response'}: continue
            source = value['response']
            if (source['operation_id'] == right['operation_id'] and source['path'] == params['right']['path']
                and canonical({k: v for k, v in source.get('where', {}).items() if v}) == canonical({k: v for k, v in right['where'].items() if v})):
                linked = True
        if not linked: raise ValueError('goal_identity_target_required')

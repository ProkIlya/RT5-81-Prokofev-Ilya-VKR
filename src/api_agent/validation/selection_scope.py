"""Область ожидания задаётся командой до HTTP, не выводится из языка цели."""
from copy import deepcopy
from api_agent.openapi.parser import canonical
from api_agent.validation import schema_issues
from api_agent.bounded_json import validate_tree


def prepare_scopes(catalog, conditions, supplied):
    """Принять точные параметры либо явно объявленный просмотр без query."""
    validate_tree(supplied,max_bytes=16384)
    if not isinstance(supplied, dict) or not set(supplied) <= set(conditions):
        raise ValueError('invalid_selection_scopes')
    result = {}
    for name, scope in supplied.items():
        if not isinstance(scope, dict) or set(scope) - {'mode','parameters'} or scope.get('mode') not in {'exact','unfiltered'}:
            raise ValueError('invalid_selection_scope')
        op = catalog.operations[name]
        if op['method'] != 'GET':
            raise ValueError('selection_scope_requires_read')
        params = scope.get('parameters', {})
        descriptors = {p['name']:p for p in op['parameters']}
        if not isinstance(params,dict) or not set(params) <= set(descriptors):
            raise ValueError('invalid_selection_parameters')
        if any(p['required'] and p['name'] not in params for p in op['parameters']):
            raise ValueError('selection_scope_missing_parameter')
        if any(schema_issues(value,descriptors[key]['schema']) for key,value in params.items()):
            raise ValueError('selection_scope_parameter_type')
        if scope['mode'] == 'unfiltered' and any(p['in']=='query' and (p['required'] or p['name'] in params) for p in op['parameters']):
            raise ValueError('unfiltered_scope_has_query')
        result[name] = {'mode':scope['mode'],'parameters':deepcopy(params)}
    return result


def scope_result(expectation, exchange):
    """Сравнить нормализованные реальные параметры; не доверять model args."""
    scope = expectation.get('selection_scope')
    if scope is None:
        return None
    actual = exchange.get('request_parameters')
    expected = (exchange.get('initial_request_parameters') if scope['mode']=='first_request'
                else scope['parameters'])
    return {'mode':scope['mode'],'expected_parameters':deepcopy(expected),
            'actual_parameters':deepcopy(actual),
            'applicable':actual is not None and expected is not None and canonical(actual)==canonical(expected)}


def applicable(validation):
    """Чужая выборка сохраняет contract result, но не заменяет intent result."""
    return all(c.get('selection_scope',{}).get('applicable',True) for c in validation['checks'])

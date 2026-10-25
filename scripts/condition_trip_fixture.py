"""Адаптация frozen условий опорной цели к v2, без графа и будущих ID."""
from copy import deepcopy

from scripts.stand_trip_case import proposal as legacy_proposal
from api_agent.goals.generation import _row_fields
from api_agent.openapi.parser import canonical


def _nested(row):
    result = {}
    for path, value in row.items():
        parts = [p.replace('~1', '/').replace('~0', '~') for p in path[1:].split('/')]
        cursor = result
        for part in parts[:-1]: cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = deepcopy(value)
    return result


def proposal(catalog, config, goal, login):
    legacy = legacy_proposal(catalog, config, goal, login)
    final = legacy['slots'][-1]['id']
    final_operation = legacy['slots'][-1]['operation_id']
    checks = [{'kind': 'performed', 'operation_id': 'post_api_users_register',
               'where': {'body': {'login': login}}},
              {'kind': 'performed', 'operation_id': 'post_api_users_login'},
              {'kind': 'performed', 'operation_id': 'get_api_scenarios'},
              {'kind': 'performed', 'operation_id': 'get_api_trips_scenarioscart'}]
    for condition in legacy['conditions']:
        if condition['slot'] != final or condition['predicate'] == 'status_in': continue
        params = condition['parameters']
        if condition['predicate'] == 'json_value_equals':
            checks.append({'kind': 'equals', 'operation_id': final_operation,
                           'path': params['path'], 'expected': deepcopy(params['value'])})
        elif condition['predicate'] == 'json_collection_matches':
            checks.append({'kind': 'rows', 'operation_id': final_operation,
                'path': params['path'], 'mode': params['mode'],
                'expected_rows': [_nested(row) for row in params['rows']]})
        else: raise ValueError('unsupported_frozen_fixture_condition')
    return {'checks': checks, 'answer_fields': [
        {'operation_id': final_operation, 'path': field['path']} for field in legacy['answer_fields']]}


def accept_conditions(candidate, expected):
    """До HTTP проверяет исходные ожидания; не даёт oracle feedback генератору."""
    checks = candidate.get('checks', [])
    for required in expected['checks']:
        matches = [c for c in checks if c.get('kind') == required['kind']
                   and c.get('operation_id') == required['operation_id']]
        if required['kind'] == 'performed':
            if not any(c.get('count', 1) >= required.get('count', 1)
                and c.get('where', {}) == required.get('where', {}) for c in matches): return False
        elif required['kind'] == 'equals':
            if not any(c.get('path') == required['path'] and c.get('expected') == required['expected'] for c in matches): return False
        else:
            rows = sorted([_row_fields(row) for row in required['expected_rows']], key=canonical)
            if not any(c.get('path') == required['path'] and c.get('mode') == required['mode']
                and sorted([_row_fields(row) for row in c.get('expected_rows', [])], key=canonical) == rows for c in matches): return False
    fields = {(f.get('operation_id'), f.get('path')) for f in candidate.get('answer_fields', [])}
    operation = expected['answer_fields'][0]['operation_id']
    return {(operation, path) for path in ('/id', '/scenarios', '/status')} <= fields

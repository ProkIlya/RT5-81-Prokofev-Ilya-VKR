"""Чистый адаптер historical checks, без prompt/generator/исполнения."""
from copy import deepcopy


def _wire_value(value, schema=None, *, literals=False):
    """Имена источников данных не меняют literals в полях object/array schema."""
    kind = (schema or {}).get('type')
    kinds = kind if isinstance(kind, list) else [kind]
    if 'object' in kinds and isinstance(value, dict):
        properties = schema.get('properties', {})
        return {k: _wire_value(v, properties[k], literals=literals) if k in properties else deepcopy(v) for k, v in value.items()}
    if 'array' in kinds and isinstance(value, list):
        return [_wire_value(v, schema.get('items', {}), literals=literals) for v in value]
    if isinstance(value, dict):
        if literals and set(value) == {'literal'}: return deepcopy(value['literal'])
        if set(value) == {'ask_user'}: return {'input': deepcopy(value['ask_user'])}
        if set(value) == {'response', 'path'}:
            return {'from_slot': deepcopy(value['response']), 'path': deepcopy(value['path'])}
    return deepcopy(value)


def _row_fields(value):
    """Частичный JSON record → те же literal leaves с точными JSON Pointers."""
    from api_agent.openapi.parser import pointer_part
    result = {}
    def visit(node, path):
        if isinstance(node, dict) and node:
            for key, child in node.items(): visit(child, path + '/' + pointer_part(key))
        else:
            result[path] = deepcopy(node)
    visit(value, '')
    return result


def _lower_checks(proposal):
    """Перевести явные wire checks без исправления значений или догадок."""
    if 'checks' not in proposal: return
    if 'conditions' in proposal: raise ValueError('goal_invalid_shape')
    checks = proposal.pop('checks')
    if not isinstance(checks, list): raise ValueError('goal_invalid_list')
    shapes = {'equals': ('json_value_equals', {'slot', 'path', 'expected'}),
        'status': ('status_in', {'slot', 'expected'}),
        'rounded': ('json_number_rounded', {'slot', 'path', 'expected'}),
        'within': ('json_number_within', {'slot', 'path', 'expected', 'tolerance'}),
        'rows': ('json_collection_matches', {'slot', 'path', 'expected_rows', 'mode', 'scope'}),
        'same': ('value_equal', {'slot', 'left', 'right'}), 'schema': ('response_schema', {'slot'})}
    result = []
    for check in checks:
        if not isinstance(check, dict) or not isinstance(check.get('kind'), str) or check['kind'] not in shapes:
            raise ValueError('goal_invalid_condition')
        if check['kind'] == 'rows' and 'scope' not in check: check = {**check, 'scope': 'response'}
        predicate, keys = shapes[check['kind']]
        if set(check) != keys | {'kind'}: raise ValueError('goal_invalid_shape')
        kind = check['kind']
        parameters = {k: deepcopy(check[k]) for k in keys - {'slot', 'scope'}}
        if kind == 'equals': parameters['value'] = _wire_value(parameters.pop('expected'))
        if kind == 'status':
            status = parameters.pop('expected')
            parameters['values'] = status if isinstance(status, list) else [status]
        if kind == 'rows':
            if check['scope'] != 'response': raise ValueError('goal_invalid_scope')
            rows = parameters.pop('expected_rows')
            if not isinstance(rows, list): raise ValueError('goal_invalid_collection')
            parameters['rows'] = [_row_fields(row) for row in rows]
            for slot in proposal.get('slots', []):
                if isinstance(slot, dict) and slot.get('id') == check['slot']:
                    slot.setdefault('scope', 'response')
        result.append({'slot': check['slot'], 'predicate': predicate, 'parameters': parameters})
    proposal['conditions'] = result

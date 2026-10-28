"""Закрытая классификация Python ошибок без отражения exception text/значений."""
from api_agent.goals.checks.compiler import PRIVATE_NAMES


REASONS = {'sequence_index_type', 'mapping_key_missing', 'not_subscriptable', 'not_iterable'}
HINTS = {
    'sequence_index_type': 'A JSON list requires an integer index, not a field name. Use the actual response body shape; iterate list items or select an item before reading its fields.',
    'mapping_key_missing': 'The mapping has no requested key. An SDK result is a wrapper: result["body"] contains the API JSON. Read JSON fields inside result["body"], not directly from result. Use the actual body shape and returned fields; do not invent identifiers.',
    'not_subscriptable': 'This value cannot be indexed. Check the actual response body type before accessing a field or item.',
    'not_iterable': 'This value cannot be iterated. Check the actual response body type before a loop or collection expression.',
}


def public_keys(artifact):
    """Только названия публичных JSON properties из pinned response schemas."""
    keys = {'body', 'status', 'json_valid', 'truncated', 'outcome'}
    def visit(schema):
        if not isinstance(schema, dict): return
        for key, child in schema.get('properties', {}).items():
            if key.lower() in PRIVATE_NAMES: continue
            keys.add(key); visit(child)
        visit(schema.get('items'))
    for operation in artifact.manifest['operations'].values():
        for response in operation['responses'].values():
            visit(response.get('content', {}).get('application/json', {}).get('schema'))
    return keys


def python_detail(error, keys):
    """Builtin args остаются в worker; наружу идут только enum и schema key."""
    if type(error) is KeyError:
        result = {'reason': 'mapping_key_missing'}
        if len(error.args) == 1 and type(error.args[0]) is str and error.args[0] in keys:
            result['key'] = error.args[0]
        return result
    if type(error) is not TypeError or len(error.args) != 1 or type(error.args[0]) is not str:
        return None
    message = error.args[0]
    if len(message) > 256: return None
    if message.startswith(('list indices must be integers or slices,', 'tuple indices must be integers or slices,')):
        return {'reason': 'sequence_index_type'}
    for kind in ('NoneType', 'bool', 'int', 'float'):
        if message == f"'{kind}' object is not subscriptable": return {'reason': 'not_subscriptable'}
        if message == f"'{kind}' object is not iterable": return {'reason': 'not_iterable'}
    return None


def valid_detail(value, keys):
    """IPC остаётся недоверенным; неизвестные поля/коды не допускаются."""
    return value is None or (type(value) is dict and set(value) in ({'reason'}, {'reason', 'key'})
        and type(value.get('reason')) is str and value['reason'] in REASONS
        and ('key' not in value or value['reason'] == 'mapping_key_missing'
             and type(value['key']) is str and value['key'] in keys))

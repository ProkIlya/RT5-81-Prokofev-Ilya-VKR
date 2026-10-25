"""Общие schema objects кодируются локальными $defs без изменения допустимых значений."""
from copy import deepcopy
import json


def _children(schema):
    yield from schema.get('properties', {}).values()
    for key in ('oneOf', 'anyOf', 'allOf'):
        yield from schema.get(key, [])
    if isinstance(schema.get('items'), dict): yield schema['items']


def compact_schema(schema, *, threshold=49152):
    """Intern DAG построителя; enum/const/default остаются данными, не схемами."""
    if '$defs' in schema or len(json.dumps(schema, ensure_ascii=False).encode()) <= threshold:
        return schema
    nodes, counts, active = {}, {}, set()
    def collect(node):
        identity = id(node)
        if identity in active: raise ValueError('goal_schema_cycle')
        counts[identity] = counts.get(identity, 0) + 1
        if identity in nodes: return
        nodes[identity] = node
        active.add(identity)
        for child in _children(node): collect(child)
        active.remove(identity)
    collect(schema)
    selected = {key: 'v' + str(index) for index, (key, node) in enumerate(nodes.items())
        if counts[key] > 1 and len(json.dumps(node, ensure_ascii=False).encode()) >= 40}
    if not selected: return schema
    def emit(node, *, definition=False):
        if id(node) in selected and not definition:
            return {'$ref': '#/$defs/' + selected[id(node)]}
        result = {}
        for key, value in node.items():
            if key == 'properties': result[key] = {name: emit(child) for name, child in value.items()}
            elif key in ('oneOf', 'anyOf', 'allOf'): result[key] = [emit(child) for child in value]
            elif key == 'items' and isinstance(value, dict): result[key] = emit(value)
            else: result[key] = deepcopy(value)
        return result
    result = emit(schema)
    result['$defs'] = {label: emit(nodes[key], definition=True) for key, label in selected.items()}
    return result

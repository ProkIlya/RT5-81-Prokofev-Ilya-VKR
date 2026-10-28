"""Типизированный черновик формы; семантика/check_goal остаются в общем Core."""
from copy import deepcopy
import json

from api_agent.bounded_json import validate_tree
from api_agent.goals.checks.compiler import PRIVATE_NAMES, _schema, _closed
from api_agent.goals.checks.selected_compile import compile_selected
from api_agent.openapi.model import PROFILE_VERSION, pointer_part
from api_agent.policy.config import decision
from api_agent.validation import schema_issues
from api_agent.desktop.project_views import operation_rows


def typed_literal(text, schema):
    """Строка остаётся строкой; boolean/null/числа вводятся явно по схеме."""
    if text == 'null' and schema.get('nullable'):
        value = None
    elif schema.get('type') == 'string':
        value = text
    else:
        try: value = json.loads(text)
        except (TypeError, ValueError): raise ValueError('Введите значение указанного типа') from None
    if schema_issues(value, schema): raise ValueError('Значение не соответствует типу/enum поля')
    return value


def schema_fields(schema, path=''):
    """Публичные schema paths; индекс0 обозначает поле элемента, а не найденный ID."""
    if not isinstance(schema, dict): return {}
    result = {path: deepcopy(schema)}
    if schema.get('type') == 'object':
        for key, field in schema.get('properties', {}).items():
            if key.lower() not in PRIVATE_NAMES:
                result.update(schema_fields(field, path + '/' + pointer_part(key)))
    elif schema.get('type') == 'array':
        result.update(schema_fields(schema.get('items'), path + '/0'))
    return result


class ConstructorState:
    def __init__(self, catalog, config, cards=None, *, attachments=None):
        self.catalog = catalog; self.config = config
        self.cards = deepcopy(cards or []); self.attachments = deepcopy(attachments or [])

    def operations(self, *, search='', method='ALL'):
        return [row for row in operation_rows(self.catalog, self.config, search=search, method=method)
            if decision(self.config.snapshot, row['id'], self.catalog.operation_hashes[row['id']]) != 'DENY']

    def response_fields(self, operation, statuses):
        condition = {'slot': '_ui', 'predicate': 'status_in', 'parameters': {'values': statuses}}
        return schema_fields(_schema(self.catalog, {'id': '_ui', 'operation_id': operation}, [condition]))

    def request_fields(self, operation):
        item = self.catalog.operations[operation]
        return {'parameters': {p['name']: deepcopy(p['schema']) for p in item['parameters']
                    if p['name'].lower() not in PRIVATE_NAMES},
            'body': {k: deepcopy(v) for k, v in item.get('requestBody', {}).get('content', {})
                .get('application/json', {}).get('schema', {}).get('properties', {}).items()
                if k.lower() not in PRIVATE_NAMES}}

    def document(self, goal, *, cards=None):
        selected = self.cards if cards is None else cards
        return {'schema_version': 'selected-checks-v1', 'goal': goal,
            'mode': 'testing' if any(c.get('enabled', True) for c in selected) else 'research',
            'cards': deepcopy(selected)}

    def compiled(self, goal, *, cards=None):
        accepted = compile_selected(self.catalog, self.config, self.document(goal, cards=cards), source='user_constructor')
        snapshot = self.config.snapshot
        for card in accepted.snapshot['cards']:
            if not card['enabled']: continue
            operations = [card['operation_id']] + [v['response']['operation_id']
                for v in card['where'].get('parameters', {}).values() if isinstance(v, dict) and 'response' in v]
            if any(decision(snapshot, key, self.catalog.operation_hashes[key]) == 'DENY' for key in operations):
                raise ValueError('selected_policy_denied')
        return accepted

    def replace(self, cards):
        self.compiled('Черновик условий', cards=cards)
        self.cards = deepcopy(cards)

    def export_text(self):
        self.compiled('Черновик условий')
        return json.dumps({'schema_version': 'constructor-v1', 'profile_version': PROFILE_VERSION,
            'spec_hash': self.catalog.spec_hash, 'cards': self.cards}, ensure_ascii=False, indent=2)

    def import_text(self, text):
        if not isinstance(text, str) or len(text.encode('utf-8')) > 65536: raise ValueError('constructor_import_size')
        try: value = json.loads(text)
        except ValueError: raise ValueError('constructor_invalid_json') from None
        validate_tree(value, max_depth=24, max_bytes=65536)
        _closed(value, {'schema_version', 'profile_version', 'spec_hash', 'cards'})
        if value['schema_version'] != 'constructor-v1' or value['profile_version'] != PROFILE_VERSION:
            raise ValueError('constructor_unsupported_version')
        if value['spec_hash'] != self.catalog.spec_hash: raise ValueError('constructor_stale_spec')
        self.replace(value['cards'])

    def attachment_choices(self, schema):
        return [deepcopy(field) for attachment in self.attachments for field in attachment.get('fields', [])
            if not isinstance(field.get('value'), (dict, list)) and not schema_issues(field.get('value'), schema)]

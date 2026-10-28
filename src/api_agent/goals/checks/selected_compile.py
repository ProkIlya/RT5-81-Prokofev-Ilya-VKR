"""Ручной versioned config: декларативные карточки без generator/HTTP-плана."""
from copy import deepcopy
import re

from api_agent.bounded_json import validate_tree
from api_agent.openapi.parser import canonical
from api_agent.validation import fingerprint, schema_issues
from api_agent.goals.checks.compiler import AcceptedGoal, _closed, _path_schema, _schema, _number
from api_agent.goals.checks.condition_scope import validate_where
from api_agent.goals.checks.selected_inputs import compile_inputs

CONTRACT = 'selected-checks-v1'
CHECKER = 'selected-evidence-v2'


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', value):
        raise ValueError('selected_invalid_id')
    return value


def _assertion(value, schema):
    _closed(value, {'predicate'}, {'path', 'value', 'expected', 'tolerance', 'mode', 'rows'})
    kind = value['predicate']
    forms = {'equals': ({'path', 'value'}, 'json_value_equals'),
        'not_equals': ({'path', 'value'}, 'json_value_not_equals'),
        'exists': ({'path'}, 'json_field_exists'), 'schema': (set(), 'response_schema'),
        'number_rounded': ({'path', 'expected'}, 'json_number_rounded'),
        'number_within': ({'path', 'expected', 'tolerance'}, 'json_number_within'),
        'collection': ({'path', 'mode', 'rows'}, 'json_collection_matches')}
    if kind not in forms: raise ValueError('selected_unsupported_predicate')
    fields, predicate = forms[kind]
    if set(value) != fields | {'predicate'}: raise ValueError('selected_invalid_assertion')
    params = {k: deepcopy(value[k]) for k in fields}
    field = _path_schema(schema, params['path']) if 'path' in params else schema
    if kind in {'equals', 'not_equals'}:
        if field.get('type') in {'object', 'array'} or schema_issues(params['value'], field):
            raise ValueError('selected_invalid_value')
    if kind.startswith('number_'):
        if field.get('type') not in {'integer', 'number'}: raise ValueError('selected_invalid_number')
        _number(params['expected'], max_decimals=18)
        if 'tolerance' in params and _number(params['tolerance'], max_decimals=18) <= 0:
            raise ValueError('selected_invalid_tolerance')
    if kind == 'collection':
        if field.get('type') != 'array' or params['mode'] not in {'includes', 'exact'}:
            raise ValueError('selected_invalid_collection')
        rows = params['rows']
        if not isinstance(rows, list) or len(rows) > 32 or params['mode'] == 'includes' and not rows:
            raise ValueError('selected_invalid_collection')
        for row in rows:
            if not isinstance(row, dict) or not row or len(row) > 16: raise ValueError('selected_invalid_row')
            for path, expected in row.items():
                column = _path_schema(field['items'], path)
                if column.get('type') in {'object', 'array'} or schema_issues(expected, column):
                    raise ValueError('selected_invalid_row_value')
        params['mode'] = 'contains' if params['mode'] == 'includes' else 'exact'
    return predicate, params


def _negative(value, catalog, name, statuses):
    _closed(value, {'location', 'path', 'rule'})
    if value['location'] != 'body' or value['rule'] not in {'required', 'schema_type', 'enum'}:
        raise ValueError('selected_unsupported_negative')
    if not statuses or not all(400 <= s < 500 for s in statuses): raise ValueError('selected_negative_status')
    schema = catalog.operations[name].get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema')
    path = value['path']
    if not isinstance(path, str) or not path.startswith('/') or path.count('/') != 1:
        raise ValueError('selected_unsupported_negative_path')
    field = _path_schema(schema, path)
    key = path[1:].replace('~1', '/').replace('~0', '~')
    if value['rule'] == 'required' and key not in schema.get('required', []):
        raise ValueError('selected_negative_not_required')
    if value['rule'] == 'enum' and 'enum' not in field: raise ValueError('selected_negative_no_enum')
    return deepcopy(value)


def compile_selected(catalog, config, document, *, source='user_import', version=1):
    """Валидировать форму и типы; принятие снимка выполняет application отдельно."""
    validate_tree(document, max_depth=24, max_nodes=10000, max_bytes=65536)
    _closed(document, {'schema_version', 'goal', 'mode', 'cards'}, {'attachments','input_slots'})
    if document['schema_version'] != CONTRACT or source not in {'user_import', 'user_constructor'}:
        raise ValueError('selected_unsupported_version')
    goal, mode, cards = document['goal'], document['mode'], document['cards']
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 12000: raise ValueError('selected_invalid_goal')
    if mode not in {'research', 'testing'}: raise ValueError('selected_invalid_mode')
    if type(version) is not int or version < 1: raise ValueError('selected_invalid_version')
    if not isinstance(cards, list) or len(cards) > 16: raise ValueError('selected_card_limit')
    if catalog.spec_hash != config.snapshot['spec_hash']: raise ValueError('selected_stale_config')
    inputs=compile_inputs(catalog,config,document.get('input_slots',[]))
    conditions, observations, normalized, ids = [], [], [], set()
    for card in cards:
        _closed(card, {'id', 'operation_id', 'statuses'},
            {'assertions', 'where', 'selector', 'auth', 'enabled', 'name', 'negative'})
        key = _identifier(card['id'])
        if key in ids: raise ValueError('selected_duplicate_card')
        ids.add(key)
        if type(card.get('enabled', True)) is not bool: raise ValueError('selected_invalid_enabled')
        name = card['operation_id']
        if not isinstance(name, str) or name not in config.snapshot['operations'] or name not in catalog.operations:
            raise ValueError('selected_foreign_operation')
        statuses = card['statuses']
        if not isinstance(statuses, list) or not 1 <= len(statuses) <= 16 or any(type(s) is not int or not 100 <= s <= 599 for s in statuses):
            raise ValueError('selected_invalid_statuses')
        statuses = sorted(set(statuses))
        selector, auth = card.get('selector', 'last'), card.get('auth', 'any')
        if selector not in {'first', 'last', 'all'} or auth not in {'any', 'current', 'anonymous'}:
            raise ValueError('selected_unsupported_scope')
        if auth == 'current' and not catalog.operations[name]['security']:
            raise ValueError('selected_current_auth_not_declared')
        label = card.get('name', '')
        if not isinstance(label, str) or len(label) > 200: raise ValueError('selected_invalid_name')
        observation = {'id': key, 'operation_id': name, 'where': validate_where(catalog,
            config.snapshot['operations'], name, card.get('where', {})), 'selector': selector, 'auth': auth}
        assertions = card.get('assertions', [])
        if not isinstance(assertions, list) or len(assertions) > 16: raise ValueError('selected_assertion_limit')
        status_condition = {'id': key + '_status', 'requirement_id': 'selected', 'observation_id': key,
            'predicate': 'status_in', 'parameters': {'values': statuses}, 'source': 'USER_DEFINED', 'basis': {'source': source}}
        schema = _schema(catalog, {'id': key, 'operation_id': name}, [{**status_condition, 'slot': key}])
        compiled = [status_condition]
        for index, assertion in enumerate(assertions, 1):
            predicate, params = _assertion(assertion, schema)
            compiled.append({**deepcopy(status_condition), 'id': key + '_' + str(index),
                'predicate': predicate, 'parameters': params})
        item = {**observation, 'statuses': statuses, 'assertions': deepcopy(assertions), 'name': label, 'enabled': card.get('enabled', True)}
        if 'negative' in card: item['negative'] = _negative(card['negative'], catalog, name, statuses)
        normalized.append(item)
        if item['enabled']:
            observations.append(observation); conditions.extend(compiled)
    if mode == 'testing' and not observations: raise ValueError('selected_empty_testing')
    if mode == 'research' and observations: raise ValueError('selected_research_conditions')
    if len(conditions) > 64: raise ValueError('selected_condition_limit')
    attachments = document.get('attachments', [])
    if not isinstance(attachments, list) or len(attachments) > 8: raise ValueError('selected_attachment_limit')
    snapshot = {'contract': CONTRACT, 'checker_version': CHECKER, 'version': version,
        'goal': goal, 'goal_hash': fingerprint(goal), 'mode': mode, 'project_id': config.snapshot['project_id'],
        'target': config.snapshot['target'], 'spec_hash': catalog.spec_hash, 'config_hash': fingerprint(config.snapshot),
        'config_version': config.snapshot['version'], 'source': source, 'cards': normalized,
        'observations': observations, 'conditions': conditions, 'answer_fields': [], 'input_slots': inputs,
        'attachments': deepcopy(attachments), 'coverage': 'selected_conditions' if mode == 'testing' else 'not_configured',
        'operation_hashes': deepcopy(catalog.operation_hashes), 'acceptance': 'pending'}
    return AcceptedGoal(canonical(snapshot))

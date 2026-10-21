"""S09b1: закрытый compiler принятого смысла и checklist без HTTP/LLM."""
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import json
import re

from ..bounded_json import validate_tree
from ..openapi.parser import canonical, pointer_get
from ..validation import fingerprint, schema_issues, prepare_expectations, validate_exchange

PREDICATES = {'status_in', 'json_value_equals', 'json_number_rounded', 'json_collection_matches', 'value_equal', 'response_schema'}
PRIVATE_NAMES = {'password', 'token', 'secret', 'authorization', 'cookie', 'access_token', 'refresh_token'}


@dataclass(frozen=True)
class AcceptedGoal:
    """Независимые JSON-копии не меняют закреплённые условия."""
    _payload: bytes

    @property
    def snapshot(self):
        return json.loads(self._payload)

    @property
    def hash(self):
        return fingerprint(self.snapshot)


def _closed(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or not set(required) <= value.keys():
        raise ValueError('goal_invalid_shape')


def _index(items):
    if not isinstance(items, list) or not 1 <= len(items) <= 32:
        raise ValueError('goal_invalid_list')
    if any(not isinstance(v, dict) or not isinstance(v.get('id'), str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', v['id']) for v in items):
        raise ValueError('goal_invalid_id')
    result = {v['id']: v for v in items}
    if len(result) != len(items):
        raise ValueError('goal_duplicate_id')
    return result


def _schema(catalog, slot):
    operation = catalog.operations[slot['operation_id']]
    status = min((s for s in operation['responses'] if s.isdigit() and 200 <= int(s) < 300), default=None)
    if status is None:
        raise ValueError('goal_unsupported_response')
    return operation['responses'][status].get('content', {}).get('application/json', {}).get('schema')


def _path_schema(schema, path):
    if not isinstance(path, str) or len(path) > 500 or (path and not path.startswith('/')) or re.search(r'~(?![01])', path):
        raise ValueError('goal_unsupported_path')
    for raw in path[1:].split('/') if path else []:
        part = raw.replace('~1', '/').replace('~0', '~')
        if part.lower() in PRIVATE_NAMES:
            raise ValueError('goal_private_path')
        if not isinstance(schema, dict):
            raise ValueError('goal_unsupported_path')
        if schema.get('type') == 'array' and re.fullmatch(r'0|[1-9][0-9]*', part):
            schema = schema.get('items')
        else:
            schema = schema.get('properties', {}).get(part)
    if not isinstance(schema, dict):
        raise ValueError('goal_unsupported_path')
    return schema


def _number(text):
    if not isinstance(text, str) or not re.fullmatch(r'-?\d{1,18}(?:[.,]\d{1,12})?', text):
        raise ValueError('goal_invalid_decimal_expectation')
    return Decimal(text.replace(',', '.'))


def resolve_inputs(value, inputs):
    """Заполнение принятого typed slot не меняет сохранённый descriptor."""
    if isinstance(value, dict):
        if set(value) == {'input'}: return deepcopy(inputs[value['input']])
        return {k: resolve_inputs(v, inputs) for k, v in value.items()}
    if isinstance(value, list): return [resolve_inputs(v, inputs) for v in value]
    return deepcopy(value)


def input_references(value):
    """Структурные ссылки, без поиска ключевых слов в сериализованном JSON."""
    if isinstance(value, dict):
        if set(value) == {'input'}: return {value['input']}
        return set().union(*(input_references(v) for v in value.values()))
    if isinstance(value, list): return set().union(*(input_references(v) for v in value))
    return set()


def _registered(goal, proposal, catalog):
    """Узкая независимо проверяемая форма, без доменных goal adapters."""
    match = re.fullmatch(r'Проверь ([A-Za-z][A-Za-z0-9_-]*): статус ([1-5][0-9]{2})\.', goal)
    if not match or len(proposal['slots']) != 1 or len(proposal['conditions']) != 1:
        return False
    slot, condition = proposal['slots'][0], proposal['conditions'][0]
    return (slot['operation_id'] == match[1] and catalog.operations[match[1]]['method'] == 'GET'
            and slot['parameters'] == {} and 'body' not in slot and slot['auth_mode'] == 'anonymous'
            and slot['actor'] == 'anonymous' and slot['auth_epoch'] == 0 and not slot['depends_on']
            and condition['predicate'] == 'status_in' and condition['parameters'] == {'values': [int(match[2])]})


def compile_goal(catalog, config, goal, proposal, *, confirmation=None, force_confirmation=False):
    """Модель не может сама назначить USER_DEFINED/complete или принять себя."""
    validate_tree(proposal, max_depth=24, max_nodes=10000, max_bytes=65536)
    _closed(proposal, {'version', 'goal_hash', 'spec_hash', 'config_hash', 'requirements', 'slots', 'conditions', 'answer_fields', 'unresolved'}, {'input_slots'})
    if (type(proposal['version']) is not int or proposal['version'] < 1 or not isinstance(goal, str)
            or not goal.strip() or len(goal) > 12000 or proposal['goal_hash'] != fingerprint(goal)
            or proposal['spec_hash'] != catalog.spec_hash or proposal['config_hash'] != fingerprint(config.snapshot)
            or config.snapshot['spec_hash'] != catalog.spec_hash):
        raise ValueError('goal_identity_mismatch')
    if not isinstance(proposal['unresolved'], list) or proposal['unresolved']:
        raise ValueError('goal_unresolved')
    requirements, slots, conditions, answers = (_index(proposal[k]) for k in ('requirements', 'slots', 'conditions', 'answer_fields'))
    inputs = _index(proposal['input_slots']) if proposal.get('input_slots') else {}
    for descriptor in inputs.values():
        _closed(descriptor, {'id', 'type'})
        if descriptor['type'] not in {'string', 'integer', 'number', 'boolean'}: raise ValueError('goal_invalid_input_type')
    samples = {key: {'string': 'A', 'integer': 1, 'number': 1.0, 'boolean': True}[v['type']] for key, v in inputs.items()}
    covered = set()
    for requirement in requirements.values():
        _closed(requirement, {'id', 'span', 'interpretation'})
        span = requirement['span']
        if (not isinstance(span, list) or len(span) != 2 or any(type(v) is not int for v in span)
                or not 0 <= span[0] < span[1] <= len(goal) or not isinstance(requirement['interpretation'], str)
                or not requirement['interpretation'].strip()):
            raise ValueError('goal_invalid_requirement')
        positions = {i for i in range(*span) if not goal[i].isspace()}
        if covered & positions:
            raise ValueError('goal_overlapping_requirements')
        covered |= positions
    if covered != {i for i, char in enumerate(goal) if not char.isspace()}:
        raise ValueError('goal_missing_requirement')
    previous = set()
    for slot in slots.values():
        _closed(slot, {'id', 'operation_id', 'parameters', 'auth_mode', 'actor', 'auth_epoch', 'depends_on'}, {'body', 'scope'})
        operation = catalog.operations.get(slot['operation_id'])
        if operation is None or slot['operation_id'] not in config.snapshot['operations']:
            raise ValueError('goal_foreign_operation')
        if slot['auth_mode'] not in {'anonymous', 'anonymous_negative', 'authenticated', 'old_epoch'} or slot['actor'] not in {'anonymous', 'current_user'} or type(slot['auth_epoch']) is not int or slot['auth_epoch'] < 0:
            raise ValueError('goal_invalid_actor')
        if (slot['actor'] == 'anonymous') != (slot['auth_mode'] in {'anonymous', 'anonymous_negative'}):
            raise ValueError('goal_invalid_actor')
        if not isinstance(slot['depends_on'], list) or not set(slot['depends_on']) <= previous:
            raise ValueError('goal_invalid_dependency')
        previous.add(slot['id'])
        descriptors = {p['name']: p for p in operation['parameters']}
        params = slot['parameters']
        if not isinstance(params, dict) or not params.keys() <= descriptors.keys():
            raise ValueError('goal_invalid_parameters')
        for name, descriptor in descriptors.items():
            if descriptor['required'] and name not in params:
                raise ValueError('goal_missing_parameter')
            if name in params and schema_issues(params[name], descriptor['schema']):
                # Динамический input ссылается только на предшествующий slot.
                value = params[name]
                if isinstance(value, dict) and set(value) == {'input'} and value['input'] in inputs:
                    if schema_issues(samples[value['input']], descriptor['schema']): raise ValueError('goal_invalid_input_type')
                    continue
                if not isinstance(value, dict) or set(value) != {'from_slot', 'path'} or value['from_slot'] not in slot['depends_on']:
                    raise ValueError('goal_invalid_parameter_type')
                _path_schema(_schema(catalog, slots[value['from_slot']]), value['path'])
        if 'body' in slot:
            schema = operation.get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema')
            if schema is None:
                raise ValueError('goal_body_denied')
            def placeholders(value):
                if isinstance(value, dict):
                    if set(value) == {'input'}:
                        if value['input'] not in samples: raise ValueError('goal_unknown_input')
                        return samples[value['input']]
                    if set(value) == {'private'} and isinstance(value['private'], str): return 'private-input'
                    return {k: placeholders(v) for k, v in value.items()}
                if isinstance(value, list): return [placeholders(v) for v in value]
                return value
            if schema_issues(placeholders(slot['body']), schema):
                raise ValueError('goal_invalid_body')
            for key in PRIVATE_NAMES & slot['body'].keys():
                if not isinstance(slot['body'][key], dict) or set(slot['body'][key]) != {'private'}:
                    raise ValueError('goal_private_input_required')
        if 'scope' in slot:
            _closed(slot['scope'], {'mode', 'parameters'})
            if slot['scope']['mode'] != 'response' or slot['scope']['parameters'] != params:
                raise ValueError('goal_invalid_scope')
    for condition in conditions.values():
        _closed(condition, {'id', 'requirement_id', 'slot', 'predicate', 'parameters', 'source', 'basis'})
        if condition['requirement_id'] not in requirements or condition['slot'] not in slots or condition['predicate'] not in PREDICATES or condition['source'] not in {'MODEL_PROPOSED', 'CONTRACT'}:
            raise ValueError('goal_invalid_condition')
        slot, params, predicate = slots[condition['slot']], condition['parameters'], condition['predicate']
        schema = _schema(catalog, slot)
        if predicate == 'status_in':
            _closed(params, {'values'})
            if not isinstance(params['values'], list) or not params['values'] or any(type(v) is not int or not 100 <= v <= 599 for v in params['values']):
                raise ValueError('goal_invalid_status')
        elif predicate in {'json_value_equals', 'json_number_rounded'}:
            _closed(params, {'path', 'value'} if predicate == 'json_value_equals' else {'path', 'expected'})
            field = _path_schema(schema, params['path'])
            if predicate == 'json_value_equals':
                expected = params['value']
                if isinstance(expected, dict) and set(expected) == {'input'} and expected['input'] in samples:
                    expected = samples[expected['input']]
                if isinstance(expected, (dict, list)) or schema_issues(expected, field):
                    raise ValueError('goal_invalid_expected_value')
            elif field.get('type') not in {'number', 'integer'}:
                raise ValueError('goal_invalid_decimal_type')
            else:
                _number(params['expected'])
                span = requirements[condition['requirement_id']]['span']
                source_text = goal[span[0]:span[1]].replace(',', '.')
                literal = params['expected'].replace(',', '.')
                if not re.search(r'(?<![\d.,])' + re.escape(literal) + r'(?!\d|[.,]\d)', source_text):
                    raise ValueError('goal_expected_input_required')
        elif predicate == 'json_collection_matches':
            _closed(params, {'path', 'rows', 'mode'})
            array = _path_schema(schema, params['path'])
            if 'scope' not in slot:
                raise ValueError('goal_collection_scope_required')
            if array.get('type') != 'array' or params['mode'] not in {'contains', 'exact', 'absent'} or not isinstance(params['rows'], list) or not 1 <= len(params['rows']) <= 16:
                raise ValueError('goal_invalid_collection')
            for row in params['rows']:
                if not isinstance(row, dict) or not row: raise ValueError('goal_invalid_collection')
                for path, value in row.items():
                    if schema_issues(value, _path_schema(array['items'], path)): raise ValueError('goal_invalid_collection')
        elif predicate == 'value_equal':
            _closed(params, {'left', 'right'})
            for ref in params.values():
                _closed(ref, {'slot', 'path'})
                if ref['slot'] not in slots: raise ValueError('goal_foreign_slot')
                _path_schema(_schema(catalog, slots[ref['slot']]), ref['path'])
            if params['left']['slot'] != condition['slot'] or params['right']['slot'] not in slot['depends_on']:
                raise ValueError('goal_self_comparison')
            source_slot = slots[params['right']['slot']]
            if (catalog.operations[source_slot['operation_id']]['method'] == 'POST'
                and catalog.operations[slot['operation_id']]['method'] == 'GET'
                and not any(v == {'from_slot': source_slot['id'], 'path': params['right']['path']} for v in slot['parameters'].values())):
                raise ValueError('goal_identity_target_required')
        else:
            _closed(params, set())
            if schema is None: raise ValueError('goal_unsupported_response')
        if condition['source'] == 'CONTRACT':
            if predicate not in {'status_in', 'json_value_equals'}: raise ValueError('goal_unsupported_contract_basis')
            prepare_expectations(catalog, [{'source': 'CONTRACT', 'predicate': predicate, 'parameters': params,
                'intent': 'positive', 'basis': condition['basis'], 'preconditions': 'verified'}], 'compile', goal,
                operation_id=slot['operation_id'])
        elif condition['basis'] is not None:
            raise ValueError('goal_false_source')
    condition_requirements = {c['requirement_id'] for c in conditions.values()}
    answer_requirements = set()
    for answer in answers.values():
        _closed(answer, {'id', 'requirement_id', 'slot', 'path'})
        if answer['requirement_id'] not in requirements or answer['slot'] not in slots:
            raise ValueError('goal_foreign_answer')
        _path_schema(_schema(catalog, slots[answer['slot']]), answer['path'])
        answer_requirements.add(answer['requirement_id'])
    if requirements.keys() != condition_requirements or requirements.keys() != answer_requirements:
        raise ValueError('goal_missing_requirement_mapping')
    if {c['slot'] for c in conditions.values()} != slots.keys():
        raise ValueError('goal_unmapped_slot')
    for slot in slots.values():
        if slot['auth_mode'] == 'old_epoch' and (catalog.operations[slot['operation_id']]['method'] != 'GET'
            or slot['auth_epoch'] < 1 or not any(c['slot'] == slot['id'] and c['predicate'] == 'status_in'
                and c['parameters'] == {'values': [401]} for c in conditions.values())):
            raise ValueError('goal_invalid_old_epoch')
    registered = _registered(goal, proposal, catalog) and not force_confirmation
    confirmed = (isinstance(confirmation, dict) and confirmation == {
        'proposal_hash': fingerprint(proposal), 'approved': True, 'complete': True})
    if not registered and not confirmed:
        raise ValueError('goal_acceptance_required')
    accepted = deepcopy(proposal)
    accepted.update(goal=goal, proposal_hash=fingerprint(proposal), coverage='complete',
                    acceptance='registered_form' if registered else 'user_confirmed')
    if registered:
        for requirement in accepted['requirements']:
            requirement['interpretation'] = goal[requirement['span'][0]:requirement['span'][1]]
    for condition in accepted['conditions']:
        if condition['source'] == 'MODEL_PROPOSED': condition['source'] = 'USER_DEFINED'
    return AcceptedGoal(canonical(accepted))


def check_goal(accepted, catalog, run):
    """Только сохранённые exchanges: нет сети, выбора последнего PASS или LLM."""
    manifest = accepted.snapshot
    if manifest['spec_hash'] != catalog.spec_hash:
        raise ValueError('goal_stale_manifest')
    slots = {s['id']: s for s in manifest['slots']}
    actual = {}
    for name, slot in slots.items():
        candidates = [e for e in run.get('exchanges', []) if e.get('occurrence_id') == name and e['operation_id'] == slot['operation_id']]
        if len(candidates) != 1: continue
        exchange = candidates[0]
        principal = exchange.get('request_principal', {})
        try: params = resolve_inputs(slot['parameters'], run.get('goal_inputs', {}))
        except KeyError: continue
        try:
            for key, value in params.items():
                if isinstance(value, dict) and 'from_slot' in value:
                    params[key] = pointer_get(actual[value['from_slot']]['response']['json'], value['path'])
        except (KeyError, TypeError, IndexError): continue
        if (exchange.get('outcome') != 'received' or exchange.get('truncated')
            or not exchange.get('response', {}).get('json_valid') or principal.get('mode') != slot['auth_mode']
            or principal.get('auth_epoch') != slot['auth_epoch'] or (slot['actor'] == 'current_user' and principal.get('actor') is None)
            or canonical(exchange.get('request_parameters', {})) != canonical(params)):
            continue
        actual[name] = exchange
    results = []
    for condition in manifest['conditions']:
        exchange = actual.get(condition['slot'])
        try: params = resolve_inputs(condition['parameters'], run.get('goal_inputs', {}))
        except KeyError:
            results.append({'condition_id': condition['id'], 'requirement_id': condition['requirement_id'], 'verdict': 'UNKNOWN', 'expected': condition['parameters'], 'actual': None, 'exchange_id': None})
            continue
        predicate = condition['predicate']
        verdict, observed = 'UNKNOWN', None
        try:
            if exchange is not None:
                body = exchange['response']['json']
                if predicate == 'status_in':
                    observed = exchange['response']['status']; passed = observed in params['values']
                elif predicate == 'json_value_equals':
                    try:
                        observed = pointer_get(body, params['path']); passed = canonical(observed) == canonical(params['value'])
                    except (KeyError, IndexError):
                        observed, passed = None, False
                elif predicate == 'json_number_rounded':
                    observed = pointer_get(body, params['path'])
                    expected = _number(params['expected'])
                    passed = type(observed) in (int, float) and Decimal(str(observed)).quantize(Decimal(1).scaleb(expected.as_tuple().exponent), rounding=ROUND_HALF_UP) == expected
                elif predicate == 'value_equal':
                    observed = pointer_get(body, params['left']['path'])
                    other = actual[params['right']['slot']]['response']['json']
                    passed = canonical(observed) == canonical(pointer_get(other, params['right']['path']))
                elif predicate == 'response_schema':
                    passed = not schema_issues(body, _schema(catalog, slots[condition['slot']]))
                else:
                    prepared = {'id': condition['id'], 'source': 'USER_DEFINED', 'predicate': predicate,
                        'parameters': params, 'intent': 'positive', 'preconditions': 'verified', 'basis': None}
                    passed = validate_exchange(catalog, exchange, [prepared])['checks'][0]['verdict'] == 'pass'
                verdict = 'PASS' if passed else 'FAIL'
        except (KeyError, IndexError, TypeError, InvalidOperation, ValueError): pass
        results.append({'condition_id': condition['id'], 'requirement_id': condition['requirement_id'],
            'verdict': verdict, 'expected': deepcopy(params), 'actual': deepcopy(observed),
            'exchange_id': exchange['id'] if exchange else None})
    fields = {}
    for answer in manifest['answer_fields']:
        try:
            body = actual[answer['slot']]['response']['json']
            fields[answer['id']] = deepcopy(body if answer['path'] == '' else pointer_get(body, answer['path']))
        except (KeyError, IndexError, TypeError): pass
    verdicts = {r['verdict'] for r in results}
    verdict = 'FAIL' if 'FAIL' in verdicts else 'PASS' if verdicts == {'PASS'} and len(fields) == len(manifest['answer_fields']) and len(actual) == len(slots) else 'UNKNOWN'
    return {'manifest_hash': accepted.hash, 'verdict': verdict, 'conditions': results, 'answer_fields': fields,
            'coverage': 'complete' if verdict == 'PASS' else 'partial'}

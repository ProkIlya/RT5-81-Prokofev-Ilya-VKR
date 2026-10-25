"""Неизменные условия цели без предписанного графа HTTP (контракт v2)."""
from copy import deepcopy

from ..bounded_json import validate_tree
from ..openapi.parser import canonical, pointer_get
from ..validation import fingerprint
from .compiler import (AcceptedGoal, _closed, _index, _path_schema,
                       _schema, _number, evaluate_condition, validate_conditions)
from .generation import _lower_checks
from .condition_scope import (validate_where, resolve_where, subset as _subset,
                              validate_when, observation_window, validate_comparison)
from .condition_auth import validate_auth_scope, validate_auth_observations, auth_candidate


CONTRACT = 'conditions-v2'


def _evaluation_view(manifest):
    """Проекция схем для общих validators; это не последовательность HTTP."""
    slots = [{**deepcopy(o), 'parameters': {}, 'depends_on': [],
              'scope': {'mode': 'response', 'parameters': {}}} for o in manifest['observations']]
    conditions = [{**{k: deepcopy(v) for k, v in c.items() if k != 'observation_id'},
                   'slot': c['observation_id']} for c in manifest['conditions']
                  if c['predicate'] != 'operation_observed']
    return {'slots': slots, 'conditions': conditions}


def _provided_numbers(catalog, config, provided, slots=None, conditions=None):
    """Отдельный доверенный ввод конструктора; предложение модели его не содержит."""
    if provided is None: return []
    validate_tree(provided, max_depth=4, max_nodes=100, max_bytes=8192)
    if not isinstance(provided, list) or not 1 <= len(provided) <= 16:
        raise ValueError('goal_invalid_expected_numbers')
    used = [(slots[c['slot']]['operation_id'], c['parameters']['path'], c['parameters']['expected'], c['parameters'].get('tolerance'))
        for c in conditions.values() if c['predicate'] in {'json_number_rounded', 'json_number_within'}] if conditions is not None else None
    seen = set()
    for item in provided:
        _closed(item, {'operation_id', 'path', 'expected'}, {'tolerance'})
        name, path, expected = item['operation_id'], item['path'], item['expected']
        if not isinstance(name, str) or name not in config.snapshot['operations']:
            raise ValueError('goal_foreign_operation')
        field = _path_schema(_schema(catalog, {'id': '_provided', 'operation_id': name}), path)
        if field.get('type') not in {'number', 'integer'}:
            raise ValueError('goal_invalid_decimal_type')
        _number(expected, max_decimals=18 if 'tolerance' in item else 12)
        if 'tolerance' in item and _number(item['tolerance'], max_decimals=18) <= 0:
            raise ValueError('goal_invalid_tolerance')
        if (name, path) in seen or used is not None and (name, path, expected, item.get('tolerance')) not in used:
            raise ValueError('goal_unused_expected_number')
        seen.add((name, path))
    return deepcopy(provided)


def compile_conditions(catalog, config, goal, proposal, *, confirmation=None, version=1, user_numbers=None):
    """Модель задаёт checks; только человек принимает их полноту и смысл."""
    validate_tree(proposal, max_depth=24, max_nodes=10000, max_bytes=65536)
    _closed(proposal, {'checks', 'answer_fields'}, {'unresolved', 'input_slots'})
    if not isinstance(goal, str) or not goal.strip() or len(goal) > 12000:
        raise ValueError('goal_invalid_requirement')
    if config.snapshot['spec_hash'] != catalog.spec_hash: raise ValueError('goal_identity_mismatch')
    if type(version) is not int or version < 1: raise ValueError('goal_version_mismatch')
    if proposal.get('unresolved', []): raise ValueError('goal_unresolved')
    checks, answers = proposal['checks'], proposal['answer_fields']
    if not isinstance(checks, list) or not 1 <= len(checks) <= 32:
        raise ValueError('goal_invalid_list')
    if not isinstance(answers, list) or not 0 <= len(answers) <= 32:
        raise ValueError('goal_invalid_list')
    inputs = _index(proposal['input_slots']) if proposal.get('input_slots') else {}
    samples = {}
    for name, descriptor in inputs.items():
        _closed(descriptor, {'id', 'type'})
        if descriptor['type'] not in {'string', 'integer', 'number', 'boolean'}:
            raise ValueError('goal_invalid_input_type')
        samples[name] = {'string': 'A', 'integer': 1, 'number': 1.0, 'boolean': True}[descriptor['type']]
    observations, conditions, fields = [], [], []
    def observation(value):
        name = value['operation_id']
        if not isinstance(name, str) or name not in config.snapshot['operations'] or name not in catalog.operations:
            raise ValueError('goal_foreign_operation')
        where = validate_where(catalog, config.snapshot['operations'], name, value.get('where', {}))
        when = validate_when(catalog, config.snapshot['operations'], name, value['when']) if 'when' in value else None
        auth_scope = validate_auth_scope(value, catalog, name)
        existing = next((o for o in observations if o['operation_id'] == name and o['where'] == where
                         and o.get('when') == when and o.get('auth_scope') == auth_scope), None)
        if existing is None:
            existing = {'id': 'o' + str(len(observations) + 1), 'operation_id': name, 'where': where}
            if when is not None: existing['when'] = when
            if auth_scope is not None: existing['auth_scope'] = auth_scope
            observations.append(existing)
        return existing['id']
    for index, check in enumerate(checks, 1):
        if not isinstance(check, dict) or not isinstance(check.get('operation_id'), str):
            raise ValueError('goal_invalid_condition')
        key = observation(check)
        if check.get('kind') == 'performed':
            _closed(check, {'operation_id', 'kind'}, {'where', 'count', 'auth_scope'})
            count = check.get('count', 1)
            if type(count) is not int or not 1 <= count <= 32: raise ValueError('goal_invalid_condition')
            predicate, parameters = 'operation_observed', {'count': count}
        elif check.get('kind') == 'same':
            _closed(check, {'operation_id', 'kind', 'path', 'other'}, {'where', 'when', 'auth_scope'})
            _closed(check['other'], {'operation_id', 'path'}, {'where', 'when', 'auth_scope'})
            other = observation(check['other'])
            predicate, parameters = 'value_equal', {'left': {'slot': key, 'path': check['path']},
                'right': {'slot': other, 'path': check['other']['path']}}
        else:
            wire = {k: deepcopy(v) for k, v in check.items() if k not in {'operation_id', 'where', 'when', 'auth_scope'}}
            wire['slot'] = key
            lowered = {'checks': [wire], 'slots': []}
            _lower_checks(lowered)
            item = lowered['conditions'][0]
            predicate, parameters = item['predicate'], item['parameters']
            if predicate == 'value_equal': raise ValueError('goal_unsupported_condition')
        conditions.append({'id': 'c' + str(index), 'requirement_id': 'r1', 'observation_id': key,
            'predicate': predicate, 'parameters': parameters, 'source': 'MODEL_PROPOSED', 'basis': None})
    for index, answer in enumerate(answers, 1):
        _closed(answer, {'operation_id', 'path'}, {'where', 'when', 'auth_scope'})
        fields.append({'id': 'a' + str(index), 'requirement_id': 'r1',
                       'observation_id': observation(answer), 'path': answer['path']})
    for observation in observations:
        key, name = observation['id'], observation['operation_id']
        if not any(c['observation_id'] == key and c['predicate'] == 'status_in' for c in conditions):
            statuses = [int(s) for s in catalog.operations[name]['responses'] if s.isdigit() and 200 <= int(s) < 300]
            if not statuses: raise ValueError('goal_unsupported_response')
            status = min(statuses)
            conditions.append({'id': 'status_' + key, 'requirement_id': 'r1', 'observation_id': key,
                'predicate': 'status_in', 'parameters': {'values': [status]}, 'source': 'CONTRACT',
                'basis': {'pointer': '/responses/' + str(status), 'operation_hash': catalog.operation_hashes[name],
                          'spec_hash': catalog.spec_hash}})
    validate_auth_observations(observations, conditions)
    requirement = {'id': 'r1', 'span': [0, len(goal)], 'interpretation': goal}
    manifest = {'contract': CONTRACT, 'version': version, 'goal_hash': fingerprint(goal),
        'spec_hash': catalog.spec_hash, 'config_hash': fingerprint(config.snapshot), 'goal': goal,
        'requirements': [requirement], 'observations': observations, 'conditions': conditions,
        'answer_fields': fields, 'input_slots': list(inputs.values()), 'unresolved': []}
    view = _evaluation_view(manifest)
    indexed = _index(view['slots'])
    checked = _index(view['conditions'])
    provided = _provided_numbers(catalog, config, user_numbers, indexed, checked)
    validate_conditions(catalog, goal, {'r1': requirement}, indexed,
        {key: value for key, value in checked.items() if value['predicate'] != 'value_equal'}, inputs, samples,
        user_numbers=provided)
    comparison_view = {**manifest, 'conditions': view['conditions']}
    for condition in conditions:
        if condition['predicate'] == 'value_equal': validate_comparison(condition, comparison_view, catalog)
    for answer in fields:
        _path_schema(_schema(catalog, indexed[answer['observation_id']], view['conditions']), answer['path'])
    approved = {'proposal_hash': fingerprint(proposal), 'approved': True, 'complete': True}
    if confirmation != approved: raise ValueError('goal_acceptance_required')
    for condition in conditions:
        if condition['source'] == 'MODEL_PROPOSED': condition['source'] = 'USER_DEFINED'
    if provided: manifest['user_numbers'] = provided
    manifest.update(proposal_hash=fingerprint(proposal), acceptance='user_confirmed', coverage='complete')
    return AcceptedGoal(canonical(manifest))


def _candidates(observation, manifest, catalog, run, *, final=True, require_json=True):
    exchanges = run.get('exchanges', [])
    operation = catalog.operations[observation['operation_id']]
    read = operation['method'] in {'GET', 'HEAD', 'OPTIONS'}
    historical_auth = observation.get('auth_scope') == 'revoked' and 'when' not in observation
    window = observation_window(observation, catalog, run) if read and final and not historical_auth else (-1, len(exchanges))
    if window is None: return []
    expected_status = {v for c in manifest['conditions'] if c['observation_id'] == observation['id']
                       and c['predicate'] == 'status_in' for v in c['parameters']['values']}
    principal = run.get('auth_state') or {}
    where = resolve_where(observation, catalog, run)
    if where is None: return []
    candidates = []
    for index, exchange in enumerate(exchanges):
        if exchange.get('operation_id') != observation['operation_id']: continue
        if read and final and not window[0] < index < window[1]: continue
        complete = {'received'} if require_json else {'received', 'invalid_json', 'invalid_content_type'}
        if exchange.get('outcome') not in complete or exchange.get('truncated'): continue
        if require_json and not (exchange.get('response') or {}).get('json_valid'): continue
        actor = exchange.get('request_principal') or {}
        if not auth_candidate(observation, exchange, run, index, principal, catalog): continue
        if read and principal.get('principal') is not None and expected_status != {401}:
            if actor.get('actor') != principal['principal'] or actor.get('auth_epoch') != principal.get('auth_epoch'):
                continue
        if not _subset(where.get('parameters', {}), exchange.get('request_parameters', {})): continue
        if not _subset(where.get('body', {}), exchange.get('request', {}).get('json', {})): continue
        candidates.append(exchange)
    # Выбор сущности не выводится из совпадения ожидаемых значений ответа.
    if read and final and len({canonical(e.get('request_parameters', {})) for e in candidates}) > 1: return []
    return candidates


def check_conditions(accepted, catalog, run):
    """Сохранённые HTTP evidence; нет LLM, нового HTTP или выбора лучшего PASS."""
    manifest = accepted.snapshot
    from .completion import final_state_policy
    final_state = final_state_policy(run)
    if manifest.get('contract') != CONTRACT or manifest['spec_hash'] != catalog.spec_hash:
        raise ValueError('goal_stale_manifest')
    view = _evaluation_view(manifest)
    candidates = {o['id']: _candidates(o, manifest, catalog, run) for o in manifest['observations']}
    statuses = {o['id']: _candidates(o, manifest, catalog, run, require_json=False) for o in manifest['observations']}
    events = {o['id']: _candidates(o, manifest, catalog, run, final=False, require_json=False) for o in manifest['observations']}
    performed = {c['observation_id'] for c in manifest['conditions'] if c['predicate'] == 'operation_observed'}
    by_id = {c['id']: c for c in view['conditions']}
    results = []
    for condition in manifest['conditions']:
        key = condition['observation_id']
        observed = (events[key] if condition['predicate'] == 'operation_observed' or
            condition['predicate'] == 'status_in' and key in performed else
            statuses[key] if condition['predicate'] == 'status_in' else candidates[key])
        if final_state:
            if (condition['predicate'] == 'operation_observed' or condition['predicate'] == 'status_in'
                and key in performed and condition['source'] == 'CONTRACT'):
                expected = {s for c in manifest['conditions'] if c['observation_id'] == key and c['predicate'] == 'status_in'
                    for s in c['parameters']['values']}
                observed = [e for e in observed if (e.get('response') or {}).get('status') in expected]
            elif condition['predicate'] != 'value_equal':
                # Последний ответ в прежнем identity/actor/window scope, не лучший PASS.
                observed = observed[-1:]
        if condition['predicate'] == 'operation_observed':
            result = {'condition_id': condition['id'], 'requirement_id': 'r1',
                'verdict': 'PASS' if len(observed) >= condition['parameters']['count'] else 'UNKNOWN',
                'expected': condition['parameters'], 'actual': {'count': len(observed)},
                'exchange_id': observed[-1]['id'] if observed else None}
        elif condition['predicate'] == 'value_equal':
            other = condition['parameters']['right']['slot']
            pairs = [(e, e) for e in observed] if other == key else [(left, right) for left in observed for right in candidates[other]
                if left['id'] != right['id'] or condition['parameters']['left']['path'] != condition['parameters']['right']['path']]
            evaluated = [evaluate_condition(by_id[condition['id']], view, catalog, {key: left, other: right},
                run.get('goal_inputs', {})) for left, right in pairs] or [evaluate_condition(by_id[condition['id']],
                    view, catalog, {}, run.get('goal_inputs', {}))]
            result = next((v for v in evaluated if v['verdict'] == 'FAIL'), evaluated[-1])
            if pairs:
                right = pairs[next(i for i, value in enumerate(evaluated) if value is result)][1]
                result['other_exchange_id'] = right['id']
                try: result['other_actual'] = deepcopy(pointer_get(right['response']['json'], condition['parameters']['right']['path']))
                except (KeyError, IndexError, TypeError, ValueError): result['other_actual'] = None
        else:
            evaluated = [evaluate_condition(by_id[condition['id']], view, catalog, {key: e},
                run.get('goal_inputs', {})) for e in observed] or [evaluate_condition(by_id[condition['id']],
                    view, catalog, {}, run.get('goal_inputs', {}))]
            result = next((v for v in evaluated if v['verdict'] == 'FAIL'), evaluated[-1])
        results.append(result)
    fields = {}
    for answer in manifest['answer_fields']:
        observed = candidates[answer['observation_id']]
        if not observed: continue
        try:
            body = observed[-1]['response']['json']
            fields[answer['id']] = deepcopy(body if answer['path']=='' else pointer_get(body, answer['path']))
        except (KeyError, IndexError, TypeError, ValueError): pass
    verdicts = {c['verdict'] for c in results}
    verdict = 'FAIL' if 'FAIL' in verdicts else 'PASS' if verdicts == {'PASS'} and len(fields) == len(manifest['answer_fields']) else 'UNKNOWN'
    return {'manifest_hash': accepted.hash, 'verdict': verdict, 'conditions': results,
            'answer_fields': fields, 'coverage': 'complete' if verdict == 'PASS' else 'partial'}

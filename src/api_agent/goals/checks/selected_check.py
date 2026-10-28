"""Выбор evidence до оценки ожиданий; общий predicate evaluator для S10."""
from copy import deepcopy
from decimal import localcontext

from api_agent.openapi.parser import canonical, pointer_get
from api_agent.validation import display_value, schema_issues
from api_agent.goals.checks.compiler import evaluate_condition
from api_agent.goals.legacy.condition_contract import _evaluation_view
from api_agent.goals.checks.condition_scope import subset, _unique_value
from api_agent.goals.checks.selected_compile import CONTRACT


def _resolve(observation, catalog, run, before):
    where = deepcopy(observation['where'])
    for key, literal in where.get('parameters', {}).items():
        if not isinstance(literal, dict) or set(literal) != {'response'}: continue
        source = literal['response']
        candidates = []
        for event in run.get('exchanges', [])[:before]:
            response = event.get('response') or {}
            if (event['operation_id'] != source['operation_id'] or event.get('outcome') != 'received'
                or event.get('truncated') or not response.get('json_valid') or not 200 <= response.get('status', 0) < 300): continue
            if not subset(source.get('where', {}).get('parameters', {}), event.get('request_parameters', {})): continue
            if not subset(source.get('where', {}).get('body', {}), event.get('request', {}).get('json', {})): continue
            principal = event.get('request_principal') or {}
            current = run.get('auth_state') or {}
            if principal.get('mode') == 'authenticated' and (principal.get('actor') != current.get('principal')
                or principal.get('auth_epoch') != current.get('auth_epoch')): continue
            schema = catalog.operations[event['operation_id']]['responses'].get(str(response['status']), {}).get('content', {}).get('application/json', {}).get('schema')
            if schema is None or schema_issues(response['json'], schema): continue
            candidates.append(event)
        occurrence = source.get('occurrence')
        if occurrence == 'first': candidates = candidates[:1]
        elif occurrence == 'last': candidates = candidates[-1:]
        values = {}
        for event in candidates:
            try: value = _unique_value(event['response']['json'], source['path'])
            except (KeyError, IndexError, ValueError, TypeError): continue
            values[canonical(value)] = value
        if len(values) != 1: return None
        where['parameters'][key] = next(iter(values.values()))
    return where


def _candidates(observation, catalog, run):
    result = []
    for index, event in enumerate(run.get('exchanges', [])):
        if event['operation_id'] != observation['operation_id']: continue
        where = _resolve(observation, catalog, run, index)
        if where is None: continue
        if not subset(where.get('parameters', {}), event.get('request_parameters', {})): continue
        if not subset(where.get('body', {}), event.get('request', {}).get('json', {})): continue
        principal, auth = event.get('request_principal') or {}, observation['auth']
        if auth == 'anonymous' and principal.get('mode') not in {'anonymous', 'anonymous_negative'}: continue
        if auth == 'current':
            current = run.get('auth_state') or {}
            if (principal.get('mode') != 'authenticated' or not current.get('principal')
                or principal.get('actor') != current['principal'] or principal.get('auth_epoch') != current.get('auth_epoch')): continue
        result.append(event)
    if observation['selector'] == 'first': return result[:1]
    if observation['selector'] == 'last': return result[-1:]
    return result


def _collection(body, params):
    collection = pointer_get(body, params['path']) if params['path'] else body
    if not isinstance(collection, list): return False
    # Двудольное сопоставление: одинаковым строкам нужны разные элементы.
    candidates = []
    for template in params['rows']:
        indices = []
        for index, item in enumerate(collection):
            try: matching = all(canonical(pointer_get(item, path)) == canonical(expected) for path, expected in template.items())
            except (KeyError, IndexError, TypeError, ValueError): matching = False
            if matching: indices.append(index)
        candidates.append(indices)
    assigned = {}
    def assign(row, seen):
        for index in candidates[row]:
            if index in seen: continue
            seen.add(index)
            if index not in assigned or assign(assigned[index], seen):
                assigned[index] = row; return True
        return False
    passed = all(assign(row, set()) for row in range(len(candidates)))
    return passed and (params['mode'] != 'exact' or len(collection) == len(params['rows']))


def _evaluate(condition, manifest, catalog, event):
    view = _evaluation_view(manifest)
    descriptor = {**condition, 'slot': condition['observation_id']}
    result = {'condition_id': condition['id'], 'requirement_id': 'selected',
        'verdict': 'UNKNOWN', 'expected': deepcopy(condition['parameters']), 'actual': None,
        'actual_present': False, 'exchange_id': event['id'] if event else None, 'reason': 'missing_evidence'}
    if event is None: return result
    response, predicate = event.get('response') or {}, condition['predicate']
    if event.get('outcome') not in {'received', 'invalid_json', 'invalid_content_type'} or event.get('truncated'):
        result['reason'] = event.get('outcome', 'incomplete_evidence'); return result
    if predicate == 'status_in':
        actual = response.get('status'); passed = actual in condition['parameters']['values']
    elif not response.get('json_valid'):
        result['reason'] = 'body_unavailable'; return result
    else:
        try:
            path = condition['parameters'].get('path', '')
            actual = pointer_get(response['json'], path) if path else response['json']
            if predicate == 'json_collection_matches': passed = _collection(response['json'], condition['parameters'])
            elif predicate == 'json_field_exists': passed = True
            elif predicate == 'json_value_not_equals': passed = canonical(actual) != canonical(condition['parameters']['value'])
            else:
                # 400 digits покрывают finite binary64 (до309 целых цифр)
                # и принятые18 десятичных знаков; legacy context не меняется.
                with localcontext() as context:
                    # JSON boundary допускает4096-bit integer: до1234 цифр
                    # плюс18 дробных знаков при quantize, без потери precision.
                    context.prec=1300 if manifest['checker_version']=='selected-evidence-v2' else 28
                    evaluated = evaluate_condition(descriptor, view, catalog,
                        {condition['observation_id']: event}, {},
                        numeric_decimals=18 if manifest['checker_version']=='selected-evidence-v2' else 12)
                reason={'PASS':'expectation_met','FAIL':'expectation_mismatch','UNKNOWN':'evaluation_unavailable'}[evaluated['verdict']]
                return {**evaluated, 'actual_present': True, 'reason':reason}
        except (KeyError, IndexError, ValueError, TypeError):
            result.update(verdict='FAIL', reason='field_missing'); return result
    result.update(verdict='PASS' if passed else 'FAIL', actual=display_value(actual),
        actual_present=True, reason='expectation_met' if passed else 'expectation_mismatch')
    return result


def check_selected(accepted, catalog, run):
    manifest = accepted.snapshot
    if manifest.get('contract') != CONTRACT or manifest['spec_hash'] != catalog.spec_hash:
        raise ValueError('selected_stale_manifest')
    if manifest.get('checker_version') not in {'selected-evidence-v1','selected-evidence-v2'}:
        raise ValueError('selected_unsupported_checker')
    events = run.get('exchanges', [])
    ids = [e['id'] for e in events]
    if len(set(ids)) != len(ids) or any(e.get('run_id') != run.get('run_id') for e in events):
        raise ValueError('selected_foreign_evidence')
    if manifest['mode'] == 'research':
        return {'manifest_hash': accepted.hash, 'verdict': 'N/A', 'conditions': [], 'answer_fields': {}, 'coverage': 'not_configured'}
    observations = {o['id']: o for o in manifest['observations']}
    selected = {key: _candidates(o, catalog, run) for key, o in observations.items()}
    results = []
    for condition in manifest['conditions']:
        candidates = selected[condition['observation_id']]
        parts = [_evaluate(condition, manifest, catalog, e) for e in candidates] or [_evaluate(condition, manifest, catalog, None)]
        chosen = next((v for v in parts if v['verdict'] == 'FAIL'),
            next((v for v in parts if v['verdict'] == 'UNKNOWN'), parts[-1]))
        results.append({**chosen, 'evidence_ids': [e['id'] for e in candidates],
            'scope': deepcopy(observations[condition['observation_id']])})
    verdicts = {r['verdict'] for r in results}
    verdict = 'FAIL' if 'FAIL' in verdicts else 'UNKNOWN' if 'UNKNOWN' in verdicts or not results else 'PASS'
    return {'manifest_hash': accepted.hash, 'verdict': verdict, 'conditions': results,
        'answer_fields': {}, 'coverage': 'selected_conditions'}

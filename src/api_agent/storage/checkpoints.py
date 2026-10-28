"""Версионированное состояние задачи: источники, ledger и защита эффектов."""
from copy import deepcopy

from api_agent.agent.response_views import response_page
from api_agent.bounded_json import validate_tree
from api_agent.goals.checks.compiler import PRIVATE_NAMES
from api_agent.validation import fingerprint
from api_agent.openapi.model import canonical, pointer_part


def public_data(value):
    """Дополнительная защита проекции от credential-подобных полей."""
    if isinstance(value, dict):
        return {k: public_data(v) for k, v in value.items() if k.lower() not in PRIVATE_NAMES}
    if isinstance(value, list): return [public_data(v) for v in value]
    return deepcopy(value)


def retained_scalars(value, path=''):
    """Малые значения из полного safe JSON переживают обрезку первой страницы."""
    if isinstance(value, dict):
        return {key: scalar for name, child in value.items()
                for key, scalar in retained_scalars(child, path + '/' + pointer_part(name)).items()}
    if isinstance(value, list):
        return {key: scalar for index, child in enumerate(value)
                for key, scalar in retained_scalars(child, path + '/' + str(index)).items()}
    return {path: deepcopy(value)} if len(canonical(value)) <= 256 else {}


def mutation_key(name, parameters, body):
    return fingerprint([name, parameters, body])


def scope_hash(run):
    scope = {k: run.get(k) for k in ('run_id', 'project_id', 'session_id', 'message_id',
        'goal_hash', 'target', 'spec_hash', 'admission_manifest', 'parent_run_id', 'continuation',
        'runtime_mapping_hash', 'actor_identity_hash', 'reset_epoch', 'limits')}
    snapshot = (run.get('model') or {}).get('snapshot')
    if snapshot is not None: scope['model_snapshot'] = snapshot
    return fingerprint(scope)


def reserved_active_seconds(broker, observed):
    return max([0.0, *(deadline - observed for deadline in broker.active_intervals.values())])


def build_checkpoint(broker):
    """Вызывается в одной транзакции со snapshot и последним событием."""
    run = broker.run
    prior = (run.get('continuation') or {}).get('prior_ledger', {})
    memory = (run.get('continuation') or {}).get('memory', {})
    resources = run.get('resources', {})
    active = max(0.0, broker.active_clock() - broker.active_started)
    reserve = reserved_active_seconds(broker, active)
    upper = active + reserve
    ledger = {'steps': prior.get('steps', 0) + len(run['steps']),
        'http': prior.get('http', 0) + max(broker.session.requests, run.get('http_attempt_reserved', 0)),
        'tokens': resources.get('tokens_upper_bound', prior.get('tokens', 0)),
        'active_seconds': prior.get('active_seconds', 0.0) + upper}
    checkpoint = {'schema_version': 'run-checkpoint-v2', 'run_id': run['run_id'],
        'scope_hash': scope_hash(run), 'sequence': len(run['events']), 'ledger': ledger,
        'limits': vars(broker.limits),
        'active_usage': {'own_observed_seconds': active, 'reserved_seconds': reserve,
            'inherited_upper_seconds': prior.get('active_seconds', 0.0),
            'inflight': deepcopy(broker.active_intervals)},
        'exchange_hashes': {e['id']: fingerprint(e) for e in run['exchanges']},
        'public_values': [v for v in broker.session.state.public() if not v['secret']],
        'auth_context': deepcopy(run.get('auth_feedback', {})),
        'completed_mutations': deepcopy(run.get('completed_mutations', [])),
        'pending_intent': deepcopy(run.get('pending_mutation') or memory.get('pending_intent')),
        'uncertain': bool(broker.session.uncertain or run.get('pending_mutation')
            or run.get('uncertain_write') or memory.get('uncertain'))}
    checkpoint = broker.safe(checkpoint)
    checkpoint['hash'] = fingerprint(checkpoint)
    return checkpoint


def validate_checkpoint(history, run):
    """Повреждённое состояние не становится пустой успешной памятью."""
    checkpoint = run.get('checkpoint')
    if not isinstance(checkpoint, dict) or checkpoint.get('schema_version') != 'run-checkpoint-v2':
        raise ValueError('checkpoint_missing_or_incompatible')
    if checkpoint.get('hash') != fingerprint({k: v for k, v in checkpoint.items() if k != 'hash'}):
        raise ValueError('checkpoint_hash_changed')
    if checkpoint.get('scope_hash') != scope_hash(run) or checkpoint.get('run_id') != run['run_id']:
        raise ValueError('checkpoint_scope_changed')
    if checkpoint.get('sequence') != len(run['events']): raise ValueError('checkpoint_sequence_changed')
    stored = history.db.execute('SELECT payload FROM events WHERE run_id=? ORDER BY sequence', (run['run_id'],)).fetchall()
    import json
    if [json.loads(row['payload']) for row in stored] != run['events']:
        raise ValueError('checkpoint_event_journal_changed')
    if checkpoint['exchange_hashes'] != {e['id']: fingerprint(e) for e in run['exchanges']}:
        raise ValueError('checkpoint_evidence_changed')
    return deepcopy(checkpoint)


def recovery_memory(history, run, checkpoint):
    """Ограниченные наблюдения с источниками; не полная копия старого trace."""
    previous = deepcopy((run.get('continuation') or {}).get('memory', {}))
    observations = list(previous.get('observations', []))
    for event in run['exchanges']:
        body = public_data((event.get('response') or {}).get('json'))
        view = response_page(body, max_bytes=4096)
        item = {'run_id': run['run_id'], 'exchange_id': event['id'],
            'operation_id': event['operation_id'], 'parameters': public_data(event.get('request_parameters', {})),
            'status': (event.get('response') or {}).get('status'), 'outcome': event['outcome'],
            'body': view['value'], 'complete': bool(view['complete'] and not event.get('truncated')
                and event.get('outcome') == 'received' and (event.get('response') or {}).get('json_valid')),
            'source': {'run_id': run['run_id'], 'exchange_id': event['id'], 'path': ''}}
        if not view['complete']:
            item['retained_scalars'] = retained_scalars(body)
            item['omitted_paths'] = view['omitted_paths']
            item['next_offset'] = view['next_offset']
        observations.append(item)
    errors = list(previous.get('errors', []))
    for execution in run.get('code_executions', []):
        if execution.get('error'):
            errors.append({'run_id': run['run_id'], 'execution_id': execution['execution_id'], 'error': execution['error']})
    for step in run['steps']:
        value = step.get('observation') or {}
        if value.get('error'):
            errors.append({'run_id': run['run_id'], 'step': step['index'], 'error': value['error']})
    values = {}
    for item in previous.get('public_values', []) + checkpoint['public_values']:
        key = (item['exchange_id'], item['path'], item.get('occurrence_id'))
        if key in values and canonical(values[key]['value']) != canonical(item['value']):
            raise ValueError('recovery_source_invalid')
        values[key] = item
    result = {'observations': observations, 'errors': errors,
        'inputs': {**previous.get('inputs', {}), **{key: deepcopy(value) for key, value in run.get('goal_inputs', {}).items()
            if any(item['id'] == key and not item['private'] for item in
                run['admission_manifest']['goal_contract']['manifest'].get('input_slots', []))}},
        'public_values': list(values.values()),
        'historical_checks': {'run_id': run['run_id'], 'verdict': (run.get('report') or {}).get('checks_verdict')},
        'auth_context': checkpoint['auth_context'], 'uncertain': checkpoint['uncertain'],
        'pending_intent': checkpoint['pending_intent'],
        'python_namespace': 'new; use saved values, recompute calculations; never replay completed writes'}
    validate_tree(result, max_depth=32, max_bytes=65536)
    return result

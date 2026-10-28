"""Публичные binding refs только из проверенной цепочки linked продолжения."""
from copy import deepcopy

from api_agent.openapi.model import canonical, pointer_get
from api_agent.storage.checkpoints import recovery_memory, validate_checkpoint
from api_agent.validation.selection_scope import applicable


def restore_sources(broker):
    """Не переносить старую authority: новые refs, прежний immutable provenance."""
    continuation = broker.run.get('continuation')
    if not continuation: return {}
    current = broker.run
    parent_id, expected = current.get('parent_run_id'), continuation['parent_checkpoint_hash']
    if parent_id != continuation['parent_run_id']: raise ValueError('recovery_source_lineage')
    sources, visited = {}, set()
    while parent_id:
        if parent_id in visited or len(visited) > broker.limits.max_steps:
            raise ValueError('recovery_source_lineage')
        visited.add(parent_id)
        parent = broker.history.goal_parent(current['project_id'], parent_id)
        checkpoint = validate_checkpoint(broker.history, parent)
        if len(visited) == 1:
            fixed_continuation = {'parent_run_id': parent_id,
                'root_run_id': (parent.get('continuation') or {}).get('root_run_id', parent_id),
                'parent_checkpoint_hash': checkpoint['hash'], 'prior_ledger': checkpoint['ledger'],
                'memory': recovery_memory(broker.history, parent, checkpoint),
                'completed_mutations': checkpoint['completed_mutations']}
            if parent['status'] != 'partial' or canonical(continuation) != canonical(fixed_continuation):
                raise ValueError('recovery_source_descriptor_changed')
        if (checkpoint['hash'] != expected or checkpoint['limits'] != vars(broker.limits)
                or any(parent.get(key) != current.get(key) for key in
                    ('target', 'spec_hash', 'runtime_mapping_hash', 'actor_identity_hash', 'reset_epoch', 'goal_hash', 'mode', 'policy_config'))):
            raise ValueError('recovery_source_scope')
        for exchange in parent['exchanges']:
            validation = next((v for v in parent['validations'] if v['exchange_id'] == exchange['id']), {})
            sources[exchange['id']] = (parent, checkpoint, exchange, validation)
        if parent_id == continuation['root_run_id']: break
        prior = parent.get('continuation') or {}
        parent_id, expected = prior.get('parent_run_id'), prior.get('parent_checkpoint_hash')
    if continuation['root_run_id'] not in visited: raise ValueError('recovery_source_lineage')
    restored, identities = {}, set()
    for record in continuation['memory']['public_values']:
        source = sources.get(record.get('exchange_id'))
        if source is None or record.get('secret') is not False: raise ValueError('recovery_source_invalid')
        parent, checkpoint, exchange, validation = source
        fields = ('operation_id', 'exchange_id', 'path', 'type', 'secret', 'value', 'occurrence_id')
        fixed = any(all(canonical(item.get(k)) == canonical(record.get(k)) for k in fields)
                    for item in checkpoint['public_values'])
        response = exchange.get('response') or {}
        try: actual = pointer_get(response['json'], record['path'])
        except (KeyError, IndexError, TypeError, ValueError): raise ValueError('recovery_source_invalid') from None
        if (not fixed or canonical(actual) != canonical(record['value'])
                or record['operation_id'] != exchange['operation_id'] or exchange['outcome'] != 'received'
                or exchange.get('truncated') or response.get('json_valid') is not True
                or validation.get('contract_verdict') != 'pass' or validation.get('intent_verdict') != 'pass'
                or not validation.get('task_postcondition') or not applicable(validation)):
            raise ValueError('recovery_source_invalid')
        identity = (exchange['id'], record['path'], record.get('occurrence_id'))
        if identity in identities: continue
        identities.add(identity)
        fresh = broker.session.state.restore_public(record, origin_run_id=parent['run_id'])
        restored[fresh['ref']] = {'exchange': deepcopy(exchange), 'validation': deepcopy(validation)}
    return restored


def binding_source(broker, record):
    """Исторический source разрешает binding, но не становится новым exchange/PASS."""
    restored = broker.recovered_sources.get(record['ref'])
    if restored: return restored['exchange'], restored['validation'], True
    source = next((e for e in broker.session.exchanges if e['id'] == record['exchange_id']), None)
    checked = next((v for v in broker.run['validations'] if v['exchange_id'] == record['exchange_id']), {})
    return source, checked, False

"""Закрытые validators S09a: факт выводится из закреплённого основания.

На входе только безопасная история приложения, не присланный моделью exchange.
Свободный текст не является oracle; публичный ID остаётся в trace, а не в знании
о долговременной связи операций. Здесь нет сетевых вызовов или модели.
"""
from copy import deepcopy
import math
from urllib.parse import quote, urlsplit

from .bounded_json import validate_tree
from .openapi.parser import Catalog, canonical, pointer_get
from .validation import fingerprint, validate_exchange

TYPES = {'declared_contract', 'response_observation', 'create_read_link'}
SOURCES = {'DECLARED', 'OBSERVED', 'INFERRED', 'USER_PROVIDED'}


def checked_time(value):
    """NaN/бесконечность и bool не могут отключить сроки свежести."""
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('fact_invalid_time')
    return value


def public_value(value):
    """Маска или omitted не превращается в подтверждённое исходное значение."""
    if isinstance(value, str):
        return '[REDACTED]' not in value and not value.startswith(('value-', 'binding-'))
    if isinstance(value, dict):
        return not value.get('omitted') and all(public_value(k) and public_value(v) for k, v in value.items())
    if isinstance(value, list):
        return all(public_value(v) for v in value)
    return True


def at(value, path):
    """Отсутствующий/неканонический путь даёт безопасную причину без данных."""
    try:
        return pointer_get(value, path)
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError('fact_invalid_pointer') from None


def _exchange(run, exchange_id, operation_id=None):
    found = [e for e in run['exchanges'] if e['id'] == exchange_id]
    if len(found) != 1 or found[0]['run_id'] != run['run_id']:
        raise ValueError('fact_unknown_evidence')
    exchange = found[0]
    if operation_id is not None and exchange['operation_id'] != operation_id:
        raise ValueError('fact_foreign_operation')
    if exchange['outcome'] != 'received' or exchange.get('truncated') or not exchange.get('response'):
        raise ValueError('fact_incomplete_evidence')
    if not public_value(exchange['request']):
        raise ValueError('fact_private_request')
    return exchange


def _predicate(catalog, run, exchange, expectation_id):
    matches = [e for e in run['expectations'] if e['id'] == expectation_id
               and e['operation_id'] == exchange['operation_id']]
    if len(matches) != 1:
        raise ValueError('fact_unknown_expectation')
    condition = matches[0]
    if condition['source'] not in {'CONTRACT', 'USER_DEFINED'} or not public_value(condition):
        raise ValueError('fact_untrusted_condition')
    # Полный response используется заново; сохранённый validation verdict не oracle.
    validation = validate_exchange(catalog, exchange, [condition])
    if not validation['complete_response'] or validation['checks'][0]['verdict'] != 'pass':
        raise ValueError('fact_predicate_not_verified')
    return condition


def observation_scope(exchange, condition):
    """Сравнимость требует тех же запроса, principal и формы утверждения."""
    params = condition['parameters']
    request = {'request': exchange['request'], 'parameters': exchange.get('request_parameters', {}),
               'principal': exchange.get('request_principal', {})}
    return {'hash': fingerprint(request), 'predicate': condition['predicate'],
            'path': params.get('path', params.get('schema_pointer'))}


def verify_fact(run, snapshot, descriptor, stamp, policy):
    """Проверить один закрытый proposal и вывести весь сохраняемый DTO кодом."""
    validate_tree(descriptor, max_bytes=8192)
    if not isinstance(descriptor, dict) or set(descriptor) != {'type', 'source', 'operation_id', 'verify'}:
        raise ValueError('fact_invalid_descriptor')
    kind, source, operation, verify = (descriptor[k] for k in ('type', 'source', 'operation_id', 'verify'))
    if not all(isinstance(v, str) for v in (kind, source, operation)) or kind not in TYPES or source not in SOURCES or not isinstance(verify, dict):
        raise ValueError('fact_unsupported_descriptor')
    catalog = Catalog(canonical(snapshot['contract']))
    if catalog.spec_hash != run['spec_hash'] or operation not in catalog.operations:
        raise ValueError('fact_snapshot_mismatch')
    if not public_value(descriptor):
        raise ValueError('fact_private_descriptor')
    dependencies = {operation: catalog.operation_hashes[operation]}
    evidence = []
    condition = None
    scope = None
    expected_source = 'DECLARED' if kind == 'declared_contract' else 'OBSERVED'
    if source in {'DECLARED', 'OBSERVED'} and source != expected_source:
        raise ValueError('fact_source_mismatch')
    if kind == 'declared_contract':
        if set(verify) != {'pointer'}:
            raise ValueError('fact_invalid_descriptor')
        value = at(catalog.operations[operation], verify['pointer'])
        basis = {'spec_hash': catalog.spec_hash, 'operation_hash': dependencies[operation],
                 'pointer': verify['pointer']}
    else:
        if stamp is None or stamp['reset_epoch'] != policy['reset_epoch']:
            raise ValueError('fact_stale_evidence')
        basis = {'spec_hash': catalog.spec_hash, 'verify': deepcopy(verify)}
        if kind == 'response_observation':
            if set(verify) != {'exchange_id', 'expectation_id'}:
                raise ValueError('fact_invalid_descriptor')
            exchange = _exchange(run, verify['exchange_id'], operation)
            condition = _predicate(catalog, run, exchange, verify['expectation_id'])
            predicate, parameters = condition['predicate'], condition['parameters']
            if predicate == 'status_in':
                value = exchange['response']['status']
            elif predicate == 'json_value_equals':
                path = parameters['path']
                # Извлечённый runtime ID не сохраняется как стабильное знание.
                extraction = snapshot['metadata'].get('extraction', {}).get(operation, {})
                if path in extraction or path.rsplit('/', 1)[-1].lower() in {'id', 'token', 'password', 'secret'}:
                    raise ValueError('fact_runtime_value')
                value = at(exchange['response']['json'], path)
                if isinstance(value, (dict, list)):
                    raise ValueError('fact_unsupported_value')
            else:
                # Закрытые schema/required predicates дают только scoped boolean.
                if predicate not in {'json_valid', 'content_type_is_json', 'required_fields_present', 'supported_schema_types'}:
                    raise ValueError('fact_unsupported_predicate')
                value = True
            evidence = [exchange['id']]
            scope = observation_scope(exchange, condition)
        else:
            required = {'create_exchange_id', 'read_exchange_id', 'create_id_path', 'read_id_path',
                        'request_parameter', 'expectation_id'}
            if set(verify) != required:
                raise ValueError('fact_invalid_descriptor')
            created = _exchange(run, verify['create_exchange_id'])
            read = _exchange(run, verify['read_exchange_id'], operation)
            create_op = catalog.operations[created['operation_id']]
            read_op = catalog.operations[operation]
            exchanges = run['exchanges']
            if create_op['method'] != 'POST' or read_op['method'] != 'GET' or exchanges.index(created) >= exchanges.index(read):
                raise ValueError('fact_invalid_create_read')
            dependencies[created['operation_id']] = catalog.operation_hashes[created['operation_id']]
            parameter = verify['request_parameter']
            if not any(p['name'] == parameter and p['in'] == 'path' for p in read_op['parameters']):
                raise ValueError('fact_invalid_request_parameter')
            identifier = at(created['response'].get('json'), verify['create_id_path'])
            actual = at(read['response'].get('json'), verify['read_id_path'])
            if type(identifier) not in (int, str) or not identifier or canonical(identifier) != canonical(actual):
                raise ValueError('fact_identity_mismatch')
            route = read_op['path'].replace('{' + parameter + '}', quote(str(identifier), safe=''))
            if '{' in route or urlsplit(read['request']['url']).path != route:
                raise ValueError('fact_identity_mismatch')
            binding = snapshot['metadata'].get('binding_rules', {}).get(operation, {}).get(parameter)
            extraction = snapshot['metadata'].get('extraction', {}).get(created['operation_id'], {}).get(verify['create_id_path'])
            if binding != {'source_operation': created['operation_id'], 'source_path': verify['create_id_path']} or not extraction or extraction.get('secret'):
                raise ValueError('fact_untrusted_binding')
            if read.get('request_parameters', {}).get(parameter) != identifier:
                raise ValueError('fact_identity_mismatch')
            condition = _predicate(catalog, run, read, verify['expectation_id'])
            body, response = created['request'].get('json'), read['response'].get('json')
            if not isinstance(body, dict) or not body or not isinstance(response, dict):
                raise ValueError('fact_content_unavailable')
            # Минимальный поддержанный профиль: публичные поля create body в read.
            if any(k not in response or canonical(response[k]) != canonical(v) for k, v in body.items()):
                raise ValueError('fact_content_mismatch')
            if validate_exchange(catalog, created, [])['contract_verdict'] != 'pass' or validate_exchange(catalog, read, [])['contract_verdict'] != 'pass':
                raise ValueError('fact_content_mismatch')
            evidence = [created['id'], read['id']]
            value = {k: verify[k] for k in ('create_id_path', 'read_id_path', 'request_parameter')}
            value.update(create_operation=created['operation_id'], read_operation=operation)
            scope = {'content_fields': sorted(body), 'request_scope': 'these_two_exchanges'}
    if not public_value(value) or not public_value(condition):
        raise ValueError('fact_private_value')
    validate_tree(value, max_bytes=4096)
    verified_at = stamp['started_at'] if kind != 'declared_contract' else None
    return {'id': 'fact-' + fingerprint([run['run_id'], descriptor]), 'type': kind, 'source': source,
        'status': 'VERIFIED' if source == expected_source else 'CANDIDATE',
        'value': deepcopy(value), 'condition': deepcopy(condition), 'basis': basis,
        'dependencies': dependencies, 'evidence_ids': evidence, 'verify': deepcopy(verify),
        'origin': {'project_id': run['project_id'], 'session_id': run['session_id'], 'run_id': run['run_id']},
        'last_verified_at': verified_at,
        'expires_at': verified_at + policy[kind] if verified_at is not None else None,
        'reset_epoch': stamp['reset_epoch'] if verified_at is not None else None,
        'scope': scope, 'freshness': 'current'}


def contradict_fact(fact, run, snapshot, stamp, policy, exchange_id):
    """INVALIDATED требует явной команды и сопоставимого опровергающего HTTP."""
    if fact['type'] != 'response_observation' or fact['status'] != 'VERIFIED' or stamp is None:
        raise ValueError('fact_not_contradicted')
    if stamp['reset_epoch'] != fact['reset_epoch'] or stamp['reset_epoch'] != policy['reset_epoch'] or stamp['started_at'] <= fact['last_verified_at']:
        raise ValueError('fact_stale_evidence')
    catalog = Catalog(canonical(snapshot['contract']))
    if any(catalog.operation_hashes.get(op) != digest for op, digest in fact['dependencies'].items()):
        raise ValueError('fact_stale_evidence')
    operation = fact['condition']['operation_id']
    exchange = _exchange(run, exchange_id, operation)
    if observation_scope(exchange, fact['condition']) != fact['scope']:
        raise ValueError('fact_not_comparable')
    accepted = [c for c in run['expectations'] if c['operation_id'] == operation
                and c['source'] in {'CONTRACT', 'USER_DEFINED'} and c['preconditions'] == 'verified'
                and c['predicate'] == fact['condition']['predicate']
                and canonical(c['parameters']) == canonical(fact['condition']['parameters'])]
    if not accepted or not public_value(exchange['response']):
        raise ValueError('fact_not_contradicted')
    result = validate_exchange(catalog, exchange, [fact['condition']])
    if result['checks'][0]['verdict'] != 'fail':
        raise ValueError('fact_not_contradicted')
    updated = deepcopy(fact)
    updated.update(status='INVALIDATED', invalidation={'run_id': run['run_id'],
                   'exchange_id': exchange_id, 'verified_at': stamp['started_at']})
    return updated


def automatic_proposals(run, snapshot):
    """Предлагать лишь ограниченные формы из реальных операций и условий."""
    operations = snapshot['configuration'].get('operations', {})
    for operation in operations:
        if operation in run['contract']['operations']:
            yield {'type': 'declared_contract', 'source': 'DECLARED', 'operation_id': operation,
                   'verify': {'pointer': '/security'}}
    for exchange in run['exchanges']:
        for expectation in run['expectations']:
            if expectation['operation_id'] == exchange['operation_id']:
                yield {'type': 'response_observation', 'source': 'OBSERVED', 'operation_id': exchange['operation_id'],
                       'verify': {'exchange_id': exchange['id'], 'expectation_id': expectation['id']}}
        bindings = snapshot['metadata'].get('binding_rules', {}).get(exchange['operation_id'], {})
        for parameter, binding in bindings.items():
            for created in run['exchanges']:
                if created['operation_id'] == binding['source_operation']:
                    for expectation in run['expectations']:
                        if expectation['operation_id'] == exchange['operation_id']:
                            yield {'type': 'create_read_link', 'source': 'OBSERVED', 'operation_id': exchange['operation_id'],
                                'verify': {'create_exchange_id': created['id'], 'read_exchange_id': exchange['id'],
                                    'create_id_path': binding['source_path'], 'read_id_path': binding['source_path'],
                                    'request_parameter': parameter, 'expectation_id': expectation['id']}}


def fact_view(fact, catalog, policy, now):
    """Свежесть — проекция; исходное наблюдение/evidence остаётся неизменным."""
    value = deepcopy(fact)
    freshness = 'current'
    if any(catalog.operation_hashes.get(op) != digest for op, digest in fact['dependencies'].items()):
        freshness = 'operation_changed'
    elif fact['type'] != 'declared_contract':
        value['expires_at'] = min(fact['expires_at'], fact['last_verified_at'] + policy[fact['type']])
        if fact['reset_epoch'] != policy['reset_epoch']:
            freshness = 'reset'
        elif now < fact['last_verified_at']:
            freshness = 'clock_before_evidence'
        elif now >= value['expires_at']:
            freshness = 'expired'
    value['freshness'] = freshness
    if freshness != 'current' and value['status'] == 'VERIFIED':
        value['status'] = 'STALE'
    return value

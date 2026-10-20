"""Закрытые runtime descriptors: основание относится к конкретным inputs."""
from copy import deepcopy
from ..bounded_json import validate_tree
from ..openapi.parser import canonical, pointer_get
from ..policy import PolicyStop
from ..validation.selection_scope import applicable


def prepare_admission(rules, allowed, occurrence_ids):
    """Принять application descriptors до сети, без вычислений/кода модели."""
    validate_tree(rules, max_bytes=16384)
    if not isinstance(rules, dict) or not set(rules) <= set(allowed) | set(occurrence_ids):
        raise ValueError('invalid_admission_rules')
    for rule in rules.values():
        if not isinstance(rule, dict) or set(rule) - {'target', 'witnesses'}:
            raise ValueError('invalid_admission_rule')
        witnesses = rule.get('witnesses', [])
        if not isinstance(witnesses, list) or len(witnesses) > 16:
            raise ValueError('invalid_admission_witness')
        for witness in witnesses:
            if (not isinstance(witness, dict) or set(witness) -
                {'source_operation', 'source_occurrence', 'path', 'equals', 'complete_path', 'parameters'}
                or not {'source_operation', 'path', 'equals'} <= set(witness)
                or witness['source_operation'] not in allowed):
                raise ValueError('invalid_admission_witness')
            if isinstance(witness['equals'], list) and not witness.get('complete_path'):
                raise ValueError('collection_completeness_required')
        target = rule.get('target')
        if target is not None and (not isinstance(target, dict) or
            set(target) - {'parameter', 'body_path', 'source_operation', 'source_occurrence', 'source_path'}
            or not {'source_operation', 'source_path'} <= set(target)
            or ('parameter' in target) == ('body_path' in target)
            or target['source_operation'] not in allowed):
            raise ValueError('invalid_target_binding')
    return deepcopy(rules)


def source_exchange(session, descriptor):
    """Только полный, оценённый и применимый exchange своего Run и actor."""
    candidates = [e for e in session.exchanges if e['run_id'] == session.run_id
        and e['operation_id'] == descriptor['source_operation']
        and ('source_occurrence' not in descriptor or e.get('occurrence_id') == descriptor['source_occurrence'])]
    if len(candidates) != 1:
        return None
    exchange = candidates[0]
    response = exchange.get('response') or {}
    validation = next((v for v in session.run.get('validations', []) if v['exchange_id'] == exchange['id']), None)
    if (exchange.get('outcome') != 'received' or exchange.get('truncated')
        or response.get('json_valid') is not True or validation is None
        or validation.get('contract_verdict') != 'pass' or validation.get('intent_verdict') != 'pass'
        or not validation.get('task_postcondition') or not applicable(validation)):
        return None
    if 'parameters' in descriptor and canonical(exchange.get('request_parameters')) != canonical(descriptor['parameters']):
        return None
    # Локальные schema/status PASS не доказывают, что ответ относится к
    # запрошенной сущности. Та же проверка identity нужна ДО зависимого HTTP,
    # а не только при последующем completion/report всей цепочки.
    try:
        identity = session.workflow.binding_check(session, exchange)
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if identity is not None and identity.get('verdict') != 'pass':
        return None
    source = exchange.get('request_principal', {})
    current = session._request_principal
    if source.get('mode') == 'authenticated' and any(source.get(k) != current.get(k) for k in ('actor', 'auth_epoch')):
        return None
    return exchange


def admit_conditions(session, name, args, parameters, body):
    """False и unknown останавливают зависимое действие до approval и HTTP."""
    keys = [name, args.get('occurrence_id')]
    for key in keys:
        rule = session.workflow.admission_rules.get(key, {})
        target = rule.get('target')
        if target is not None:
            source = source_exchange(session, target)
            try:
                actual = parameters[target['parameter']] if 'parameter' in target else pointer_get(body, target['body_path'])
                expected = pointer_get(source['response']['json'], target['source_path']) if source else None
            except (KeyError, TypeError, ValueError, IndexError):
                raise PolicyStop('target_not_applicable') from None
            if source is None or canonical(actual) != canonical(expected):
                raise PolicyStop('target_not_applicable')
        for witness in rule.get('witnesses', []):
            source = source_exchange(session, witness)
            try:
                data = source['response']['json']
                valid = canonical(pointer_get(data, witness['path'])) == canonical(witness['equals'])
                if 'complete_path' in witness:
                    valid = valid and pointer_get(data, witness['complete_path']) is True
            except (KeyError, TypeError, ValueError, IndexError):
                valid = False
            if not valid:
                raise PolicyStop('precondition_not_verified')

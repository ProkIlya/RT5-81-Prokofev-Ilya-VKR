"""Область credential для условий: не план, не доступ модели к Bearer."""
from api_agent.validation import validate_exchange


def _confirmed_logout(catalog, exchange):
    status = (exchange.get('response') or {}).get('status')
    return (exchange.get('outcome') == 'received' and not exchange.get('truncated')
        and type(status) is int and 200 <= status < 300
        and validate_exchange(catalog, exchange, [])['contract_verdict'] == 'pass')


def validate_auth_scope(value, catalog, name):
    scope = value.get('auth_scope')
    if 'auth_scope' not in value: return None
    if not isinstance(scope, str) or scope not in {'current', 'revoked'} or catalog.operations[name]['method'] != 'GET':
        raise ValueError('goal_invalid_auth_scope')
    return scope


def validate_auth_observations(observations, conditions):
    for observation in observations:
        scope = observation.get('auth_scope')
        if scope is None: continue
        if any(o['operation_id'] == observation['operation_id'] and 'auth_scope' not in o for o in observations):
            raise ValueError('goal_ambiguous_auth_scope')
        if scope == 'revoked':
            statuses = [c['parameters']['values'] for c in conditions
                if c['observation_id'] == observation['id'] and c['predicate'] == 'status_in']
            if statuses != [[401]]: raise ValueError('goal_invalid_auth_scope')








def auth_candidate(observation, exchange, run, index, principal, catalog):
    """Проверка actor/epoch по сохранённым событиям, без вывода из статуса 401."""
    scope = observation.get('auth_scope')
    if scope is None: return True
    actor = exchange.get('request_principal') or {}
    if scope == 'current':
        return (actor.get('mode') == 'authenticated' and actor.get('actor') is not None
            and actor.get('actor') == principal.get('principal')
            and actor.get('auth_epoch') == principal.get('auth_epoch'))
    if (actor.get('mode') != 'old_epoch' or actor.get('actor') is None
        or type(actor.get('auth_epoch')) is not int or type(principal.get('auth_epoch')) is not int
        or actor['auth_epoch'] >= principal['auth_epoch']
        or principal.get('principal') not in (None, actor['actor'])): return False
    # Имена logout берутся из pinned application mapping, не из предметной области.
    exchanges = run.get('exchanges', [])
    metadata = (run.get('admission_manifest') or {}).get('goal_contract', {}).get('auth_scope_mapping', {})
    logouts = [e for e in exchanges[:index] if e['operation_id'] in metadata.get('logout_operations', [])]
    if not logouts: return False
    last = logouts[-1]; before = last.get('request_principal') or {}
    return (_confirmed_logout(catalog, last) and before.get('mode') == 'authenticated'
        and before.get('actor') == actor['actor'] and before.get('auth_epoch') == actor.get('auth_epoch'))

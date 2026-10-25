"""Внутренние credentials/epochs выводятся из действий, не выбираются моделью."""
from copy import deepcopy


def resolve_request_plan(catalog, slots, conditions, auth=None):
    auth = auth or {}
    epoch, token_epoch, logout_slot = 0, None, None
    result = deepcopy(slots)
    fields = {'auth_mode', 'actor', 'auth_epoch'}
    for slot in result:
        operation = catalog.operations.get(slot.get('operation_id'))
        if operation is None: raise ValueError('goal_foreign_operation')
        expected = set()
        for condition in conditions:
            if condition.get('slot') != slot.get('id') or condition.get('predicate') != 'status_in': continue
            parameters = condition.get('parameters')
            values = parameters.get('values') if isinstance(parameters, dict) else None
            if not isinstance(values, list) or any(type(v) is not int for v in values):
                raise ValueError('goal_invalid_status')
            expected.update(values)
        present = fields & slot.keys()
        if present and present != fields: raise ValueError('goal_invalid_actor')
        protected = bool(operation['security']) and all(bool(r) for r in operation['security'])
        if not present:
            old = (protected and expected == {401} and token_epoch is not None
                and logout_slot in slot.get('depends_on', []))
            active = token_epoch is not None and token_epoch == epoch
            login = slot['operation_id'] == auth.get('login_operation')
            mode = 'old_epoch' if old else ('authenticated'
                if (protected or active) and expected != {401} and not login else 'anonymous')
            slot.update(auth_mode=mode, actor='current_user' if mode in {'authenticated', 'old_epoch'} else 'anonymous',
                auth_epoch=token_epoch if old else epoch)
        success = bool(expected) and all(200 <= s < 300 for s in expected)
        if slot['operation_id'] == auth.get('login_operation') and success:
            epoch += 1
            token_epoch, logout_slot = epoch, None
        elif slot['operation_id'] in auth.get('logout_operations', []) and success:
            epoch += 1
            logout_slot = slot['id']
    return result

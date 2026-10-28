"""Явный допуск открытых тестовых паролей по указанию владельца, 22.10.2026."""
from api_agent.execution.state import TestPassword


def authorize(config, mappings, passwords):
    """Допуск только явно переданных password fields тестовой resettable среды."""
    if not isinstance(passwords, dict) or not 1 <= len(passwords) <= 8:
        raise ValueError('goal_invalid_test_passwords')
    if config.snapshot['mode'] != 'TEST' or not config.snapshot['resettable']:
        raise ValueError('goal_test_password_scope')
    bodies = [mappings.get('auth', {}).get('body', {}), *mappings.get('private_bodies', {}).values()]
    available = {body['password'] for body in bodies if isinstance(body.get('password'), str)}
    if any(not isinstance(k, str) or not k or not isinstance(v, str) or not v or v not in available
           for k, v in passwords.items()): raise ValueError('goal_test_password_scope')
    marked = {v: TestPassword(v) for v in passwords.values()}
    for body in bodies:
        if body.get('password') in marked: body['password'] = marked[body['password']]
    return marked

"""Проекция фактического состояния Run, без плана и подстановки ответов модели."""
from ..goals.compiler import PRIVATE_NAMES
from ..state.sensitive import TestPassword
from ..validation import display_value


def execution_state(broker, names, types, requests_complete):
    """Журнал HTTP и worker inventory — данные, а не доказательство успеха цели."""
    session = broker.session.state
    auth = broker.workflow.auth or {}
    authenticated = session.principal is not None
    private_bodies = getattr(broker.workflow, 'private_bodies', {})
    validations = {v['exchange_id']: v for v in broker.run['validations']}
    calls = []
    for index, exchange in enumerate(broker.run['exchanges'], 1):
        name = exchange['operation_id']
        operation = broker.artifact.manifest['operations'][name]
        supplied = exchange.get('request_parameters', {})
        arguments = {}
        for parameter in operation['parameter_mapping']:
            key = parameter['in'] + '__' + parameter['name']
            key = key if key in supplied else parameter['name']
            if key in supplied and parameter['name'].lower() not in PRIVATE_NAMES:
                arguments[parameter['python_name']] = supplied[key]
        body = (exchange.get('request') or {}).get('json')
        def public(value, root=False):
            if isinstance(value, dict):
                return {k: public(v) for k, v in value.items() if k.lower() not in PRIVATE_NAMES
                    or root and isinstance(private_bodies.get(name, {}).get(k), TestPassword)}
            if isinstance(value, list): return [public(v) for v in value]
            return value
        # Тело login принадлежит приложению, Python действительно вызывался без него.
        if body is not None and name != auth.get('login_operation'):
            arguments['body'] = public(body, root=True)
        response = exchange.get('response') or {}
        status = response.get('status')
        validation = validations.get(exchange['id'], {})
        has_error = (exchange.get('outcome') not in {'received', 'invalid_json', 'invalid_content_type'}
            or exchange.get('truncated', False) or type(status) is not int or status >= 400
            or validation.get('contract_verdict') == 'fail' or validation.get('intent_verdict') == 'fail')
        calls.append({'index': index, 'function': 'api.' + operation['python_name'],
            'arguments': display_value(arguments), 'status': status, 'outcome': exchange['outcome'],
            'has_error': bool(has_error),
            **{k: validation[k] for k in ('contract_verdict', 'intent_verdict') if k in validation}})
    private = bool(broker.workflow.secrets())
    return broker.safe({'auth': authenticated, 'auth_session': {'authenticated': authenticated,
        'principal': session.principal, 'auth_epoch': session.auth_epoch,
        'account_inputs': {key: auth.get('body', {})[key] for key in auth.get('public_body_fields', [])
            if authenticated and key in auth.get('body', {}) and key.lower() not in PRIVATE_NAMES},
        'role': None}, 'api_calls': calls,
        'workspace_variables': {'names': [] if private else names, 'types': {} if private else types, 'limit': 32},
        'requests_complete': requests_complete})

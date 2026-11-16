"""Раздельные схема входа и разовые данные из цели, без сетевых действий."""
from api_agent.openapi.model import pointer_part
from api_agent.policy.config import decision
from api_agent.setup.credentials import compile_connection
from api_agent.execution.session_cookie import cookie_name_valid, cookie_schemes


MASK = '[пароль скрыт]'
_QUOTES = {'`': '`', '"': '"', '«': '»', "'": "'"}
_SIGN_IN = {'войди', 'войти', 'войдите', 'авторизуйся', 'авторизоваться', 'авторизуйтесь'}
_LOGIN_FIELDS = {'login', 'username', 'user', 'account', 'email', 'identifier'}
_PASSWORD_FIELDS = {'password', 'passphrase', 'passwd', 'pass', 'secret'}
_TOKEN_FIELDS = {'token', 'accesstoken', 'access', 'bearertoken', 'jwt'}


def _horizontal_space(text, index):
    while index < len(text) and text[index] in ' \t':
        index += 1
    return index


def _operand(text, index):
    start = _horizontal_space(text, index)
    if text[start:start + 1] in {':', '='}:
        start = _horizontal_space(text, start + 1)
    if text.startswith(MASK, start):
        return start, start + len(MASK), None
    quote = _QUOTES.get(text[start:start + 1])
    end = start
    if quote:
        end += 1
        while end < len(text) and text[end] not in {quote, '\r', '\n'}:
            end += 1
        if text[end:end + 1] == quote:
            return start, end + 1, text[start + 1:end] or None
        return start, end, None
    while end < len(text) and not text[end].isspace() and text[end] not in ',;':
        end += 1
    while end > start and text[end - 1] == '.':
        end -= 1
    return start, end, text[start:end] or None


def _credential_parts(text):
    """Разбирать явные поля, пропуская содержимое их значений."""
    index, previous, before_previous = 0, '', ''
    logins, passwords = [], []
    while index < len(text):
        if not text[index].isalpha():
            index += 1
            continue
        start = index
        while index < len(text) and text[index].isalpha():
            index += 1
        word = text[start:index].lower()
        follows_sign_in = previous in _SIGN_IN or previous == 'с' and before_previous in _SIGN_IN
        if word in {'логин', 'логином'} and follows_sign_in:
            item = _operand(text, index)
            logins.append(item)
            index = item[1]
        elif word in {'пароль', 'паролем'}:
            marker = text[_horizontal_space(text, index):][:1]
            if word == 'паролем' or previous in {'тестовый', 'тестовым'} or marker in {':', '=', '[', *_QUOTES}:
                item = _operand(text, index)
                passwords.append(item)
                index = item[1]
        before_previous, previous = previous, word
    return logins, passwords


def goal_login(text):
    """Маскировать в том числе незакрытый ввод и возвращать только одну явную пару."""
    logins, passwords = _credential_parts(text)
    safe = text
    for start, end, _ in reversed(passwords):
        if end > start:
            safe = safe[:start] + MASK + safe[end:]
    if len(logins) == len(passwords) == 1:
        login, password = logins[0][2], passwords[0][2]
        if login and password:
            return safe.replace(password, MASK) if len(password) >= 4 else safe, (login, password)
    return safe, None


def explicit_login(text):
    return bool(_credential_parts(text)[0])


def login_operations(catalog):
    return {name: op for name, op in catalog.operations.items()
            if op['method'] == 'POST' and (not op['security'] or any(not scheme for scheme in op['security']))
            and not any(p.get('required') for p in op['parameters']) and _body(op).get('type') == 'object'}


def _body(operation):
    return operation.get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema', {})


def string_paths(schema, prefix=''):
    if schema.get('type') == 'string' and prefix:
        return [prefix]
    if schema.get('type') != 'object':
        return []
    return [path for name, field in schema.get('properties', {}).items()
            for path in string_paths(field, prefix + '/' + pointer_part(name))]


def token_paths(operation):
    statuses = sorted(int(code) for code in operation['responses'] if code.isdigit() and 200 <= int(code) < 300)
    if not statuses:
        return []
    schema = operation['responses'][str(statuses[0])].get('content', {}).get('application/json', {}).get('schema', {})
    return string_paths(schema)


def login_fields(operation):
    return [name for name, value in _body(operation).get('properties', {}).items() if value.get('type') == 'string']


def login_cookies(catalog, policy=None):
    return {cookie for name, operation in catalog.operations.items()
            if policy is None or decision(policy.snapshot, name, catalog.operation_hashes[name]) != 'DENY'
            for cookie in cookie_schemes(operation) if cookie_name_valid(cookie)}


def validate_login_schema(catalog, schema):
    common = {'operation', 'login_field', 'password_field'}
    if not isinstance(schema, dict) or set(schema) - common not in ({'token_path'}, {'cookie_name'}) or not common <= schema.keys():
        raise ValueError('connection_login_schema_invalid')
    if any(not isinstance(value, str) for value in schema.values()):
        raise ValueError('connection_login_schema_invalid')
    name, login, password = (schema[key] for key in ('operation', 'login_field', 'password_field'))
    operation = login_operations(catalog).get(name)
    if operation is None or login == password or {login, password} - set(login_fields(operation)):
        raise ValueError('connection_login_schema_invalid')
    if set(_body(operation).get('required', [])) - {login, password}:
        raise ValueError('connection_login_schema_invalid')
    if 'cookie_name' in schema:
        if schema['cookie_name'] not in login_cookies(catalog) or not any(
                code.isdigit() and 200 <= int(code) < 300 for code in operation['responses']):
            raise ValueError('connection_cookie_invalid')
    elif schema['token_path'] not in token_paths(operation):
        raise ValueError('connection_login_schema_invalid')
    return schema


def _normalized(value):
    return ''.join(character for character in value.lower() if 'a' <= character <= 'z')


def _candidate(name, operation, cookies):
    fields = login_fields(operation)
    login = [field for field in fields if _normalized(field) in _LOGIN_FIELDS]
    password = [field for field in fields if _normalized(field) in _PASSWORD_FIELDS]
    tokens = [('token_path', path) for path in token_paths(operation)
              if _normalized(path.rsplit('/', 1)[-1]) in _TOKEN_FIELDS]
    declares_cookie = any('Set-Cookie' in response.get('headers', {}) for response in operation['responses'].values())
    if declares_cookie or not tokens:
        tokens.extend(('cookie_name', cookie) for cookie in cookies)
    if len(login) == len(password) == len(tokens) == 1:
        key, value = tokens[0]
        return {'operation': name, 'login_field': login[0], 'password_field': password[0], key: value}
    return None


def discover_login(catalog, policy):
    choices = []
    cookies = login_cookies(catalog, policy)
    for name, operation in login_operations(catalog).items():
        route = _normalized(operation['path'].rstrip('/').rsplit('/', 1)[-1])
        if (route not in {'login', 'signin', 'authenticate'}
                and _normalized(name) not in {'login', 'loginuser', 'signin', 'authenticate', 'authenticateuser'}):
            continue
        if decision(policy.snapshot, name, catalog.operation_hashes[name]) == 'DENY':
            continue
        candidate = _candidate(name, operation, cookies)
        if candidate and not set(_body(operation).get('required', [])) - {candidate['login_field'], candidate['password_field']}:
            choices.append(candidate)
    if len(choices) != 1:
        raise ValueError('connection_login_schema_choice_required')
    return choices[0]


def legacy_login_schema(catalog, auth):
    operation = login_operations(catalog).get(auth.get('login_operation'))
    if operation is None:
        return None
    fields = auth.get('body', {})
    login = [name for name in fields if _normalized(name) in _LOGIN_FIELDS]
    password = [name for name in fields if _normalized(name) in _PASSWORD_FIELDS]
    if len(login) == len(password) == 1:
        credential = 'cookie_name' if 'cookie_name' in auth else 'token_path'
        return {'operation': auth['login_operation'], 'login_field': login[0],
                'password_field': password[0], credential: auth[credential]}
    return None


def authorized_login_schema(catalog, policy, schema):
    schema = validate_login_schema(catalog, schema)
    name = schema['operation']
    if decision(policy.snapshot, name, catalog.operation_hashes[name]) == 'DENY':
        raise ValueError('connection_login_policy_denied')
    if 'cookie_name' in schema and schema['cookie_name'] not in login_cookies(catalog, policy):
        raise ValueError('connection_login_policy_denied')
    return schema


def login_mapping(catalog, policy, schema, account):
    schema = authorized_login_schema(catalog, policy, schema)
    login, password = account
    if not isinstance(login, str) or not login.strip() or not isinstance(password, str) or not password:
        raise ValueError('connection_goal_credentials_missing')
    result = compile_connection(catalog, schema['operation'], schema.get('token_path'),
                                {schema['login_field']: login, schema['password_field']: password},
                                cookie_name=schema.get('cookie_name'))
    result['auth']['public_body_fields'] = [schema['login_field']]
    return result

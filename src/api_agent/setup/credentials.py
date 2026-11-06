"""Явный generic login и локальное хранение credential через Windows DPAPI."""
import base64
import ctypes
import hashlib
import json
import os
import re
from pathlib import Path
import uuid

from api_agent.openapi.model import canonical
from api_agent.validation.schema import schema_issues


def _credential_schema(schema, path):
    """Закрытый locator секрета: не публичный predicate/attachment path."""
    if not isinstance(path, str) or not path.startswith('/') or len(path) > 500 or re.search(r'~(?![01])', path):
        raise ValueError('connection_token_path_invalid')
    for raw in path[1:].split('/'):
        part = raw.replace('~1', '/').replace('~0', '~')
        if not isinstance(schema, dict): raise ValueError('connection_token_path_invalid')
        if schema.get('type') == 'array' and re.fullmatch(r'0|[1-9][0-9]*', part): schema = schema.get('items')
        else: schema = schema.get('properties', {}).get(part)
    if not isinstance(schema, dict): raise ValueError('connection_token_path_invalid')
    return schema


def compile_connection(catalog, operation, token_path, body):
    """Операция/путь выбраны человеком; пароль и токен не выводятся из имён."""
    op = catalog.operations.get(operation)
    if op is None or op['method'] != 'POST' or any(p.get('required') for p in op['parameters']):
        raise ValueError('connection_login_operation_invalid')
    schema = op.get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema')
    if schema is None or schema.get('type') != 'object' or not isinstance(body, dict) or schema_issues(body, schema):
        raise ValueError('connection_body_invalid')
    successes = [int(s) for s in op['responses'] if s.isdigit() and 200 <= int(s) < 300]
    if not successes: raise ValueError('connection_token_path_invalid')
    response = op['responses'][str(min(successes))].get('content', {}).get('application/json', {}).get('schema', {})
    try: field = _credential_schema(response, token_path)
    except ValueError: raise ValueError('connection_token_path_invalid') from None
    if field.get('type') != 'string': raise ValueError('connection_token_path_invalid')
    if len(canonical(body)) > 8192: raise ValueError('connection_body_invalid')
    return {'auth': {'login_operation': operation, 'token_path': token_path, 'body': body,
                      'public_body_fields': []},
            'extraction': {operation: {token_path: {'type': 'string', 'secret': True}}}}


def _crypt(raw, scope, *, decrypt=False):
    if os.name != 'nt': raise ValueError('connection_secure_storage_unavailable')
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    def blob(data):
        buffer = ctypes.create_string_buffer(data)
        return Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer
    data, data_buffer = blob(raw)
    entropy, entropy_buffer = blob(hashlib.sha256(b'api-agent-connection-v1' + canonical(scope)).digest())
    output = Blob()
    library = ctypes.WinDLL('crypt32', use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(data), None, ctypes.byref(entropy), None, None, 1, ctypes.byref(output)):
        raise ValueError('connection_secure_storage_failed')
    try: return ctypes.string_at(output.data, output.size)
    finally:
        free = ctypes.WinDLL('kernel32').LocalFree
        free.argtypes = [ctypes.c_void_p]; free.restype = ctypes.c_void_p
        free(output.data)


def save_connection(path, mappings, scope):
    raw = canonical(mappings)
    if len(raw) > 16384: raise ValueError('connection_body_invalid')
    sealed = _crypt(raw, scope)
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(canonical({'schema_version': 'local-connection-v1',
                                         'data': base64.b64encode(sealed).decode('ascii')}))
        os.replace(temporary, path)
    finally:
        if temporary.exists(): temporary.unlink()


def project_connection(values, previous, connection, *, path, scope):
    """Один выбранный источник; отсутствие нового выбора сохраняет прежний вход."""
    result = dict(values)
    result.pop('connection_path', None)
    if connection is not None:
        # Явный результат формы заменяет импорт; {} означает отключить оба источника.
        result.pop('mappings_path', None)
        if connection:
            save_connection(path, connection, scope)
            result['connection_path'] = str(path)
    elif not result.get('mappings_path') and previous.get('connection_path'):
        result['connection_path'] = previous['connection_path']
    return result


def load_connection(path, scope):
    try:
        with Path(path).open('rb') as stream: raw = stream.read(32769)
        if len(raw) > 32768: raise ValueError
        envelope = json.loads(raw)
        if set(envelope) != {'schema_version', 'data'} or envelope['schema_version'] != 'local-connection-v1': raise ValueError
        value = json.loads(_crypt(base64.b64decode(envelope['data'], validate=True), scope, decrypt=True))
        if not isinstance(value, dict) or set(value) != {'auth', 'extraction'}: raise ValueError
        return value
    except (OSError, ValueError, TypeError, KeyError):
        raise ValueError('connection_secure_storage_failed') from None

"""Один ограниченный preflight того же target, без модели и owner API."""
import http.client
import json
import re
import time

from api_agent.transport.http import loopback_origin, reject_json_constant
from api_agent.transport.socket_deadline import SocketDeadline


def check_service(target, settings):
    """Никаких retries/redirect/login; ответ не передаётся модели."""
    if not isinstance(settings, dict) or set(settings) - {'path', 'expected_status', 'expected_json'}:
        raise ValueError('health_configuration_required')
    path = settings.get('path')
    if not isinstance(path, str) or not re.fullmatch(r'/[A-Za-z0-9_/-]{1,200}', path) or '// ' in path:
        raise ValueError('health_invalid_path')
    if '//' in path or any(p in {'.', '..', 'control', 'reset', 'mode'} for p in path.split('/')):
        raise ValueError('health_invalid_path')
    expected_status = settings.get('expected_status', 200)
    if type(expected_status) is not int or not 200 <= expected_status < 300:
        raise ValueError('health_invalid_status')
    _, port = loopback_origin(target)
    if port == 18084: raise ValueError('health_control_target_denied')
    started = time.monotonic(); timeout = 2.0
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    result = {'path': path, 'status': None, 'ready': False, 'reason': 'service_unavailable'}
    try:
        connection.connect()
        with SocketDeadline(connection.sock, started + timeout) as watchdog:
            connection.request('GET', path, headers={'Accept': 'application/json'})
            response = connection.getresponse(); result['status'] = response.status
            body = bytearray()
            while len(body) <= 4096:
                part = response.read1(min(4097 - len(body), 1024)); watchdog.check()
                if not part: break
                body.extend(part)
            if len(body) > 4096 or response.length not in (None, 0):
                result['reason'] = 'health_incomplete_response'
            elif response.status == expected_status:
                expected = settings.get('expected_json')
                actual = json.loads(body, parse_constant=reject_json_constant) if expected is not None else None
                result['ready'] = expected is None or actual == expected
                result['reason'] = 'ready' if result['ready'] else 'health_not_ready'
    except (OSError, http.client.HTTPException, ValueError, RecursionError): pass
    finally: connection.close()
    result['seconds'] = time.monotonic() - started
    return result

"""Структурная поддержка, качество описаний и advisory соединение S14."""
import hashlib
import re
from pathlib import Path

from api_agent.goals.entry.readiness import check_service
from api_agent.openapi import load_source, OpenApiError
from api_agent.openapi.model import pointer_part, sensitive
from api_agent.policy.config import ProjectConfig, decision
from api_agent.transport.http import loopback_origin
from api_agent.validation import fingerprint


PREDICATES = ['exists', 'equals', 'not_equals', 'schema', 'number_rounded',
              'number_within', 'collection']


def _file_digest(name):
    if not name: return None
    path = Path(name)
    if str(path).startswith(('\\\\', '//')): return 'denied'
    try:
        with path.open('rb') as stream:
            raw = stream.read(262145)
        return hashlib.sha256(raw).hexdigest() if len(raw) <= 262144 else 'too_large'
    except (OSError, ValueError): return 'unavailable'


def project_fingerprint(values):
    """Проверка привязана к bytes, а не только execution hash без описаний."""
    return fingerprint({'target': values.get('target'),
        'files': {key: {'path': str(values.get(key) or ''), 'digest': _file_digest(values.get(key))}
                  for key in ('spec_path', 'policy_path', 'mappings_path')}})


def issue(level, code, pointer, reason, correction):
    return {'level': level, 'code': code, 'pointer': pointer[:512],
            'reason': reason, 'correction': correction}


def quality(catalog):
    """Эвристики объясняют недостающую семантику; текст не становится правилом."""
    result = []; annotations = catalog.documentation
    for op in catalog.operations.values():
        root = '/paths/' + pointer_part(op['path']) + '/' + op['method'].lower()
        if not annotations.get(root + '/description', {}).get('text', '').strip():
            result.append(issue('recommend' if op['method'] in {'GET', 'HEAD'} else 'warn',
                'operation_description_missing', root,
                'Не описаны назначение и ограничения операции.',
                'Дополните описание, если смысл входов или эффектов неясен. Отсутствие описания не блокирует профиль.'))
        if op.get('security') and not any(not alternative for alternative in op['security']):
            result.append(issue('warn', 'auth_semantics_required', root + '/security',
                'Схема безопасности не определяет вход, аккаунт и ожидаемый статус отказа.',
                'Явно настройте поддержанное подключение. Ожидаемые статусы задаются человеком.'))
        for status, response in op['responses'].items():
            schema = response.get('content', {}).get('application/json', {}).get('schema', {})
            if schema.get('type') == 'array' and any(p['in'] == 'query' for p in op['parameters']):
                result.append(issue('recommend', 'collection_scope_required', root + '/responses/' + status,
                    'Полнота коллекции и правила пагинации не следуют из её типа.',
                    'Укажите область/пагинацию до проверки отсутствия или точного состава.'))
    return result[:128]


def probe_candidates(catalog, policy):
    """Нельзя обойти DENY/HIDE/CONFIRM или самостоятельно подобрать inputs/auth."""
    return [name for name, op in catalog.operations.items()
        if op['method'] == 'GET' and '{' not in op['path']
        and re.fullmatch(r'/[A-Za-z0-9_/-]{1,200}', op['path'])
        and not any(p.get('required') for p in op['parameters'])
        and not op.get('requestBody', {}).get('required')
        and (not op['security'] or any(not alternative for alternative in op['security']))
        and not any(part in {'control', 'reset', 'mode'} for part in op['path'].split('/'))
        and decision(policy.snapshot, name, catalog.operation_hashes[name]) == 'ALLOW']


def inspect_project(values, *, network=False, probe_operation=None):
    """Без Run/LLM/worker и записи файлов; соединение никогда не блокирует save."""
    report = {'schema_version': 'project-doctor-v1', 'fingerprint': project_fingerprint(values),
        'can_save': False, 'structural': {'supported': False}, 'issues': [],
        'connection': {'state': 'not_checked', 'ready': False, 'request_sent': False},
        'supported_predicates': PREDICATES.copy(), 'probe_operations': []}
    try:
        target, _ = loopback_origin(values.get('target'))
        catalog = load_source(values.get('spec_path'), target=target)
        if values.get('policy_path'):
            policy = ProjectConfig.load(values['policy_path'], catalog)
            if policy.snapshot['target'] != target: raise ValueError('project_target_mismatch')
        else:
            policy = ProjectConfig.create('doctor', target, catalog, mode='TEST', resettable=False,
                operations={n: ('ALLOW' if op['method'] in {'GET', 'HEAD'}
                                else 'DENY' if op['method'] == 'DELETE' else 'CONFIRM')
                            for n, op in catalog.operations.items()})
        if values.get('mappings_path'):
            from api_agent.goals.entry.manual_cli import _read
            mappings = _read(values['mappings_path'])
            if not isinstance(mappings, dict) or set(mappings) - {'auth', 'extraction', 'binding_rules',
                    'private_bodies', 'model_test_passwords', 'health_check', 'public_response_headers'}:
                raise ValueError('selected_invalid_mappings')
        report['can_save'] = True
        report['structural'] = {'supported': True, 'spec_hash': catalog.spec_hash,
                                'operations': len(catalog.operations)}
        report['policy'] = {'mode': policy.snapshot['mode'],
            'operations': [{'operation_id': name, 'access': decision(policy.snapshot, name, catalog.operation_hashes[name])}
                           for name in catalog.operations]}
        report['issues'] = quality(catalog)
        report['probe_operations'] = probe_candidates(catalog, policy)
        if network and probe_operation in report['probe_operations']:
            health = check_service(target, {'path': catalog.operations[probe_operation]['path']})
            report['connection'] = {'state': 'reachable' if health['ready'] else 'unavailable',
                'ready': health['ready'], 'request_sent': True, 'status': health['status'],
                'operation_id': probe_operation, 'seconds': health['seconds']}
        elif network and probe_operation:
            report['issues'].append(issue('warn', 'probe_not_permitted', '/connection',
                'Операция не является разрешённым публичным GET без обязательных входов.',
                'Выберите показанную подходящую операцию либо оставьте соединение непроверенным.'))
    except (OpenApiError, ValueError, OSError, TypeError) as error:
        pointer = getattr(error, 'pointer', '')
        if any(sensitive(part) for part in pointer.split('/')): pointer = ''
        report['issues'].append(issue('block', getattr(error, 'code', 'project_configuration_invalid'), pointer,
            'Конфигурация не соответствует поддержанному профилю или правам проекта.',
            'Проверьте локальный OpenAPI JSON, адрес и совместимость выбранного файла прав/подключения.'))
    return report

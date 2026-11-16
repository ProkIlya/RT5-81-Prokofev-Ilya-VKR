"""Выбор источника для прежнего механизма входа приложения."""
from copy import deepcopy
from api_agent.setup.credentials import load_connection
from api_agent.setup.login_profile import authorized_login_schema, discover_login, legacy_login_schema, login_mapping
from api_agent.openapi import load_source
from api_agent.policy.config import ProjectConfig


def connection_scope(policy, catalog):
    snapshot = policy.snapshot
    return {'project': snapshot['project_id'], 'target': snapshot['target'], 'spec': catalog.spec_hash}


def project_mappings(config, scope):
    if config.get('connection_path'):
        return load_connection(config['connection_path'], scope)
    if config.get('mappings_path'):
        from api_agent.goals.entry.manual_cli import _read
        return _read(config['mappings_path'])
    return {}


def project_schema(catalog, config, mappings):
    return config.get('login_schema') or legacy_login_schema(catalog, mappings.get('auth') or {})


def session_settings(store, project):
    config = store.project(project)['configuration']
    catalog = load_source(config['spec_path'], target=config['target'])
    policy = ProjectConfig.load(config['policy_path'], catalog)
    scope = connection_scope(policy, catalog)
    return config, catalog, policy, scope


def save_session_account(store, project, session, account):
    if account is None:
        store.accounts.save(project, session, {}, None)
        return
    config, catalog, policy, scope = session_settings(store, project)
    schema = project_schema(catalog, config, project_mappings(config, scope)) or discover_login(catalog, policy)
    login_mapping(catalog, policy, schema, account)
    store.accounts.save(project, session, scope, account)


def session_login_available(store, project):
    try:
        config, catalog, policy, scope = session_settings(store, project)
        schema = project_schema(catalog, config, project_mappings(config, scope)) or discover_login(catalog, policy)
        authorized_login_schema(catalog, policy, schema)
    except (KeyError, ValueError, OSError):
        return False
    return True


def legacy_login_name(store, project):
    try:
        config, catalog, _, scope = session_settings(store, project)
        auth = project_mappings(config, scope).get('auth') or {}
        schema = legacy_login_schema(catalog, auth)
        return auth['body'].get(schema['login_field']) if schema else None
    except (KeyError, ValueError, OSError):
        return None


def run_mappings(store, catalog, policy, config, session, temporary, base):
    scope = connection_scope(policy, catalog)
    mappings = deepcopy(base)
    if temporary is not None:
        configured, account = True, temporary
    elif session is not None:
        configured, account = store.accounts.load(scope['project'], session, scope)
    else:
        configured, account = False, None
    if not configured:
        return mappings
    schema = project_schema(catalog, config, mappings)
    if legacy := mappings.pop('auth', None):
        extraction = mappings.get('extraction', {})
        paths = extraction.get(legacy['login_operation'], {})
        paths.pop(legacy.get('token_path'), None)
    if account is not None:
        selected = login_mapping(catalog, policy, schema or discover_login(catalog, policy), account)
        if legacy and legacy['login_operation'] == selected['auth']['login_operation']:
            for key in ('logout_operations', 'rotation') if 'cookie_name' not in selected['auth'] else ('logout_operations',):
                if key in legacy:selected['auth'][key] = legacy[key]
        mappings['auth'] = selected['auth']
        for operation, paths in selected['extraction'].items():
            mappings.setdefault('extraction', {}).setdefault(operation, {}).update(paths)
    return mappings

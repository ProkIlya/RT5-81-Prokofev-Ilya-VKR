"""D74: сведения об отказе и подтверждённом входе, без фоновой аутентификации."""
from copy import deepcopy
import time

from api_agent.goals.checks.compiler import PRIVATE_NAMES


def _endpoint(operation):
    """OperationId не является identity внешнего эффекта."""
    import re
    if not isinstance(operation, dict) or not isinstance(operation.get('path'), str): return None
    return operation.get('method'), re.sub(r'\{[^{}]+\}', '{}', operation['path'])


def origin_allows_recovery_login(workflow, history, project, run_id):
    """Классификация UNKNOWN только по его frozen Run/snapshot, не current names."""
    run = history.goal_parent(project, run_id)
    manifest = run.get('admission_manifest') or {}
    if not isinstance(manifest, dict): return False
    origin_auth = manifest.get('auth')
    snapshot = history.snapshot(project, run['session_id'], run_id)
    metadata = snapshot.get('metadata') or {}
    if not isinstance(metadata, dict): return False
    snapshot_auth = metadata.get('auth')
    if not isinstance(origin_auth, dict) or not isinstance(snapshot_auth, dict): return False
    issuer, token = origin_auth.get('login_operation'), origin_auth.get('token_path')
    if (not isinstance(issuer, str) or not isinstance(token, str)
            or any(snapshot_auth.get(key) != origin_auth.get(key) for key in ('login_operation', 'token_path'))):
        return False
    secret_rule = {'type': 'string', 'secret': True}
    for extraction in (manifest.get('extraction'), metadata.get('extraction')):
        if not isinstance(extraction, dict) or not isinstance(extraction.get(issuer), dict): return False
        if extraction[issuer].get(token) != secret_rule: return False
    operations = (run.get('contract') or {}).get('operations', {})
    logout = origin_auth.get('logout_operations', [])
    if not isinstance(logout, list) or any(not isinstance(name, str) or name not in operations for name in logout): return False
    original_issuer = _endpoint(operations.get(issuer))
    current_issuer = _endpoint(workflow.catalog.operations.get(workflow.auth['login_operation']))
    if original_issuer is None or original_issuer != current_issuer or token != workflow.auth['token_path']: return False
    names = {e['operation_id'] for e in run['exchanges'] if e.get('effect_status') == 'uncertain'}
    if run.get('pending_mutation'): names.add(run['pending_mutation'].get('operation_id'))
    if not names or names & {issuer, *logout}: return False
    for name in names:
        operation = operations.get(name)
        if (not isinstance(operation, dict) or operation.get('method') in {'GET', 'HEAD', 'OPTIONS'}
                or _endpoint(operation) == current_issuer): return False
    return True


def recovery_login_allowed(workflow, history, project, unresolved, operation):
    """Только совместимый trusted issuer; business latch и unknown auth сохраняются."""
    auth = workflow.auth or {}
    login = auth.get('login_operation')
    if (not login or operation != login or login in auth.get('logout_operations', [])
            or workflow.extraction.get(login, {}).get(auth['token_path']) != {'type': 'string', 'secret': True}):
        return False
    return all(origin_allows_recovery_login(workflow, history, project, run_id) for run_id in unresolved)



def session_login_context(history, project, session, mapping_hash):
    """Точный Session/конфигурация; чтение не восстанавливает действующий вход."""
    from api_agent.storage.common import _key
    import json
    for row in history.db.execute('SELECT run_id,payload FROM runs WHERE project_key=? AND session_key=? '
            "AND lifecycle='finalized' ORDER BY rowid DESC LIMIT 20", (_key(project), _key(project, session))):
        run = json.loads(row['payload'])
        if run.get('runtime_mapping_hash') != mapping_hash: continue
        run = history.read_run(project, session, row['run_id'])
        last = (run.get('auth_feedback') or {}).get('last_login')
        if last: return {'last_login': deepcopy(last), 'restored_login_is_historical': True}
    return {}


def observe_auth(workflow, session, exchange, validation):
    auth = workflow.auth
    if not auth: return
    run = session.run
    response = exchange.get('response') or {}
    status = response.get('status')
    feedback = deepcopy(run.get('auth_feedback', {}))
    if (exchange['operation_id'] == auth['login_operation'] and validation['contract_verdict'] == 'pass'
            and type(status) is int and 200 <= status < 300 and session.state.principal is not None):
        feedback['last_login'] = {'run_id': run['run_id'], 'exchange_id': exchange['id'],
            'observed_at': time.time(), 'actor': session.state.principal, 'role': None,
            'account': {k: auth['body'][k] for k in auth.get('public_body_fields', []) if k.lower() not in PRIVATE_NAMES},
            'login_operation': auth['login_operation'], 'auth_epoch': session.state.auth_epoch}
    if status in (401, 403):
        principal = exchange.get('request_principal', {})
        cause = response.get('auth_error', 'unknown')
        feedback['last_failure'] = {'run_id': run['run_id'], 'exchange_id': exchange['id'],
            'status': status, 'cause': cause, 'mode': principal.get('mode'), 'observed_at': time.time()}
        if (status == 401 and cause == 'invalid_token' and principal.get('mode') == 'authenticated'
                and principal.get('auth_epoch') == session.state.auth_epoch):
            session.state.invalidate_auth()
            run['auth_state'] = {'auth_epoch': session.state.auth_epoch, 'principal': session.state.principal}
        feedback['guidance'] = 'The ordinary agent loop may choose login/refresh/ask; preserve the original failure and negative auth intent.'
    if feedback: run['auth_feedback'] = feedback

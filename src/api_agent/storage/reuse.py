"""Локальная применимость явного locator; исторические payload не изменяются."""
from api_agent.openapi.model import Catalog, canonical


def attachment_status(history, project, descriptor, catalog, config, actor_hash=None):
    """Версия всей spec не инвалидирует независимую неизменную операцию."""
    attachment = history.resolve_attachment(project, descriptor)
    origin = attachment['origin']
    run = history.read_run(project, origin['session_id'], origin['run_id'])
    old = Catalog(canonical(run['contract']))
    reasons = []
    if origin['target'] != config.snapshot['target']:
        reasons.append('target_changed')
    for key in ('profile_version', 'parser_version'):
        if old.snapshot.get(key) != catalog.snapshot.get(key):
            reasons.append(key + '_changed')
    operation = origin['operation_id']
    if catalog.operation_hashes.get(operation) != origin['operation_hash']:
        reasons.append('operation_changed')
    if origin.get('reset_epoch') != history._fact_policy(project)['reset_epoch']:
        reasons.append('known_reset')
    authenticated = (origin.get('actor') or {}).get('mode') == 'authenticated'
    unknown_actor = authenticated and (not actor_hash or not run.get('actor_identity_hash'))
    if authenticated and not unknown_actor and actor_hash != run['actor_identity_hash']:
        reasons.append('actor_changed')
    if unknown_actor: reasons.append('actor_unconfirmed')
    return {'status': 'needs_review' if unknown_actor else 'stale' if reasons else 'applicable', 'reasons': reasons,
            'operation_id': operation, 'historical_verdict_unchanged': True}


def actor_identity(mappings):
    """Только явно публичная identity mapping, credentials в digest не входят."""
    from api_agent.goals.checks.compiler import PRIVATE_NAMES
    from api_agent.validation import fingerprint
    auth = mappings.get('auth') or {}
    account = {key: auth.get('body', {}).get(key) for key in auth.get('public_body_fields', [])
               if key.lower() not in PRIVATE_NAMES}
    return fingerprint({'login_operation': auth['login_operation'], 'account': account}) if account else None


def observe_attachments(workflow, history, run, exchange, validation):
    """Только обычный обмен того же locator/scope; никаких дополнительных HTTP."""
    from api_agent.openapi.model import pointer_get
    from api_agent.validation.facts import public_value
    updates = []
    for index, attachment in enumerate(workflow.accepted.snapshot.get('attachments', [])):
        origin = attachment['origin']
        if origin['operation_id'] != exchange['operation_id']: continue
        saved = history.read_run(run['project_id'], origin['session_id'], origin['run_id'])
        old = next(e for e in saved['exchanges'] if e['id'] == origin['exchange_id'])
        if old.get('request_parameters', {}) != exchange.get('request_parameters', {}): continue
        old_actor, actor = old.get('request_principal', {}), exchange.get('request_principal', {})
        item = {'attachment_index': index, 'origin_run_id': origin['run_id'],
                'exchange_id': exchange['id'], 'status': 'needs_review', 'reason': 'observation_incomplete'}
        response = exchange.get('response') or {}
        status = response.get('status')
        if any(old_actor.get(k) != actor.get(k) for k in ('mode', 'actor')):
            item['reason'] = 'actor_context_changed'
        elif exchange['outcome'] != 'received':
            item['reason'] = 'transport_unavailable'
        elif status in (401, 403):
            item['reason'] = 'access_unconfirmed'
        elif validation.get('contract_verdict') != 'pass' or exchange.get('truncated'):
            item['reason'] = 'contract_or_incomplete_response'
        elif status == 404 and '404' in workflow.catalog.operations[exchange['operation_id']]['responses']:
            item.update(status='unavailable', reason='documented_not_found')
        elif type(status) is int and 200 <= status < 300:
            fields = []
            try:
                for field in attachment['fields']:
                    path = origin['object_path'] + field['path']
                    value = pointer_get(response.get('json'), path)
                    if isinstance(value, (dict, list)) or not public_value(value): raise ValueError()
                    fields.append({'path': field['path'], 'value': value,
                                   'source': {'run_id': run['run_id'], 'exchange_id': exchange['id'], 'path': path}})
            except (KeyError, IndexError, TypeError, ValueError):
                item['reason'] = 'selected_fields_unconfirmed'
            else:
                item.update(status='observed', reason='same_operation_and_scope', fields=fields,
                    limitation='Identity beyond this request scope requires explicit checks or trusted binding.')
        updates.append(item)
    if updates:
        prior = {item['attachment_index']: item for item in run.get('attachment_observations', [])}
        prior.update({item['attachment_index']: item for item in updates})
        run['attachment_observations'] = list(prior.values())

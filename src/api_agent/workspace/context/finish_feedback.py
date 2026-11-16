"""Заявления об успехе сверяются со свидетельствами до завершения Run."""

import json

from api_agent.agent.response_views import named_rows
from api_agent.openapi.parser import pointer_get
from api_agent.workspace.context.catalog_evidence import catalog_matches_for_rows, catalog_selection_evidence, empty_catalog_filter_hint, missing_collection_details, research_finish_guidance, research_review_basis, selected_collection_finish_hint
from api_agent.workspace.context.run_memory import read_request_identity

GOAL_PROGRESS_HINT=('Checks cover selected aspects, not the whole goal. Compare the original goal with real API state. '
    'Complete remaining feasible actions without replaying writes; preserve expectations and failed evidence. '
    'When all goal actions are done or work is blocked, call finish_run with the final Russian summary. '
    'Printing is local feedback, not a user answer or completion.')


def unsupported_passes(claims, checklist):
    actual={c['condition_id']:c['verdict'] for c in checklist['conditions']}
    if checklist['verdict'] == 'N/A':
        return [claim['condition_id'] for claim in claims]
    return [c.get('condition_id') for c in claims if isinstance(c,dict)
        and c.get('verdict')=='PASS' and actual.get(c.get('condition_id'))!='PASS']


def _finish_blocked_by_rejection(broker):
    """Только успешное чтение текущего актора снимает прежний отказ из-за отсутствия ссылки входа."""
    events = broker.run['events']
    rejected = [event for event in events if event['kind'] == 'call_rejected'
                and event.get('details', {}).get('reason') != 'duplicate_semantic_action']
    if not rejected:
        return False
    if any(event.get('details', {}).get('reason') != 'auth_reference_denied' for event in rejected):
        return True
    state = broker.session.state
    if state.principal is None:
        return True
    last_rejection = max(event['sequence'] for event in rejected)
    later_exchanges = {event['details']['exchange_id'] for event in events
                       if event['kind'] == 'http_completed' and event['sequence'] > last_rejection}
    operations = broker.artifact.manifest['operations']
    for exchange in broker.run['exchanges']:
        principal = exchange.get('request_principal') or {}
        status = (exchange.get('response') or {}).get('status')
        if (exchange['id'] in later_exchanges and operations[exchange['operation_id']]['method'] == 'GET'
                and exchange['outcome'] == 'received' and type(status) is int and 200 <= status < 300
                and principal.get('mode') == 'authenticated' and principal.get('actor') == state.principal
                and principal.get('auth_epoch') == state.auth_epoch):
            return False
    return True


def selected_finish_hint(broker, checklist):
    if broker.stopped or broker.mutation_rejected or broker.session.uncertain or _finish_blocked_by_rejection(broker):
        return ''
    return (_missing_selected_write(broker, checklist)[1] or selected_collection_finish_hint(broker, checklist)) if broker.run['exchanges'] else ''

def research_finish_hint(broker):
    if (broker.workflow.accepted.snapshot['mode'] != 'research' or broker.stopped
            or broker.mutation_rejected or broker.session.uncertain or _finish_blocked_by_rejection(broker)):
        return None
    basis = research_review_basis(broker.run['events'])
    if basis is None:
        return None
    operations = broker.artifact.manifest['operations']
    auth = broker.workflow.auth or {}
    excluded = {auth.get('login_operation'), *auth.get('logout_operations', [])}
    for exchange in broker.run['exchanges']:
        name = exchange['operation_id']
        status = (exchange.get('response') or {}).get('status')
        if (name not in excluded and operations[name]['method'] in {'POST', 'PUT', 'PATCH', 'DELETE'}
                and exchange['outcome'] == 'received' and type(status) is int and 200 <= status < 300):
            hint = research_finish_guidance(broker, basis)
            return {'basis': basis, 'hint': hint} if hint else None
    return None


def import_recovery_hint(broker, checklist):
    """После отказа импорта возвращает модель к готовому SDK и принятому действию."""
    return ('Remove the failing import. The api object is already bound; call its listed functions directly. '
            'Do not install packages. ' + _missing_selected_write(broker, checklist)[1]).rstrip()


def _missing_selected_write(broker, checklist):
    exchanges = broker.run['exchanges']
    if (broker.stopped or broker.mutation_rejected or broker.session.uncertain
            or any(e['kind'] == 'finish_reconsidered' for e in broker.run['events'])
            or _finish_blocked_by_rejection(broker)):
        return None, ''
    attempted = {exchange['operation_id'] for exchange in exchanges}
    auth = broker.workflow.auth or {}
    for condition in checklist['conditions']:
        name = condition['scope']['operation_id']
        if condition['reason'] != 'missing_evidence' or name in attempted or name not in broker.workflow.allowed:
            continue
        if name in {auth.get('login_operation'), *auth.get('logout_operations', [])}:
            continue
        operation = broker.artifact.manifest['operations'][name]
        if operation['method'] in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            return name, ('The accepted check ' + condition['condition_id'] + ' still has no HTTP attempt for api.'
                    + operation['python_name'] + '. A different operation cannot satisfy this check. '
                    'Review the goal and prerequisites, then perform the missing action if permitted or state the limitation. ')
    return None, ''


def early_selected_progress(broker, checklist, before):
    """Показывает очередную непроверенную операцию один раз после зачёта."""
    new_ids = {exchange['id'] for exchange in broker.run['exchanges'][before:]}
    if not any(c['verdict'] == 'PASS' and c.get('exchange_id') in new_ids for c in checklist['conditions']):
        return ''
    operation_id, hint = _missing_selected_write(broker, checklist)
    if not hint or any(event['kind'] == 'selected_progress_hint'
                       and event['details'].get('operation_id') == operation_id for event in broker.run['events']):
        return ''
    broker.event('selected_progress_hint', {'basis': 'unobserved_selected_write', 'operation_id': operation_id})
    return hint


def selected_read_finish_hint(broker, checklist, before):
    """После наблюдаемой записи и свежего зачтённого чтения напоминает показать результат."""
    run = broker.run
    if (checklist['verdict'] != 'PASS' or run.get('uncertain_write')
            or any(event['kind'] == 'selected_read_finish_hint' for event in run['events'])):
        return ''
    operations = broker.artifact.manifest['operations']
    evidence = {condition['exchange_id'] for condition in checklist['conditions'] if condition['verdict'] == 'PASS'}
    def matched(exchange, methods):
        status = (exchange.get('response') or {}).get('status')
        return (exchange['id'] in evidence and exchange['outcome'] == 'received' and not exchange.get('truncated')
                and type(status) is int and 200 <= status < 300
                and operations[exchange['operation_id']]['method'] in methods)
    exchanges = run['exchanges']
    last_write = max((index for index, exchange in enumerate(exchanges)
                      if matched(exchange, {'POST', 'PUT', 'PATCH', 'DELETE'})), default=-1)
    if last_write >= 0 and any(matched(exchange, {'GET'}) for exchange in exchanges[max(before, last_write + 1):]):
        broker.event('selected_read_finish_hint')
        return ('The selected checks now pass, including a fresh read of the requested state. Check the original goal '
                'for a specific missing requirement. If there is none, call finish_run with the observed values and IDs '
                'from the saved responses. Sending the same requests again cannot improve this answer. ')
    return ''


def correction(broker, checklist):
    from api_agent.goals.checks.selected_check import pending_bound_read

    workflow = broker.workflow
    if workflow.accepted.snapshot['mode'] == 'research':
        return ('Research has no selected checks. Keep the factual summary if it answers the original goal. '
                'Call finish_run with "claims": [] and "answer_fields": {}. '
                'Use the real responses already observed. Perform additional HTTP only for missing goal work.')
    auth=workflow.auth or {}
    login=auth.get('login_operation')
    entry=''
    if login and login in workflow.allowed:
        entry=' If login is needed, call the listed api function for '+login+'; the application supplies its credentials.'
    if not broker.session.uncertain and (read := pending_bound_read(broker.catalog, broker.run, checklist['conditions'], workflow.allowed)):
        name, params = read
        operation = broker.artifact.manifest['operations'][name]
        arguments = {p['python_name']: params[p['name']] for p in operation['parameter_mapping']
                     if p['name'] in params and p['in'] in {'path', 'query'}}
        evidence = broker.safe({'function': 'api.' + operation['python_name'], 'arguments': arguments})
        entry = ' A selected check lacks this read of an already observed ID: ' + json.dumps(evidence, ensure_ascii=False) + '.' + entry
    return ('The application rejected unsupported PASS claims. No invented Python dict or printed value proves an HTTP result. '
        'Continue with actual api.* function calls from the API reference, then check the real responses. '
        'Do not simulate, mock or fabricate responses. Preserve completed writes; do not blindly repeat them. '
        'If the task cannot be completed, finish honestly with UNKNOWN/FAIL claims instead of PASS.'+entry)


def comparison_mismatch_hint(broker, checklist):
    """Сверка двух наблюдений с происхождением, уже проверенным checker."""
    for condition in checklist['conditions']:
        if condition['verdict'] != 'FAIL' or not condition.get('source_exchange_id'):
            continue
        expected = condition['expected']
        observed = condition['scope']
        descriptor = next(c for c in broker.workflow.accepted.snapshot['conditions']
                          if c['id'] == condition['condition_id'])
        comparison = [{'operation_id': expected['source']['operation_id'],
                       'path': expected['source']['path'], 'value': expected['value']},
                      {'operation_id': observed['operation_id'],
                       'path': descriptor['parameters']['path'], 'value': condition['actual']}]
        if any(type(item['value']) not in {int, float, str, bool} for item in comparison):
            continue
        evidence = json.dumps(broker.safe(comparison), ensure_ascii=False)
        if len(evidence.encode()) <= 512:
            return ('Report the mismatch using these two observed sources: ' + evidence
                    + '. Keep each value with its own operation. The failed comparison cannot verify the intended object. ')
    return ''


def contract_mismatch_hint(broker, checklist):
    """Показывает причину проваленной проверки схемы по тому же HTTP-ответу."""
    schema_checks = {item['id'] for item in broker.workflow.accepted.snapshot['conditions']
                     if item['predicate'] == 'response_schema'}
    failed_exchanges = {item['exchange_id'] for item in checklist['conditions']
                        if item['verdict'] == 'FAIL' and item['condition_id'] in schema_checks}
    for validation in broker.run['validations']:
        if (validation['exchange_id'] not in failed_exchanges or not validation['complete_response']
                or not validation['confirmed_contract_violations']):
            continue
        for issue in validation['issues']:
            details = {key: issue[key] for key in ('code', 'path', 'expected') if key in issue}
            if issue['code'] == 'schema_type':
                details['observed_json_type'] = {str: 'string', int: 'integer', float: 'number',
                    bool: 'boolean', dict: 'object', list: 'array', type(None): 'null'}.get(type(issue.get('actual')))
            evidence = json.dumps(broker.safe({'operation_id': validation['operation_id'], **details}),
                                  ensure_ascii=False)
            if len(evidence.encode()) <= 512:
                return ('Confirmed response-schema mismatch: ' + evidence
                        + '. Use this observed failure when explaining the result. '
                          'A use requiring a contract-valid response remains unverified. ')
    return ''


def post_refusal_hint(broker, before):
    """Подсказывает доступное чтение того же объекта после отказа записи."""
    exchanges = broker.run['exchanges']
    operations = broker.artifact.manifest['operations']
    for index in range(len(exchanges) - 1, before - 1, -1):
        refused = exchanges[index]
        operation = operations[refused['operation_id']]
        status = (refused.get('response') or {}).get('status')
        if refused.get('outcome') != 'received' or status not in {400, 403, 409, 422}:
            continue
        if operation['method'] not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
            continue
        for read in reversed(exchanges[:index]):
            target = operations[read['operation_id']]
            path = target['path']
            if target['method'] != 'GET' or not (operation['path'] == path or operation['path'].startswith(path + '/')):
                continue
            parameters = read.get('request_parameters', {})
            path_keys = [item['name'] for item in target['parameter_mapping'] if item['in'] == 'path']
            locator = refused.get('request_parameters', {})
            if not path_keys or any(key not in parameters or parameters[key] != locator.get(key) for key in path_keys):
                continue
            principal = refused.get('request_principal')
            if (read.get('request_principal') != principal or read.get('outcome') != 'received'
                    or (read.get('response') or {}).get('status') != 200):
                continue
            if any(event['operation_id'] == read['operation_id']
                   and event.get('request_parameters') == parameters and event.get('request_principal') == principal
                   and event.get('outcome') == 'received' and (event.get('response') or {}).get('status') == 200
                   for event in exchanges[index + 1:]):
                continue
            return (f'The earlier read of this resource preceded HTTP {status}. If the goal asks to verify unchanged state, '
                    f'call api.{target["python_name"]} again using observed parameters '
                    + json.dumps(parameters, ensure_ascii=False) + '. Keep the refusal as evidence. ')
    return ''


def empty_collection_hint(broker, before):
    """Пустой список не подтверждает отсутствие объекта с известным ID."""
    operations = broker.artifact.manifest['operations']
    for exchange in reversed(broker.run['exchanges'][before:]):
        source = operations[exchange['operation_id']]
        response = exchange.get('response') or {}
        status = response.get('status')
        if (source['method'] != 'GET' or exchange['outcome'] != 'received' or exchange.get('truncated')
                or type(status) is not int or not 200 <= status < 300
                or not response.get('json_valid') or response.get('json') != []):
            continue
        if recovery := empty_catalog_filter_hint(broker, source, exchange):
            return recovery
        prefix = source['path'].rstrip('/') + '/'
        details = []
        for name in broker.workflow.allowed:
            detail = operations[name]
            if detail['method'] != 'GET' or not detail['path'].startswith(prefix):
                continue
            parameter = detail['path'][len(prefix):]
            if parameter.startswith('{') and parameter.endswith('}') and '/' not in parameter:
                details.append(detail['python_name'])
        if len(details) == 1:
            return ('The collection returned no matches for this request. If the goal or saved evidence gives a '
                    f'known item ID, use api.{details[0]} with that ID to check the item directly. ')
    return ''


def repeated_collection_hint(broker, unchanged_reads):
    for read in reversed(unchanged_reads):
        exchange = broker.run['exchanges'][read['request_index'] - 1]
        observed = named_rows(broker.safe(exchange['response']['json']), max_bytes=512)
        if observed is not None:
            evidence = {'operation_id': exchange['operation_id'], 'rows': observed}
            return ('Observed name-to-ID pairs by source operation: ' + json.dumps(evidence, ensure_ascii=False)
                    + '. Match the requested name to its row, then use that row ID only for its resource. '
                      'If the goal requires a remaining action, use the API reference to choose it. ')
    return ''


def catalog_selection_hint(broker, before):
    """Показывает наблюдённые пары ID и имени максимум для двух разных поисков."""
    run = broker.run
    shown = {event['details'].get('exchange_id') for event in run['events'] if event['kind'] == 'catalog_selection_hint'}
    if run.get('uncertain_write') or len(shown) >= 2:
        return ''
    operations = broker.artifact.manifest['operations']
    auth = broker.workflow.auth or {}
    auth_operations = {auth.get('login_operation'), *auth.get('logout_operations', [])}
    if not any(name not in auth_operations and operations[name]['method'] in {'POST', 'PUT', 'PATCH'}
               for name in broker.workflow.allowed):
        return ''
    prior_requests = {read_request_identity(exchange) for exchange in run['exchanges'] if exchange['id'] in shown}
    for exchange in run['exchanges'][before:]:
        name = exchange['operation_id']
        response = exchange.get('response') or {}
        if (operations[name]['method'] == 'GET' and exchange['outcome'] == 'received'
                and not exchange.get('truncated') and response.get('status') == 200 and response.get('json_valid')
                and read_request_identity(exchange) not in prior_requests
                and (rows := named_rows(broker.safe(response.get('json')), max_bytes=512)) is not None):
            broker.event('catalog_selection_hint', {'operation_id': name, 'exchange_id': exchange['id']})
            query = exchange.get('request_parameters', {}).get('name')
            normalized = query.strip().casefold() if isinstance(query, str) else ''
            mismatch = ('The name filter did not return an exact full-name match. ' if normalized and all(
                row['name'].strip().casefold() != normalized for row in rows) else '')
            return ('Observed catalog rows: ' + catalog_selection_evidence(broker, name, rows) + mismatch
                    + 'For each item required by the goal, use the same item ID and its own requested values together. '
                    'When an item is named in the goal, match the full name, including qualifiers. '
                    'If a required item is absent from these results, do a separate lookup for that item. '
                    'If the goal asks you to populate a new object, establish its ID before adding related rows. '
                    'Use one object and verify that the writes affect that same ID. ')
    return ''


def collection_mismatch_hint(broker, checklist, before):
    """Показывает только проверяемые поля, сохраняя их принадлежность строке."""
    fresh = {exchange['id'] for exchange in broker.run['exchanges'][before:]}
    shown = {(event['details']['condition_id'], event['details'].get('reason')) for event in broker.run['events']
             if event['kind'] == 'collection_mismatch_hint'}
    for condition in checklist['conditions']:
        identifier, reason = condition['condition_id'], condition['reason']
        expected, actual = condition.get('expected'), condition.get('actual')
        rows = expected.get('rows') if isinstance(expected, dict) else None
        if condition.get('exchange_id') not in fresh or (identifier, reason) in shown or not isinstance(rows, list):
            continue
        if reason == 'field_missing' and isinstance(expected.get('path'), str):
            path = json.dumps(broker.safe(expected['path']), ensure_ascii=False)
            broker.event('collection_mismatch_hint', {'condition_id': identifier, 'reason': reason})
            return ('The latest read did not return collection field ' + path + '. If earlier writes appeared '
                    'successful, check that the read and the writes refer to the same object before changing '
                    'anything else. Use documented reads; do not invent an ID. Never replay completed writes. ' + missing_collection_details(broker, rows))
        if reason != 'expectation_mismatch' or not isinstance(actual, list) or len(actual) > 12:
            continue
        fields = tuple(dict.fromkeys(key for row in rows for key in row))
        try:
            observed = [{path: pointer_get(item, path) for path in fields} for item in actual]
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        evidence = json.dumps(broker.safe({'expected_rows': rows, 'observed_rows': observed, 'matching_catalog_rows': catalog_matches_for_rows(broker, rows)}), ensure_ascii=False)
        if len(evidence.encode()) <= 640:
            broker.event('collection_mismatch_hint', {'condition_id': identifier, 'reason': reason})
            return ('Compare the values within each row of this selected check: ' + evidence
                    + '. If the goal requires a change, correct the affected rows and verify their current values. ')
    return ''


def uncertain_review_hint(broker):
    """Предлагает только разрешённое чтение, связь которого видна из контракта."""
    from api_agent.goals.checks.compiler import PRIVATE_NAMES
    from api_agent.goals.checks.condition_scope import private_response_path

    remaining = broker.session.uncertain_reads_remaining
    if not remaining:
        return ('The read allowance is exhausted. Call finish_run now. Say explicitly that the write response was lost, '
                'then report the ID and public state directly confirmed by the reads and what remains unknown. All writes are blocked. ')
    guidance = (f'The write response was lost. {remaining} distinct allowed GET requests remain. '
                'Use documented reads to verify the current state. Do not repeat an unchanged read. '
                'Compare earlier observations where available. '
                'In the final answer, explicitly mention the lost response and report verified facts separately. All writes are blocked. ')
    pending = broker.run['pending_mutation']
    operations = broker.artifact.manifest['operations']
    write = operations[pending['operation_id']]
    if write['method'] != 'POST':
        return guidance
    prefix = write['path'].rstrip('/') + '/'
    extraction = (broker.run.get('admission_manifest') or {}).get('extraction', {})
    reads = [(name, op) for name, op in operations.items() if broker.session.uncertain_read_allowed(name)
             and op['path'].startswith(prefix)]
    for _, detail in reads:
        for parameter in detail['parameter_mapping']:
            field = parameter['name']
            if (parameter['in'] != 'path' or field.lower() in PRIVATE_NAMES
                    or detail['path'] != prefix + '{' + field + '}'):
                continue
            path = '/' + field.replace('~', '~0').replace('/', '~1')
            for name, locator in reads:
                suffix = locator['path'][len(prefix):]
                if ('{' in suffix or '/' in suffix or any(p['required'] for p in locator['parameter_mapping'])
                        or private_response_path(extraction, name, path)):
                    continue
                for status, response in locator['responses'].items():
                    schema = response.get('content', {}).get('application/json', {}).get('schema', {})
                    property_schema = schema.get('properties', {}).get(field, {})
                    if (status.isdigit() and 200 <= int(status) < 300 and schema.get('type') == 'object'
                            and property_schema.get('type') == parameter['schema'].get('type')
                            and property_schema.get('type') in {'integer', 'string'}):
                        return (guidance + 'OpenAPI locator: api.' + locator['python_name'] + ' returns ' + path
                                + '; reuse the observed locator when available. Verify that object using api.'
                                + detail['python_name'] + ' argument '
                                + parameter['python_name'] + '. An empty list does not verify the outcome. ')
    return guidance


def repeated_read_progress(broker, checklist, unchanged_reads):
    """После повторного чтения связывает расхождение с доступными операциями ресурса."""
    collection = repeated_collection_hint(broker, unchanged_reads)
    reads = {read['operation_id'] for read in unchanged_reads}
    steps = broker.run['steps']
    consecutive = any(read['request_index'] == read['previous_request_index'] + 1 for read in unchanged_reads)
    if consecutive and len(steps) >= 2:
        last, current = (step.get('observation') or {} for step in steps[-2:])
        output = current.get('output') or ''
        if (last.get('requests_executed') == current.get('requests_executed') == 1
                and not last.get('error') and not current.get('error')
                and len(output.encode()) > 1024 and output == last.get('output')):
            collection += ('The last two blocks got the same HTTP result and the same large printed output. '
                           'For local filtering, use only the saved Python response variable and select the '
                           'requested fields. Read again only for a required fresh observation or documented polling. ')
    if not any(item['reason'] == 'expectation_mismatch' and item['scope']['operation_id'] in reads
               for item in checklist['conditions']):
        return collection
    operations = broker.artifact.manifest['operations']
    paths = {operations[name]['path'] for name in reads if '{' in operations[name]['path']}
    attempted = {exchange['operation_id'] for exchange in broker.run['exchanges']}
    auth = broker.workflow.auth or {}
    excluded = attempted | {auth.get('login_operation'), *auth.get('logout_operations', [])}
    candidates = []
    for name in broker.workflow.allowed:
        operation = operations[name]
        if (name not in excluded and operation['method'] in {'POST', 'PUT', 'PATCH', 'DELETE'}
                and any(operation['path'] == path or operation['path'].startswith(path + '/') for path in paths)):
            candidates.append({'function': 'api.' + operation['python_name'], 'method': operation['method'],
                               'path': operation['path']})
    if not 1 <= len(candidates) <= 4:
        return collection
    return (collection + 'Repeating the read did not resolve the selected mismatch. For a change requested '
            'by the original goal, inspect these unused operations on the same resource: '
            + json.dumps(candidates, ensure_ascii=False) + '. Use observed IDs and preserve the original checks. ')


def missing_key_after_unchanged_read(exchanges, before, unchanged_reads, error_detail):
    """Связывает KeyError с отсутствием поля в наблюдаемом повторном чтении."""
    detail = error_detail or {}
    key = detail.get('key')
    if (detail.get('reason') != 'mapping_key_missing' or len(exchanges) != before + 1
            or not unchanged_reads):
        return ''
    body = exchanges[-1]['response']['json']
    if not isinstance(body, dict) or (key and key in body):
        return ''
    finding = ('The unchanged HTTP response body has no top-level key ' + json.dumps(key) + '. ' if key else
               'The HTTP response is unchanged and the Python lookup raised KeyError. Inspect the actual JSON structure. ')
    return (finding
            + 'Reuse the saved response and any earlier lookup results. A repeated read will not fix this KeyError. '
              'If the goal needs a change, choose the remaining permitted goal action using the API reference, '
              'then read again to verify the change. If the goal asks for this field, report that it is absent '
              'from the observed body unless another documented read can supply it. '
              'For requested fresh reads or documented asynchronous polling, check field presence before indexing.')


def repeated_read_key_error(steps, observation):
    """Третье подряд чтение с той же ошибкой после подсказки на втором."""
    if len(steps) < 3:
        return False
    earlier, previous = (step.get('observation') or {} for step in steps[-3:-1])
    details = observation['error_detail']
    same_error = all(result.get('error') == 'KeyError' and result.get('error_detail') == details
                     and result.get('error_line') == observation.get('error_line')
                     and result.get('requests_executed') == 1 for result in (earlier, previous))
    previous_reads = previous.get('unchanged_reads', [])
    current_read = observation['unchanged_reads'][0]
    return (same_error and len(previous_reads) == 1
            and previous_reads[0]['request_index'] == current_read['previous_request_index']
            and previous_reads[0]['previous_request_index'] + 1 == previous_reads[0]['request_index']
            and current_read['previous_request_index'] + 1 == current_read['request_index'])


def repeated_read_loop(broker, observation):
    """Останавливает третье неизменное чтение подряд после обратной связи."""
    steps = broker.run['steps']
    if len(steps) < 3:
        return False
    first, previous = (step.get('observation') or {} for step in steps[-3:-1])
    if any(step.get('error') not in {None, 'duplicate_semantic_action'} or step.get('requests_executed') != 1
           for step in (first, previous, observation)):
        return False
    prior, current = previous.get('unchanged_reads', []), observation.get('unchanged_reads', [])
    if len(prior) != 1 or len(current) != 1:
        return False
    prior, current = prior[0], current[0]
    index = current['request_index']
    if (prior['request_index'] != index - 1 or current['previous_request_index'] != index - 1
            or prior['previous_request_index'] != index - 2):
        return False
    exchange = broker.run['exchanges'][index - 1]
    operation = broker.artifact.manifest['operations'][exchange['operation_id']]
    responses = operation['responses']
    return '202' not in responses and not any(name.lower() == 'retry-after' for response in responses.values()
                                              for name in (response.get('headers') or {}))

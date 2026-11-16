"""Связывает полные имена из цели с наблюдённым каталогом и адресатами записей."""
from collections import defaultdict
import json
import re
from unicodedata import normalize

from api_agent.agent.response_views import named_rows


def _normalized(value):
    return ' '.join(normalize('NFKC', value).casefold().split())


def _goal_mentions(goal, rows):
    names = defaultdict(dict)
    for row in rows:
        name = _normalized(row['name'])
        if name:
            names[name][row['id']] = row
    text = _normalized(goal)
    occurrences = []
    for name, candidates in names.items():
        for match in re.finditer(r'(?<!\w)' + re.escape(name) + r'(?!\w)', text):
            occurrences.append((match.start(), match.end(), name))
    visible = [span for span in sorted(occurrences) if not any(
        left <= span[0] and span[1] <= right and right - left > span[1] - span[0] for left, right, _ in occurrences)]
    result = {}
    for index, (start, end, name) in enumerate(visible):
        if len(names[name]) != 1:
            continue
        limit = visible[index + 1][0] if index + 1 < len(visible) else len(text)
        tail = re.split(r'[;.!?,]', text[end:min(limit, end + 80)], maxsplit=1)[0]
        result.setdefault(name, (next(iter(names[name].values())), (text[start:end] + tail).strip()))
    return list(result.values())


def catalog_names_in_goal(goal, rows):
    return [row for row, _ in _goal_mentions(goal, rows)]


def catalog_expected_numbers(broker, rows):
    accepted = getattr(broker.workflow, 'accepted', None)
    if accepted is None:
        return []
    conditions = accepted.snapshot.get('conditions', [])
    if any(condition['predicate'] == 'status_in' and condition.get('source') != 'CONTRACT'
            and all(400 <= status < 500 for status in condition['parameters']['values']) for condition in conditions):
        return []
    names = defaultdict(set)
    for row in rows:
        names[_normalized(row['name'])].add(row['id'])
    expectations = defaultdict(list)
    for condition in conditions:
        if condition['predicate'] != 'json_collection_matches':
            continue
        for row in condition['parameters']['rows']:
            matches = {_normalized(value) for value in row.values() if isinstance(value, str)} & names.keys()
            fields = {path: value for path, value in row.items() if type(value) in {int, float}}
            if len(matches) == 1 and fields:
                name = next(iter(matches))
                if fields not in expectations[name]:
                    expectations[name].append(fields)
    result = [{'id': next(iter(names[name])), 'expected_numeric_fields': values[0]}
              for name, values in expectations.items() if len(names[name]) == len(values) == 1]
    return result if len(json.dumps(result, ensure_ascii=False).encode()) <= 400 else []


def catalog_selection_evidence(broker, operation_id, rows):
    evidence = {'operation_id': operation_id, 'rows': rows,
                'mentioned_in_goal': catalog_names_in_goal(broker.workflow.goal, rows)}
    if expected := catalog_expected_numbers(broker, rows):
        evidence['numeric_expectations'] = expected
    accepted = getattr(broker.workflow, 'accepted', None)
    remaining = unresolved_catalog_names(broker) if accepted and accepted.snapshot.get('mode') == 'research' else ''
    return json.dumps(broker.safe(evidence), ensure_ascii=False) + '. ' + remaining


def empty_catalog_filter_hint(broker, operation, exchange):
    accepted = getattr(broker.workflow, 'accepted', None)
    if not accepted or accepted.snapshot.get('mode') != 'research' or broker.run.get('uncertain_write'):
        return ''
    name = exchange['operation_id']
    parameters = operation.get('parameter_mapping', [])
    supplied = exchange.get('request_parameters', {})
    events = {event['kind'] for event in broker.run['events'] if event['details'].get('operation_id') == name}
    if (name not in broker.workflow.allowed or not any(supplied.values()) or any(p['required'] for p in parameters)
            or 'catalog_selection_hint' not in events or 'catalog_filter_recovery_hint' in events):
        return ''
    optional_query = {key for p in parameters if p['in'] == 'query' for key in (p['name'], 'query__' + p['name'])}
    if not supplied.keys() <= optional_query:
        return ''
    if any(item['operation_id'] == name and item.get('request_principal') == exchange.get('request_principal')
           and not any(item.get('request_parameters', {}).values()) and item['outcome'] == 'received'
           for item in broker.run['exchanges']):
        return ''
    broker.event('catalog_filter_recovery_hint', {'operation_id': name, 'exchange_id': exchange['id']})
    return (f'This filter returned zero rows. If the goal still needs the named item, use api.{operation["python_name"]}() '
            'once with all optional query filters omitted. Check exact names in that result before drawing a conclusion. ')


def _catalog_writes(prefix, operations, exchanges):
    writes = {}
    for exchange in exchanges:
        operation = operations[exchange['operation_id']]
        status = (exchange.get('response') or {}).get('status')
        if (operation['method'] not in {'POST', 'PUT', 'PATCH', 'DELETE'} or not operation['path'].startswith(prefix)
                or exchange['outcome'] != 'received' or type(status) is not int or not 200 <= status < 300):
            continue
        segment = operation['path'][len(prefix):].split('/', 1)[0]
        if segment.startswith('{') and segment.endswith('}'):
            key = segment[1:-1]
            parameters = exchange.get('request_parameters', {})
            value = parameters.get('path__' + key, parameters.get(key))
            if type(value) in {int, str}:
                writes[value] = exchange
    return writes


def _observed_catalogs(broker, exchange_ids=None):
    run = broker.run
    shown = {event['details'].get('exchange_id') for event in run['events'] if event['kind'] == 'catalog_selection_hint'}
    if exchange_ids is not None:
        shown &= exchange_ids
    for exchange in run['exchanges']:
        if exchange.get('id') in shown and (rows := named_rows(broker.safe(exchange['response']['json']), max_bytes=512)):
            yield exchange['operation_id'], rows


def catalog_matches_for_rows(broker, expected):
    names = {_normalized(value) for row in expected for value in row.values() if isinstance(value, str)}
    matches = {(name, row['id']): {'operation_id': name, **row} for name, rows in _observed_catalogs(broker)
               for row in rows if _normalized(row['name']) in names}
    return list(matches.values())


def missing_collection_details(broker, expected):
    evidence = broker.safe({'expected_rows': expected, 'matching_catalog_rows': catalog_matches_for_rows(broker, expected)})
    encoded = json.dumps(evidence, ensure_ascii=False)
    return ('Expected rows and observed catalog matches: ' + encoded + '. If the original goal requires these rows, '
            'apply only the missing permitted changes, then read the same object. ' if len(encoded.encode()) <= 512 else '')


def unresolved_catalog_names(broker, *, exchange_ids=None, require_post=False):
    """Сообщает, какие названные записи не были адресатами наблюдаемых операций."""
    operations = broker.artifact.manifest['operations']
    missing = {}
    for name, rows in _observed_catalogs(broker, exchange_ids):
        prefix = operations[name]['path'].rstrip('/') + '/'
        written = _catalog_writes(prefix, operations, broker.run['exchanges'])
        if written:
            missing.update({(name, row['id']): {'operation_id': name, **row, 'goal_phrase': phrase}
                            for row, phrase in _goal_mentions(broker.workflow.goal, rows) if row['id'] not in written})
    evidence = json.dumps(broker.safe(list(missing.values())), ensure_ascii=False)
    if not missing or len(evidence.encode()) > 640:
        return ''
    post = _related_catalog_post(broker, missing.values())
    if require_post and not post:
        return ''
    operation = (f'Inspect {post} for the permitted addition. If the request field has no documented unit, '
                 'copy the numeric magnitude from that row’s goal phrase directly into the request. '
                 'For example, a requested amount of two weeks is 2 when the field has no specified unit. '
                 'Convert only to a unit documented by the API. ') if post else ''
    return ('These exact catalog rows occur in the goal without a matching observed write: ' + evidence
            + '. Check which the goal asks you to add or change, then verify the same parent. ' + operation)


def catalog_quantity_evidence(broker):
    """Сопоставляет исходную формулировку и фактически отправленные числа по ID."""
    observations = {}
    operations = broker.artifact.manifest['operations']
    for name, rows in _observed_catalogs(broker):
        writes = _catalog_writes(operations[name]['path'].rstrip('/') + '/', operations, broker.run['exchanges'])
        for row, phrase in _goal_mentions(broker.workflow.goal, rows):
            item = writes.get(row['id'], {})
            body = (item.get('request') or {}).get('json')
            if not isinstance(body, dict):
                continue
            numbers = {key: value for key, value in body.items() if type(value) in {int, float}}
            if numbers:
                observations[(name, row['id'])] = {'id': row['id'], 'goal_phrase': phrase, 'sent_numbers': numbers}
    evidence = json.dumps(broker.safe(list(observations.values())), ensure_ascii=False)
    return ('Compare each goal phrase with the numbers actually sent for that item: ' + evidence
            + '. Convert only when OpenAPI establishes the target unit. Correct only a confirmed mismatch. '
            if observations and len(evidence.encode()) <= 640 else '')


def _related_catalog_post(broker, matches):
    operations = broker.artifact.manifest['operations']
    prefixes = {operations[row['operation_id']]['path'].rstrip('/') + '/{' for row in matches}
    candidates = []
    for name in broker.workflow.allowed:
        operation = operations[name]
        if operation['method'] == 'POST' and any(operation['path'].startswith(prefix)
                and '}/' in operation['path'][len(prefix):] for prefix in prefixes):
            candidates.append('api.' + operation['python_name'])
    return candidates[0] if len(candidates) == 1 else None


def selected_collection_finish_hint(broker, checklist):
    """Повторно показывает подтверждённый недобор строк до публикации ответа."""
    events = broker.run['events']
    if any(event['kind'] == 'finish_reconsidered' for event in events):
        return ''
    shown = {(event['details']['condition_id'], event['details']['reason'])
             for event in events if event['kind'] == 'collection_mismatch_hint'}
    exchanges = {exchange['id']: exchange for exchange in broker.run['exchanges']}
    for condition in checklist['conditions']:
        expected = condition.get('expected')
        if (condition['verdict'] != 'FAIL' or (condition['condition_id'], condition['reason']) not in shown
                or not isinstance(expected, dict) or not isinstance(expected.get('rows'), list)):
            continue
        observed = exchanges.get(condition.get('exchange_id'), {})
        locator = observed.get('request_parameters')
        matches = catalog_matches_for_rows(broker, expected['rows'])
        if not locator or not matches:
            continue
        evidence = broker.safe({'read_operation': observed['operation_id'], 'observed_parameters': locator,
            'expected': expected, 'catalog_matches': matches})
        encoded = json.dumps(evidence, ensure_ascii=False)
        if len(encoded.encode()) <= 640:
            post = _related_catalog_post(broker, matches) if condition['reason'] == 'field_missing' else None
            related = f'The API reference includes {post} below the catalog item. Inspect its purpose and arguments. ' if post else ''
            return ('The selected collection check still failed on this exact object: ' + encoded
                    + '. Use the catalog ID for the row and the observed parameters for the parent. ' + related
                    + 'If permitted and required, correct only the missing or mismatched row on the observed parent '
                      'and read that same parent again. Do not claim that a missing change was made. ')
    return ''


def _catalog_after_coverage_review(events):
    recent = set()
    for event in reversed(events):
        if event['kind'] == 'finish_reconsidered' and event['details'].get('basis') == 'research_catalog_coverage':
            return recent
        if event['kind'] == 'catalog_selection_hint':
            recent.add(event.get('details', {}).get('exchange_id'))
    return set()


def research_review_basis(events):
    reviewed = {event['details'].get('basis') for event in events if event['kind'] == 'finish_reconsidered'}
    if 'research_catalog_coverage' in reviewed and reviewed <= {'research_auth_recovery', 'research_catalog_coverage'}:
        return 'research_new_catalog_match' if _catalog_after_coverage_review(events) else None
    if reviewed - {'research_auth_recovery'}:
        return None
    if any(event['kind'] == 'catalog_selection_hint' for event in events):
        return 'research_catalog_coverage'
    if not reviewed and any(event['kind'] == 'call_rejected'
            and event['details'].get('reason') == 'auth_reference_denied' for event in events):
        return 'research_auth_recovery'
    return None


def research_finish_guidance(broker, basis):
    if basis == 'research_new_catalog_match':
        matches = unresolved_catalog_names(broker, exchange_ids=_catalog_after_coverage_review(broker.run['events']),
                                           require_post=True)
        if matches:
            return ('The latest successful catalog response contains a requested item that still lacks a matching write. '
                    + matches + 'Use that exact observed ID and its own quantity if the goal requires an addition. '
                    'After the write, read the same parent and report the confirmed result. ')
        return ''
    if basis == 'research_auth_recovery':
        return ('Authentication recovered. Before finishing, review the remaining requested work against the real responses. '
                'Use the documented lookups for missing prerequisites and perform only the remaining permitted work. '
                'If the goal is complete or blocked, explain the observed result. ')
    return (unresolved_catalog_names(broker) + catalog_quantity_evidence(broker)
            + 'Check every requested item and value in the original goal against the latest result on the same ID. '
              'If a requested item is absent, find its exact ID and perform only the missing permitted action. '
              'Keep completed writes. If the result already covers the goal or work is blocked, call finish_run again. ')

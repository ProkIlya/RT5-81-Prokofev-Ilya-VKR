"""S09c: настоящий HTTP→pinned evidence, парное answer-only сравнение LLM.

Primary/generator при сборе controlled. В сравнении настоящая локальная LLM,
одинаковые snapshots двух вариантов; HTTP и generator не повторяются.
"""
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import hashlib
import json
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from api_agent.agent.catalog import CatalogLimits
from api_agent.agent.model import LocalModel
from api_agent.agent.resilience import TokenBudget
from api_agent.goals.feedback import correct_answer
from api_agent.goals.application import GoalApplication
from api_agent.goals.interaction import HumanInteraction
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig
from api_agent.storage import SQLiteHistory
from api_agent.validation import fingerprint


def reply(name, arguments):
    return {'choices': [{'message': {'tool_calls': [{'id': 'call-' + name, 'type': 'function',
        'function': {'name': name, 'arguments': json.dumps(arguments)}}]}}]}


def collect(directory, domain, shape, *, multiple=False):
    """Реальные response тела различной формы; expected указан в цели доHTTP."""
    field, actual, expected = {'pet': ('name', 'Murka', 'Barsik'), 'weather': ('temperature', 18, 20),
        'stock': ('quantity', 5, 7), 'holdout': ('balance', 11, 12)}[domain]
    value_schema = {'type': 'string'} if isinstance(actual, str) else {'type': 'integer'}
    row = {'type': 'object', 'required': [field], 'properties': {field: value_schema}}
    schema, body, pointer = row, {field: actual}, '/' + field
    if shape == 'array': schema, body, pointer = {'type': 'array', 'items': row}, [body], '/0' + pointer
    if shape == 'wrapped':
        schema, body, pointer = {'type': 'object', 'properties': {'items': {'type': 'array', 'items': row}}, 'required': ['items']}, {'items': [body]}, '/items/0' + pointer
    if shape == 'nested':
        schema, body, pointer = {'type': 'object', 'properties': {'result': row}, 'required': ['result']}, {'result': body}, '/result' + pointer
    journal = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            journal.append(self.path)
            payload = json.dumps(body).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload))); self.end_headers(); self.wfile.write(payload)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
    try:
        operation = 'inspect' + domain.title()
        path = '/' + domain + ('/{id}' if multiple else '')
        op = {'operationId': operation, 'responses': {'200': {'description': 'ok',
            'content': {'application/json': {'schema': schema}}}}}
        if multiple: op['parameters'] = [{'name': 'id', 'in': 'path', 'required': True, 'schema': {'type': 'integer'}}]
        catalog = parse_document({'openapi': '3.0.3', 'info': {'title': domain, 'version': '1'}, 'paths': {path: {'get': op}}})
        target = f'http://127.0.0.1:{server.server_port}'
        config = ProjectConfig.create('p', target, catalog, mode='READ_ONLY', resettable=True, operations={operation: 'A'})
        goal = (f'Проверь {field}: для сущности 1 ожидается {actual}, для сущности 2 — {expected}.' if multiple else
                f'Проверь {field}: ожидается {expected}.')
        slots = [{'id': f's{i}', 'operation_id': operation, 'parameters': {'id': i} if multiple else {},
            'auth_mode': 'anonymous', 'actor': 'anonymous', 'auth_epoch': 0, 'depends_on': []} for i in range(1, 3 if multiple else 2)]
        conditions = [{'id': f'c{i}', 'requirement_id': 'r1', 'slot': slot['id'], 'predicate': 'json_value_equals',
            'parameters': {'path': pointer, 'value': actual if multiple and i == 1 else expected},
            'source': 'MODEL_PROPOSED', 'basis': None} for i, slot in enumerate(slots, 1)]
        answers = [{'id': f'a{i}', 'requirement_id': 'r1', 'slot': slot['id'], 'path': pointer} for i, slot in enumerate(slots, 1)]
        proposal = {'version': 1, 'goal_hash': fingerprint(goal), 'config_hash': fingerprint(config.snapshot), 'spec_hash': catalog.spec_hash,
            'requirements': [{'id': 'r1', 'span': [0, len(goal)], 'interpretation': goal}], 'slots': slots,
            'conditions': conditions, 'answer_fields': answers, 'unresolved': []}
        def primary(payload):
            observations = [json.loads(m['content']) for m in payload['messages'] if m['role'] == 'tool']
            exchanges = [o['data'] for o in observations if (o.get('data') or {}).get('exchange_id')]
            if len(exchanges) < len(slots):
                slot = slots[len(exchanges)]
                return reply('execute_operation', {'operation_id': operation, 'occurrence_id': slot['id'],
                    **({'parameters': slot['parameters']} if slot['parameters'] else {})})
            return reply('finish_run', {'summary': 'Обнаружено отличие от ожидаемого.',
                'evidence_ids': [e['exchange_id'] for e in exchanges], 'claims': [{'condition_id': c['id'],
                    'verdict': 'PASS' if multiple and c['id'] == 'c1' else 'FAIL'} for c in conditions],
                'answer_fields': {a['id']: actual for a in answers}})
        with SQLiteHistory(directory / 'source.sqlite3') as history:
            source = GoalApplication(target, catalog, config, history=history, generator=lambda p: reply('propose_goal', proposal),
                human=HumanInteraction(lambda q: {'request_id': q['id'], 'value': 'yes'}), token_counter=lambda p: 100).start('p', 's', 'm', goal, primary)
        (directory / 'source-run.json').write_text(json.dumps(source, ensure_ascii=False, indent=2), encoding='utf-8')
        (directory / 'source-journal.json').write_text(json.dumps(journal), encoding='utf-8')
        assert source['status'] == 'completed' and source['report']['goal_verdict'] == 'FAIL', source.get('stop_reason')
        assert len(journal) == len(slots)
        return source
    finally:
        server.shutdown(); server.server_close(); worker.join()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--phase', choices=['pilot', 'series'], required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--thresholds', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    lock = json.loads((ROOT / 'experiments/local_model/runtime-lock.json').read_text(encoding='utf-8'))
    with (ROOT / 'models/MiniCPM5-2B-Q4_K_M.gguf').open('rb') as weights: model_hash = hashlib.file_digest(weights, 'sha256').hexdigest()
    assert model_hash == lock['model']['sha256']
    thresholds = None
    if args.phase == 'series':
        thresholds = json.loads(args.thresholds.read_text(encoding='utf-8'))
        assert thresholds['frozen_before_series'] is True
    cases = [('pet', 'object', False)] if args.phase == 'pilot' else [
        ('pet', 'object', False), ('weather', 'wrapped', False), ('stock', 'array', False),
        ('pet', 'object', True), ('holdout', 'nested', False)]
    records = []
    for index, (domain, shape, multiple) in enumerate(cases):
        directory = args.output / f'{index}-{domain}-{shape}'; directory.mkdir()
        source = collect(directory, domain, shape, multiple=multiple)
        # Fault injection только в копию draft для answer-only replay. Сам
        # сохранённый Run, expected/actual и исходное evidence не меняются.
        variants = ['false_pass', 'omitted', 'accurate'] if args.phase == 'series' else ['false_pass']
        for variant in variants:
            for enabled in (False, True):
                run = deepcopy(source); run.pop('answer_feedback', None)
                draft = run['steps'][-1]['action']['arguments']
                if variant == 'false_pass':
                    draft['summary'] = 'Все условия выполнены.'
                    for claim in draft['claims']: claim['verdict'] = 'PASS'
                if variant == 'omitted':
                    draft.pop('claims'); draft.pop('answer_fields')
                model = LocalModel('http://127.0.0.1:18081')
                budget = TokenBudget(CatalogLimits(), model.count_tokens)
                started = time.monotonic()
                result = correct_answer(run, model, budget, deadline=started + 180, clock=time.monotonic,
                    cancel=threading.Event(), diagnostic=enabled)
                record = {'case': f'{index}-{domain}-{shape}', 'variant': variant,
                    'mode': 'diagnostic' if enabled else 'code_only', 'result': result,
                    'resources': budget.snapshot(), 'seconds': time.monotonic() - started,
                    'http_during_comparison': 0, 'kind': 'answer-only replay; real local LLM; source HTTP/primary controlled',
                    'oracle': {'condition_verdicts': ['PASS', 'FAIL'] if multiple else ['FAIL'], 'goal_verdict': 'FAIL'},
                    'false_alarm': variant == 'accurate' and result['calls'] > 0,
                    'spoiled': result['answer']['goal_verdict'] != 'FAIL',
                    'corrected': variant != 'accurate' and result['status'] == 'corrected'}
                records.append(record)
                (directory / f'{variant}-{record["mode"]}.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
                print(json.dumps({k: record[k] for k in ('case', 'variant', 'mode', 'seconds', 'corrected', 'false_alarm')}, ensure_ascii=False), flush=True)
    summary = {'phase': args.phase, 'model_sha256': model_hash, 'thresholds': thresholds,
        'attempts': len(records), 'arms': {}}
    for mode in ('code_only', 'diagnostic'):
        arm = [r for r in records if r['mode'] == mode]
        summary['arms'][mode] = {'attempts': len(arm), 'corrected': sum(r['corrected'] for r in arm),
            'false_alarms': sum(r['false_alarm'] for r in arm), 'spoiled': sum(r['spoiled'] for r in arm),
            'calls': sum(r['result']['calls'] for r in arm), 'seconds': sum(r['seconds'] for r in arm),
            'measured_tokens': sum(r['resources']['total_tokens'] or 0 for r in arm),
            'unknown_usage_attempts': sum(r['resources']['usage_status'] == 'unknown' and r['result']['calls'] > 0 for r in arm)}
    if thresholds:
        code, diag = summary['arms']['code_only'], summary['arms']['diagnostic']
        summary['enable_diagnostic_by_default'] = (diag['corrected'] - code['corrected'] >= thresholds['min_additional_corrections']
            and not diag['false_alarms'] and not diag['spoiled'] and not diag['unknown_usage_attempts']
            and diag['seconds'] <= max(code['seconds'], 0.001) * thresholds['max_time_ratio'])
    (args.output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__': raise SystemExit(main())

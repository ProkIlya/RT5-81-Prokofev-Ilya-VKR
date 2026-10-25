"""Пилот: живые primary модели и HTTP, controlled proposal + acceptance; не STAND-37."""
import json
import hashlib
import os
from pathlib import Path
import sys
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent.model import LocalModel
from api_agent.agent.catalog import CatalogLimits
from api_agent.goals.application import GoalApplication
from api_agent.goals.interaction import HumanInteraction
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig
from api_agent.storage import SQLiteHistory
from api_agent.validation import fingerprint
from api_agent.workspace.runtime import WorkspaceOptions
from api_agent.workspace.sdk import build_sdk
from agent_support import reply


def case(root, domain, shape, actual, expected, field, *, interfaces=('code', 'structured')):
    row = {'type': 'object', 'required': [field], 'properties': {field: {'type': 'string' if isinstance(actual, str) else 'integer'}}}
    schema, body, pointer = row, {field: actual}, '/' + field
    if shape == 'array': schema, body, pointer = {'type': 'array', 'items': row}, [body], '/0' + pointer
    if shape == 'wrapped':
        schema, body, pointer = {'type': 'object', 'properties': {'items': {'type': 'array', 'items': row}}, 'required': ['items']}, {'items': [body]}, '/items/0' + pointer
    journal = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            journal.append({'path': self.path, 'method': 'GET'})
            raw = json.dumps(body).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw))); self.end_headers(); self.wfile.write(raw)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        operation = 'inspect' + domain.title()
        catalog = parse_document({'openapi': '3.0.3', 'info': {'title': domain, 'version': '1'},
            'paths': {'/' + domain: {'get': {'operationId': operation, 'responses': {'200': {
                'description': 'ok', 'content': {'application/json': {'schema': schema}}}}}}}})
        config = ProjectConfig.create('p', f'http://127.0.0.1:{server.server_port}', catalog,
            mode='READ_ONLY', resettable=True, operations={operation: 'A'})
        goal = f'Проверь {field}: ожидается {expected}.'
        proposal = {'version': 1, 'goal_hash': fingerprint(goal), 'spec_hash': catalog.spec_hash,
            'config_hash': fingerprint(config.snapshot),
            'requirements': [{'id': 'r1', 'span': [0, len(goal)], 'interpretation': goal}],
            'slots': [{'id': 's1', 'operation_id': operation, 'parameters': {}, 'auth_mode': 'anonymous',
                'actor': 'anonymous', 'auth_epoch': 0, 'depends_on': []}],
            'conditions': [{'id': 'c1', 'requirement_id': 'r1', 'slot': 's1', 'predicate': 'json_value_equals',
                'parameters': {'path': pointer, 'value': expected}, 'source': 'MODEL_PROPOSED', 'basis': None}],
            'answer_fields': [{'id': 'a1', 'requirement_id': 'r1', 'slot': 's1', 'path': pointer}], 'unresolved': []}
        artifact = build_sdk(catalog, config)
        directory = root / domain; directory.mkdir(); artifact.save(directory / 'sdk')
        records = []
        for interface in interfaces:
            target = directory / interface; target.mkdir()
            before = len(journal)
            model = LocalModel('http://127.0.0.1:18081')
            human = HumanInteraction(lambda q: {'request_id': q['id'], 'value': 'yes'})
            try:
                with SQLiteHistory(target / 'history.sqlite3') as history:
                    app = GoalApplication(config.snapshot['target'], catalog, config, history=history,
                        generator=lambda p: reply('propose_goal', proposal), human=human, limits=CatalogLimits(),
                        token_counter=model.count_tokens, workspace=WorkspaceOptions(artifact, os.environ['WORKSPACE_IMAGE']) if interface == 'code' else None)
                    run = app.start('p', 's', 'm', goal, model)
                (target / 'run.json').write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding='utf-8')
                (target / 'journal.json').write_text(json.dumps(journal[before:], indent=2), encoding='utf-8')
                verdict = 'PASS' if actual == expected else 'FAIL'
                record = {'domain': domain, 'shape': shape, 'interface': interface, 'status': run['status'],
                    'stop': run['stop_reason'], 'http': len(journal) - before, 'model_calls': run.get('model_calls'),
                    'goal_verdict': run['report']['goal_verdict'], 'expected_verdict': verdict,
                    'manifest_hash': run['admission_manifest']['goal_contract']['manifest_hash'],
                    'successful': run['status'] == 'completed' and run['report']['goal_verdict'] == verdict
                        and run['report']['goal_checklist']['answer_fields'] == {'a1': actual} and len(journal) - before == 1}
                records.append(record); print(json.dumps(record, ensure_ascii=False), flush=True)
            finally: human.close()
        assert len({r['manifest_hash'] for r in records}) == 1, 'different accepted basis'
        return records
    finally: server.shutdown(); server.server_close(); thread.join()


def main(output, *, domain=None, interface=None):
    root = Path(output); root.mkdir(parents=True, exist_ok=False)
    from scripts.check_workspace_sandbox import preflight
    sandbox = preflight()
    files = sorted({p for folder in ('src', 'tests', 'scripts', 'demo_api', 'docker/workspace')
        for p in (ROOT / folder).rglob('*') if p.is_file() and p.suffix in {'.py', '.sql', '.json', ''}})
    def hashes(): return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    identity = {'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        'source_hashes': hashes(), 'model_lock': json.loads((ROOT / 'experiments/local_model/runtime-lock.json').read_text(encoding='utf-8'))}
    with (ROOT / 'models/MiniCPM5-2B-Q4_K_M.gguf').open('rb') as weights:
        identity['model_sha256'] = hashlib.file_digest(weights, 'sha256').hexdigest()
    if identity['model_sha256'] != identity['model_lock']['model']['sha256']: raise ValueError('model_hash_mismatch')
    (root / 'identity.json').write_text(json.dumps(identity, ensure_ascii=False, indent=2), encoding='utf-8')
    records = []
    for args in [('pet', 'object', 'Murka', 'Murka', 'name'), ('weather', 'wrapped', 18, 20, 'temperature'),
                 ('stock', 'array', 5, 7, 'quantity')]:
        if domain is not None and args[0] != domain: continue
        records.extend(case(root, *args, interfaces=(interface,) if interface else ('code', 'structured')))
        (root / 'summary.json').write_text(json.dumps({'kind': __doc__, 'records': records,
            'sandbox': sandbox, 'limits': vars(CatalogLimits()), 'source_unchanged': hashes() == identity['source_hashes']}, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if records and all(r['successful'] for r in records) and hashes() == identity['source_hashes'] else 1


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--domain', choices=('pet', 'weather', 'stock'))
    parser.add_argument('--interface', choices=('code', 'structured'))
    args = parser.parse_args()
    raise SystemExit(main(args.output, domain=args.domain, interface=args.interface))

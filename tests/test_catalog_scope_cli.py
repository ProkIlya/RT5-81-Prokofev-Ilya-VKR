"""Публичный CLI принимает те же доверенные области, что и библиотека."""
import contextlib
import io
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent.__main__ import main
from api_agent.openapi import load_source
from api_agent.policy import ProjectConfig
from api_agent.storage import SQLiteHistory
from agent_support import ObserverModel, count_test_tokens, reply


@contextlib.contextmanager
def selection_server():
    """Реальный HTTP endpoint поддерживает query, в отличие от пилотного Demo."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.server.connections += 1
            self.server.paths.append(self.path)
            body = b'{"id":1,"name":"Murka"}'
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
        server.connections = 0
        server.paths = []
        server.target = f'http://127.0.0.1:{server.server_port}'
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()


class CatalogScopeCliTests(unittest.TestCase):
    def run_cli(self, mappings, *, parameters=None, accepted=True):
        with selection_server() as server, tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            document = json.loads((ROOT / 'tests/fixtures/demo_openapi.json').read_text())
            document['paths']['/pets/1']['get']['parameters'] = [
                {'name': 'category', 'in': 'query', 'required': False, 'schema': {'type': 'string'}}]
            spec = directory / 'openapi.json'
            spec.write_text(json.dumps(document), encoding='utf-8')
            catalog = load_source(str(spec))
            config = ProjectConfig.create('p', server.target, catalog, mode='READ_ONLY',
                                          resettable=True, operations={'getPet': 'A'})
            config.save(directory / 'project.json')
            checks = {'getPet': [{'predicate': 'status_in', 'parameters': {'values': [200]},
                                  'intent': 'positive', 'preconditions': 'verified'}]}
            (directory / 'checks.json').write_text(json.dumps(checks), encoding='utf-8')
            (directory / 'mappings.json').write_text(json.dumps(mappings), encoding='utf-8')
            observer = ObserverModel()
            calls = []

            def model(payload):
                calls.append(payload)
                if len(calls) == 1:
                    return reply('execute_operation', {'operation_id': 'getPet', 'parameters': parameters or {}})
                return observer(payload)

            args = ['Check pet', '--target', server.target, '--spec', str(spec),
                    '--project-config', str(directory / 'project.json'),
                    '--goal-checks', str(directory / 'checks.json'),
                    '--runtime-mappings', str(directory / 'mappings.json'),
                    '--evidence-dir', str(directory / 'run'), '--session-id', 's', '--message-id', 'm']
            output, errors = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = main(args, model=model, token_counter=count_test_tokens)
            if not accepted:
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(errors.getvalue())['code'], 'agent_setup_failed')
                self.assertEqual(calls, [])
                self.assertEqual(server.connections, 0)
                return
            self.assertEqual(code, 0, errors.getvalue())
            result = json.loads(output.getvalue())
            self.assertEqual(result['checks_verdict'], 'PASS')
            self.assertEqual(server.connections, 1)
            self.assertEqual(server.paths, ['/pets/1?category=active' if parameters else '/pets/1'])
            self.assertEqual(len(calls), 2)
            with SQLiteHistory(directory / 'run/history.sqlite3') as history:
                saved = history.snapshot('p', 's', result['run_id'])
                expected = {name: {**scope, 'parameters': scope.get('parameters', {})}
                            for name, scope in mappings['selection_scopes'].items()}
                self.assertEqual(saved['metadata']['selection_scopes'], expected)
            def forbidden(payload):
                self.fail('Повторная доставка вызвала модель')
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args, model=forbidden, token_counter=count_test_tokens), 0)
            self.assertEqual(server.connections, 1)

    def test_unfiltered_scope_runs_and_survives_restart(self):
        self.run_cli({'selection_scopes': {'getPet': {'mode': 'unfiltered'}}})

    def test_exact_scope_runs_and_survives_restart(self):
        self.run_cli({'selection_scopes': {'getPet': {'mode': 'exact', 'parameters': {'category': 'active'}}}},
                     parameters={'category': 'active'})

    def test_invalid_scope_rejected_before_model_or_http(self):
        self.run_cli({'selection_scopes': {'getPet': {'mode': 'unfiltered', 'parameters': {'category': 'active'}}}},
                     accepted=False)

    def test_unknown_mapping_rejected_before_model_or_http(self):
        self.run_cli({'selection_scopes': {'getPet': {'mode': 'unfiltered'}}, 'unknown_option': {}}, accepted=False)

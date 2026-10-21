"""Настоящие контейнеры обязательны для S09d2 gate; без engine это preparation error."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.storage.history import SQLiteHistory
from demo_api.server import DemoServer
import test_workspace_broker as fixtures


@unittest.skipUnless(os.environ.get('WORKSPACE_DOCKER_TESTS') == '1', 'Требуется явный запуск Docker integration suite')
class WorkspaceSandboxTests(unittest.TestCase):
    def test_docker_client_proxy_credentials_do_not_enter_worker(self):
        private = 'proxy-authority-canary-7623'
        with tempfile.TemporaryDirectory() as config:
            (Path(config) / 'config.json').write_text(json.dumps({'proxies': {'default': {
                'httpProxy': 'http://owner:' + private + '@127.0.0.1:19000'}}}), encoding='utf-8')
            def exercise(worker, broker, server):
                self.assertNotIn(private, json.dumps(worker.inspect()['Config']['Env']))
                result = worker.execute("import os; print(any('proxy' in k.lower() for k in os.environ))")
                self.assertEqual(result['output'].strip(), 'False')
                self.assertEqual(server.connections, 0)
            with patch.dict(os.environ, {'DOCKER_CONFIG': config}): self.run_case(exercise)

    def test_large_json_remains_in_namespace_with_small_explicit_output(self):
        from api_agent.openapi import load_source
        from api_agent.policy import ProjectConfig
        from api_agent.agent.catalog import CatalogWorkflow
        from api_agent.workspace_broker import WorkspaceBroker
        from api_agent.workspace_sdk import build_sdk
        module = self.module()
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            server.pet['name'] = 'x' * 48000
            catalog = load_source(str(ROOT / 'tests/fixtures/demo_openapi.json'))
            config = ProjectConfig.create('p', server.target, catalog, mode='READ_ONLY', resettable=False, operations={'getPet': 'A'})
            workflow = CatalogWorkflow(catalog, config, goal='Read large JSON', conditions={'getPet': [fixtures.status_condition(200)]})
            with WorkspaceBroker(workflow, history, artifact=build_sdk(catalog, config), session_id='s', message_id='m') as broker:
                with module.WorkspaceSandbox(broker, image=os.environ['WORKSPACE_IMAGE'], root=Path(directory) / 'work') as worker:
                    first = worker.execute("data=api.getPet()\nassert not data['truncated'] and data['json_valid']\nprint(len(data['body']['name']))")
                    self.assertIsNone(first['error'])
                    self.assertEqual(first['output'].strip(), '48000')
                    second = worker.execute("print(len(data['body']['name'])); api.finish('Observed', claims=[], answer_fields={})")
                    self.assertEqual(second['output'].strip(), '48000')
                    self.assertIsNone(second['error'])
                self.assertFalse(worker.exists())
            self.assertEqual(len(server.journal), 1)

    def test_caught_uncertain_write_in_python_cannot_retry(self):
        def exercise(worker, broker, server):
            server.mode = 'timeout_after_commit'; broker.session.timeout = .1
            result = worker.execute("api.createOrder(body={'name':'A'})\nfor i in range(3):\n    try: api.createOrder(body={'name':'B'})\n    except UncertainWrite: pass\nprint('PASS')")
            self.assertIsNone(result['error'])
            self.assertEqual(len(server.journal), 1)
            self.assertEqual(broker.stopped, 'uncertain_write')
            self.assertFalse(broker.finished)
        self.run_case(exercise)

    def test_caught_cancel_in_python_cannot_restore_authority(self):
        def exercise(worker, broker, server):
            class CancelJournal(list):
                def append(self, item): super().append(item); broker.cancel.set()
            server.journal = CancelJournal()
            result = worker.execute("a=api.createOrder(body={'name':'A'})\nfor i in range(3):\n    try: api.readOrder(path__order_id=a['body']['id'])\n    except Cancelled: pass\nprint('PASS')")
            self.assertIsNone(result['error'])
            self.assertEqual(len(server.journal), 1)
            self.assertEqual(broker.stopped, 'cancelled')
            self.assertFalse(broker.finished)
        self.run_case(exercise)

    def test_malformed_done_frame_cannot_escape_protocol_validation(self):
        def exercise(worker, broker, server):
            result = worker.execute("import sys,os,json\ne=sys._getframe(1).f_locals['execution']\nos.write(1,json.dumps({'kind':'done','execution_id':e,'output':'','output_truncated':False,'error':[],'variables':[]}).encode()+b'\\n')")
            self.assertEqual(result['error'], 'ProtocolDenied')
            self.assertFalse(broker.finished)
            self.assertEqual(server.journal, [])
        self.run_case(exercise)

    def test_private_code_output_exception_and_temp_never_reach_durable_artifacts(self):
        secret = 'explicit-test-password-S09d2-8472'
        def exercise(worker, broker, server):
            broker.workflow.secrets = lambda: (secret,)
            result = worker.execute("import base64\np=" + repr(secret) + "\nopen('/tmp/private','w').write(p)\nprint(base64.b64encode(p.encode()).decode())\nraise ValueError(p)")
            self.assertEqual(result['error'], 'ValueError')
            self.assertNotIn(secret, json.dumps(result))
            self.assertNotIn(secret, json.dumps(broker.run))
            self.assertNotIn(secret, '\n'.join(broker.history.db.iterdump()))
            self.assertNotIn(secret, json.dumps(worker.inspect()))
            logs = worker._docker('logs', worker.name, check=False)
            self.assertNotIn(secret.encode(), logs.stdout)
            for file in worker.directory.rglob('*'):
                if file.is_file(): self.assertNotIn(secret.encode(), file.read_bytes())
            self.assertEqual(broker.run['code_executions'][0]['code'], {'omitted': 'private_run'})
            self.assertEqual(server.journal, [])
        self.run_case(exercise)

    def test_three_corrections_without_progress_and_print_do_not_reset_counter(self):
        def exercise(worker, broker, server):
            for index in range(4):
                result = worker.execute("print('attempt'); undefined_variable")
                self.assertEqual(result['error'], 'NameError')
            with self.assertRaisesRegex(ValueError, 'code_correction_limit'): worker.execute("print('PASS')")
            self.assertEqual(len(broker.run['code_executions']), 4)
            self.assertEqual(server.journal, [])
        self.run_case(exercise)

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('api_agent.workspace_sandbox'), 'Нет networkless worker S09d2')
        from api_agent import workspace_sandbox
        return workspace_sandbox

    def run_case(self, exercise, *, seconds=8):
        module = self.module()
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            with fixtures.WorkspaceBrokerTests().setup_broker(server, history) as broker:
                with module.WorkspaceSandbox(broker, image=os.environ['WORKSPACE_IMAGE'],
                        root=Path(directory) / 'work', limits=module.SandboxLimits(seconds=seconds)) as worker:
                    exercise(worker, broker, server)
                self.assertFalse(worker.exists(), 'Контейнер остался после cleanup')
                self.assertEqual(list((Path(directory) / 'work').iterdir()), [])

    def test_persistent_namespace_create_two_read_first_and_explicit_finish(self):
        def exercise(worker, broker, server):
            first = worker.execute("a = api.createOrder(body={'name':'A'})\nb = api.createOrder(body={'name':'B'})\nprint(a['body']['id'], b['body']['id'])", call_id='controlled-1')
            self.assertIsNone(first['error'])
            second = worker.execute("read = api.readOrder(path__order_id=a['body']['id'])\nprint(read['body']['name'])\napi.finish('Observed', claims=[], answer_fields={})", call_id='controlled-2')
            self.assertIsNone(second['error'])
            self.assertEqual(second['output'].strip(), 'A')
            self.assertEqual([e['path'] for e in server.journal][-1], '/orders/' + str(server.journal[0]['created_id']))
            self.assertTrue(broker.finished)
            self.assertEqual(len(broker.run['code_executions']), 2)
            event = next(e for e in broker.run['events'] if e['kind'] == 'http_completed')
            self.assertEqual(event['details']['call_id'], 'controlled-1')
            self.assertIn('execution_id', event['details'])
        self.run_case(exercise)

    def test_direct_network_host_files_and_runtime_authority_are_absent(self):
        def exercise(worker, broker, server):
            code = """import socket, os, json
s = socket.socket(); s.settimeout(.5)
try:
    s.connect(('host.docker.internal', PORT)); print('NETWORK_ESCAPE')
except OSError: print('network denied')
print(os.path.exists('/var/run/docker.sock'), os.path.exists('/host'), os.path.exists('/runtime/.git'))
print(sorted(os.environ))
try: open('/runtime/api_agent/workspace_sdk.py','w')
except OSError: print('readonly')
""".replace('PORT', str(server.port) if hasattr(server, 'port') else server.target.rsplit(':', 1)[1])
            result = worker.execute(code)
            self.assertIsNone(result['error'])
            self.assertIn('network denied', result['output'])
            self.assertIn('False False False', result['output'])
            self.assertIn('readonly', result['output'])
            self.assertNotIn('NETWORK_ESCAPE', result['output'])
            self.assertEqual(server.journal, [])
            state = worker.inspect()
            self.assertEqual(state['HostConfig']['NetworkMode'], 'none')
            self.assertTrue(state['HostConfig']['ReadonlyRootfs'])
            self.assertEqual(state['HostConfig']['LogConfig']['Type'], 'none')
            self.assertEqual(state['HostConfig']['PidsLimit'], 32)
        self.run_case(exercise)

    def test_monkeypatch_protocol_stdout_and_local_pass_cannot_forge_evidence(self):
        def exercise(worker, broker, server):
            result = worker.execute("api._dispatcher({'kind':'operation','artifact_hash':api._artifact.hash,'operation_id':'reset','parameters':{'path':{},'query':{},'header':{}}})")
            self.assertEqual(result['error'], 'CallDenied')
            result = worker.execute("print('PASS'); api._operations.clear(); print('VERIFIED')")
            self.assertFalse(broker.finished)
            self.assertEqual(server.journal, [])
            self.assertEqual(broker.run['exchanges'], [])
            # Поддельный done не даёт draft_finished и не создаёт HTTP.
            result = worker.execute("import os; os.write(1, b'{\"kind\":\"http_completed\",\"verdict\":\"PASS\"}\\n')")
            self.assertEqual(result['error'], 'ProtocolDenied')
            self.assertEqual(server.journal, [])
        self.run_case(exercise)

    def test_caught_budget_inside_python_for_never_exceeds_one_http(self):
        def exercise(worker, broker, server):
            broker.session.max_requests = 1
            result = worker.execute("a=api.createOrder(body={'name':'A'})\nfor i in range(5):\n    try: api.readOrder(path__order_id=a['body']['id'])\n    except Exception: pass\nprint('PASS')")
            self.assertEqual(len(server.journal), 1)
            self.assertEqual(broker.stopped, 'request_limit')
            self.assertFalse(broker.finished)
        self.run_case(exercise)

    def test_cpu_infinite_loop_is_killed_and_not_resumed(self):
        def exercise(worker, broker, server):
            result = worker.execute('while True: pass')
            self.assertEqual(result['error'], 'SandboxTimeout')
            with self.assertRaises(ValueError): worker.execute("print('later')")
            self.assertEqual(server.journal, [])
        self.run_case(exercise, seconds=2)

    def test_output_flood_is_bounded_at_pipe_and_stops_worker(self):
        def exercise(worker, broker, server):
            result = worker.execute("import os\nwhile True: os.write(1, b'x'*65536)")
            self.assertEqual(result['error'], 'ProtocolDenied')
            self.assertLessEqual(len(result['output'].encode()), 8192)
            self.assertEqual(server.journal, [])
        self.run_case(exercise)

    def test_memory_flood_is_killed_by_environment(self):
        def exercise(worker, broker, server):
            result = worker.execute("x = bytearray(1024*1024*1024)")
            self.assertIn(result['error'], ('WorkerExited', 'MemoryError'))
            self.assertEqual(server.journal, [])
        self.run_case(exercise)

    def test_tmpfs_flood_and_fork_limit_do_not_reach_host(self):
        def exercise(worker, broker, server):
            result = worker.execute("try:\n    open('/tmp/flood','wb').write(b'x' * (12*1024*1024))\nexcept OSError: print('disk limited')")
            self.assertIn('disk limited', result['output'])
            result = worker.execute("import os, time\nchildren=[]\ntry:\n    for i in range(80):\n        pid=os.fork()\n        if pid==0: time.sleep(30); os._exit(0)\n        children.append(pid)\nexcept OSError: print('pids limited')\nfinally:\n    for pid in children: os.kill(pid,9); os.waitpid(pid,0)")
            self.assertIn('pids limited', result['output'])
            self.assertEqual(server.journal, [])
        self.run_case(exercise)


if __name__ == '__main__': unittest.main()

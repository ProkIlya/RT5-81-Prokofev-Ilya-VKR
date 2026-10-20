"""Контрпримеры R01–R03 независимого ревью: реальные HTTP и SQLite."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.openapi import parse_document, load_source
from api_agent.policy import ProjectConfig
from api_agent.storage import SQLiteHistory
from agent_support import reply, count_test_tokens
from demo_api.server import DemoServer
import test_catalog_execution as support


class RedactionOriginTests(unittest.TestCase):
    def test_http_metadata_lookalikes_never_expose_secret_values(self):
        for envelope in ('operation', 'catalog_operation'):
            for secret in (314159265, 'unknown-review-canary', {'private': 'nested-review-canary', 'code': 271828182}):
                with self.subTest(envelope=envelope, secret_type=type(secret).__name__), DemoServer() as server, tempfile.TemporaryDirectory() as folder:
                    doc = json.loads((ROOT / 'tests/fixtures/auth_openapi.json').read_text(encoding='utf-8'))
                    doc['paths'].update(json.loads((ROOT / 'tests/fixtures/demo_openapi.json').read_text(encoding='utf-8'))['paths'])
                    cat = parse_document(doc)
                    config = ProjectConfig.create('p', server.target, cat, mode='TEST', resettable=True,
                        operations={'loginUser': 'C', 'getPet': 'A'})
                    wf = CatalogWorkflow(cat, config, goal='Read pet', conditions={'getPet': [support.status_condition(200)]},
                        extraction={'loginUser': {'/token': {'type': 'string', 'secret': True}}},
                        auth={'login_operation': 'loginUser', 'token_path': '/token',
                              'body': {'login': 'fixture', 'password': 'fixture-password'}})
                    server.pet = {'id': 1, 'name': 'Murka', envelope: {'operation_id': 'untrusted',
                        'method': 'GET', 'path': '/x', 'responses': {}, 'token': secret}}
                    seen, snapshots, events = [], [], []
                    def model(payload):
                        seen.append(deepcopy(payload))
                        exchanges = [json.loads(m['content'])['data'] for m in payload['messages'] if m['role'] == 'tool']
                        if not exchanges:
                            return reply('execute_operation', {'operation_id': 'getPet'})
                        if len(exchanges) == 1:
                            return reply('read_response', {'exchange_id': exchanges[0]['exchange_id']})
                        return reply('finish_run', {'summary': 'Read', 'evidence_ids': exchanges[0]['finish_evidence_ids']})
                    path = Path(folder) / 'history.sqlite3'
                    with SQLiteHistory(path) as history:
                        run = StartAgentRun(server.target, cat, workflow=wf, history_store=history,
                            token_counter=count_test_tokens, trace_sink=snapshots.append, event_sink=events.append).start(
                                'p', 's', 'm', wf.goal, model)
                        self.assertEqual(run['status'], 'completed', run['stop_reason'])
                        self.assertEqual(run['contract'], cat.snapshot)
                        self.assertEqual(run['exchanges'][0]['response']['json'][envelope]['token'], '[REDACTED]')
                        self.assertEqual(run['requests'], 1)
                        self.assertEqual(run['steps'][1]['action']['name'], 'read_response')
                        self.assertEqual(history.read_run('p', 's', run['run_id']), run)
                    canary = str(secret) if isinstance(secret, int) else secret if isinstance(secret, str) else 'nested-review-canary'
                    self.assertNotIn(canary, json.dumps([run, seen, snapshots, events]))
                    self.assertNotIn(canary.encode(), path.read_bytes())

    def test_narrow_trusted_inspect_keeps_token_schema(self):
        from api_agent.agent.chains import AuthRead
        cat = load_source(str(ROOT / 'tests/fixtures/auth_openapi.json'))
        with DemoServer() as server:
            run = StartAgentRun(server.target, cat, workflow=AuthRead(), token_counter=count_test_tokens).start(
                'p', 's', 'm', 'Login and read profile',
                lambda _: reply('inspect_operation', {'operation_id': 'loginUser'}))
            operation = run['steps'][0]['observation']['data']['operation']
            self.assertEqual(operation['responses']['200']['content']['application/json']['schema']['properties']['token'], {'type': 'string'})
            self.assertEqual(server.connections, 0)


class WitnessIdentityTests(unittest.TestCase):
    def test_wrong_response_entity_never_authorizes_dependent_mutation(self):
        for mode, expected_orders in [('state_id_mismatch', 1), ('good', 2)]:
            with self.subTest(mode=mode), DemoServer() as server:
                server.mode = mode
                cat = load_source(str(ROOT / 'tests/fixtures/chain_openapi.json'))
                config = ProjectConfig.create('p', server.target, cat, mode='TEST', resettable=True,
                    operations={'createOrder': 'C', 'readOrder': 'A'})
                wf = CatalogWorkflow(cat, config, goal='Create A; verify A before creating B',
                    conditions={'createOrder': [support.status_condition(201)], 'readOrder': [support.status_condition(200)]},
                    extraction={'createOrder': {'/id': {'type': 'integer', 'secret': False}}},
                    binding_rules={'readOrder': {'order_id': {'source_operation': 'createOrder',
                        'source_occurrence': 'a', 'source_path': '/id', 'response_path': '/id'}}},
                    admission_rules={'read-a': {'target': {'parameter': 'order_id',
                        'source_operation': 'createOrder', 'source_occurrence': 'a', 'source_path': '/id'}},
                        'b': {'witnesses': [{'source_operation': 'readOrder', 'source_occurrence': 'read-a',
                                             'path': '/name', 'equals': 'A'}]}},
                    occurrences=[{'id': 'a', 'operation_id': 'createOrder', 'body': {'name': 'A'}},
                        {'id': 'read-a', 'operation_id': 'readOrder'},
                        {'id': 'b', 'operation_id': 'createOrder', 'body': {'name': 'B'}}])
                def model(payload):
                    exchanges = [json.loads(m['content'])['data'] for m in payload['messages'] if m['role'] == 'tool']
                    if not exchanges:
                        return reply('execute_operation', {'operation_id': 'createOrder', 'occurrence_id': 'a', 'body': {'name': 'A'}})
                    if len(exchanges) == 1:
                        return reply('execute_operation', {'operation_id': 'readOrder', 'occurrence_id': 'read-a',
                            'bindings': {'order_id': exchanges[0]['runtime_values'][0]['ref']}})
                    if len(exchanges) == 2:
                        return reply('execute_operation', {'operation_id': 'createOrder', 'occurrence_id': 'b', 'body': {'name': 'B'}})
                    return reply('finish_run', {'summary': 'done', 'evidence_ids': exchanges[-1]['finish_evidence_ids']})
                run = StartAgentRun(server.target, cat, workflow=wf, token_counter=count_test_tokens).start(
                    'p', 's', 'm', wf.goal, model)
                self.assertEqual(len(server.orders), expected_orders)
                if mode == 'state_id_mismatch':
                    self.assertEqual(run['stop_reason'], 'precondition_not_verified')
                    self.assertEqual(len(server.journal), 2)
                    self.assertEqual(run['steps'][1]['observation']['data']['chain_check']['verdict'], 'fail')
                else:
                    self.assertEqual(run['status'], 'completed', run['stop_reason'])


class UncertainContinuationTests(unittest.TestCase):
    def test_crash_after_durable_prepare_blocks_new_mutation(self):
        with DemoServer() as server, tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'history.sqlite3'
            worker = '''
import os,sys
sys.path[:0]=['src','tests','.']
from api_agent.agent import StartAgentRun
from api_agent.storage import SQLiteHistory
from agent_support import count_test_tokens
import test_catalog_execution as support
class Target:
 target=sys.argv[2]
cat,_,wf,create,read=support.GenericExecutionTests().workflow(Target())
def event(value):
 if value['kind']=='http_prepared':os._exit(9)
with SQLiteHistory(sys.argv[1]) as history:
 StartAgentRun(sys.argv[2],cat,workflow=wf,history_store=history,token_counter=count_test_tokens,event_sink=event).start('p','s','first',wf.goal,support.GenericModel(create,read))
'''
            process = subprocess.run([sys.executable, '-X', 'utf8', '-c', worker, str(path), server.target],
                cwd=ROOT, capture_output=True, timeout=20)
            self.assertEqual(process.returncode, 9, process.stderr.decode())
            self.assertEqual(server.connections, 0)
            cat, _, wf, create, read = support.GenericExecutionTests().workflow(server)
            with SQLiteHistory(path) as history:
                retry = StartAgentRun(server.target, cat, workflow=wf, history_store=history,
                    token_counter=count_test_tokens).start('p', 's', 'retry', wf.goal, support.GenericModel(create, read))
                self.assertEqual(server.connections, 0)
                self.assertEqual(retry['stop_reason'], 'unresolved_uncertain_write')
                self.assertEqual(history.lookup('p', 's', 'first')['primary_safety_cause'], 'uncertain_write')

    def test_same_process_new_message_also_preserves_uncertain_lock(self):
        with DemoServer() as server:
            server.mode = 'timeout_after_commit'
            cat, _, wf, create, read = support.GenericExecutionTests().workflow(server)
            original = wf.make_session
            def short(*args):
                session = original(*args)
                session.timeout = 0.05
                return session
            wf.make_session = short
            app = StartAgentRun(server.target, cat, workflow=wf, token_counter=count_test_tokens)
            first = app.start('p', 's', 'first', wf.goal, support.GenericModel(create, read))
            retry = app.start('p', 's', 'retry', wf.goal, support.GenericModel(create, read), parent_run_id=first['run_id'])
            self.assertEqual(len(server.orders), 1)
            self.assertEqual(retry['requests'], 0)
            self.assertEqual(retry['stop_reason'], 'unresolved_uncertain_write')

    def test_uncertain_project_allows_reads_and_other_projects(self):
        with DemoServer() as server, tempfile.TemporaryDirectory() as folder:
            server.mode = 'timeout_after_commit'
            cat, _, wf, create, read = support.GenericExecutionTests().workflow(server)
            original = wf.make_session
            def short(*args):
                session = original(*args)
                session.timeout = 0.05
                return session
            wf.make_session = short
            path = Path(folder) / 'history.sqlite3'
            with SQLiteHistory(path) as history:
                StartAgentRun(server.target, cat, workflow=wf, history_store=history,
                    token_counter=count_test_tokens).start('p', 's', 'first', wf.goal, support.GenericModel(create, read))
            server.mode = 'good'
            identifier = next(iter(server.orders))
            with SQLiteHistory(path) as history:
                config = ProjectConfig.create('p', server.target, cat, mode='READ_ONLY', resettable=True,
                    operations={read: 'A'}, version=2)
                reader = CatalogWorkflow(cat, config, goal='Read the persisted order', conditions={read: [support.status_condition(200)]})
                def read_model(payload):
                    observed = [json.loads(m['content'])['data'] for m in payload['messages'] if m['role'] == 'tool']
                    if not observed:
                        return reply('execute_operation', {'operation_id': read, 'parameters': {'order_id': identifier}})
                    return reply('finish_run', {'summary': 'Read', 'evidence_ids': observed[-1]['finish_evidence_ids']})
                read_run = StartAgentRun(server.target, cat, workflow=reader, history_store=history,
                    token_counter=count_test_tokens).start('p', 'diagnostic', 'read', reader.goal, read_model)
                self.assertEqual(read_run['status'], 'completed', read_run['stop_reason'])
                self.assertEqual(len(server.orders), 1)
                config = ProjectConfig.create('other', server.target, cat, mode='TEST', resettable=True, operations={create: 'C'})
                other = CatalogWorkflow(cat, config, goal='Create B', conditions={create: [support.status_condition(201)]})
                def other_model(payload):
                    observed = [json.loads(m['content'])['data'] for m in payload['messages'] if m['role'] == 'tool']
                    if not observed:
                        return reply('execute_operation', {'operation_id': create, 'body': {'name': 'B'}})
                    return reply('finish_run', {'summary': 'Created B', 'evidence_ids': observed[-1]['finish_evidence_ids']})
                other_run = StartAgentRun(server.target, cat, workflow=other, history_store=history,
                    token_counter=count_test_tokens).start('other', 's', 'other', other.goal, other_model)
                self.assertEqual(other_run['status'], 'completed', other_run['stop_reason'])
                self.assertEqual(len(server.orders), 2)

    def test_known_completed_effect_can_be_explicitly_retested(self):
        with DemoServer() as server, tempfile.TemporaryDirectory() as folder:
            cat, _, wf, create, read = support.GenericExecutionTests().workflow(server)
            path = Path(folder) / 'history.sqlite3'
            with SQLiteHistory(path) as history:
                first = StartAgentRun(server.target, cat, workflow=wf, history_store=history,
                    token_counter=count_test_tokens).start('p', 's', 'first', wf.goal, support.GenericModel(create, read))
            with SQLiteHistory(path) as history:
                second = StartAgentRun(server.target, cat, workflow=wf, history_store=history,
                    token_counter=count_test_tokens).start('p', 's', 'retest', wf.goal, support.GenericModel(create, read), parent_run_id=first['run_id'])
                self.assertEqual([first['status'], second['status']], ['completed', 'completed'])
                self.assertEqual(len(server.orders), 2)

    def test_restart_new_message_cannot_repeat_uncertain_post(self):
        for linked in (True, False):
            with self.subTest(linked=linked), DemoServer() as server, tempfile.TemporaryDirectory() as folder:
                server.mode = 'timeout_after_commit'
                cat, _, wf, create, read = support.GenericExecutionTests().workflow(server)
                original = wf.make_session
                def short_session(*args):
                    session = original(*args)
                    session.timeout = 0.05
                    return session
                wf.make_session = short_session
                path = Path(folder) / 'history.sqlite3'
                with SQLiteHistory(path) as history:
                    first = StartAgentRun(server.target, cat, workflow=wf, history_store=history,
                        token_counter=count_test_tokens).start('p', 's', 'first', wf.goal, support.GenericModel(create, read))
                before = server.connections
                with SQLiteHistory(path) as history:
                    second = StartAgentRun(server.target, cat, workflow=wf, history_store=history,
                        token_counter=count_test_tokens).start('p', 's' if linked else 'another', 'retry', wf.goal, support.GenericModel(create, read),
                            **({'parent_run_id': first['run_id']} if linked else {}))
                    self.assertEqual(len(server.orders), 1)
                    self.assertEqual(server.connections, before)
                    self.assertEqual(second['requests'], 0)
                    self.assertEqual(second['stop_reason'], 'unresolved_uncertain_write')
                    self.assertEqual(history.read_run('p', 's', first['run_id']), first)


if __name__ == '__main__':
    unittest.main()

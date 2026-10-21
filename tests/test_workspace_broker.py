"""Broker проверяется настоящим HTTP и SQLite, независимо от Python worker."""
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent.catalog import CatalogWorkflow, CatalogLimits
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig
from api_agent.storage.history import SQLiteHistory
from api_agent.workspace_sdk import build_sdk
from demo_api.server import DemoServer
from test_catalog_execution import status_condition


class WorkspaceBrokerTests(unittest.TestCase):
    def test_broker_requires_prebuilt_project_sdk_and_rejects_changed_configuration(self):
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            broker = self.setup_broker(server, history)
            module = self.module()
            self.assertIn('artifact', inspect.signature(module.WorkspaceBroker).parameters, 'SDK должен поступать от Project setup')
            changed = ProjectConfig.create('p', server.target, broker.catalog, mode='TEST', resettable=True,
                operations={'createOrder': 'C', 'readOrder': 'A'}, version=2)
            broker.workflow.config = changed
            with self.assertRaisesRegex(ValueError, 'artifact_denied'):
                module.WorkspaceBroker(broker.workflow, history, session_id='s', message_id='m', artifact=broker.artifact)
            self.assertEqual(server.journal, [])

    def test_private_literal_in_sdk_metadata_blocks_before_persistence_or_worker(self):
        secret = 'ClassifiedPassword7321'
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            doc = json.loads((ROOT / 'tests/fixtures/chain_openapi.json').read_text())
            doc['paths']['/orders']['post']['description'] = secret
            catalog = parse_document(doc)
            config = ProjectConfig.create('p', server.target, catalog, mode='TEST', resettable=True, operations={'createOrder': 'C'})
            workflow = CatalogWorkflow(catalog, config, goal='Create', conditions={'createOrder': [status_condition(201)]})
            workflow.secrets = lambda: (secret,)
            with self.assertRaisesRegex(ValueError, 'contract_redaction_conflict'):
                with self.module().WorkspaceBroker(workflow, history, artifact=build_sdk(catalog, config), session_id='s', message_id='m'): pass
            self.assertEqual(server.journal, [])
            self.assertNotIn(secret, '\n'.join(history.db.iterdump()))

    def test_application_ask_fills_only_accepted_input_and_locks_after_http(self):
        from api_agent.goal_workflow import GoalWorkflow
        from api_agent.goals import compile_goal
        from api_agent.interaction import HumanInteraction
        from api_agent.validation import fingerprint
        from test_goal_application import pet_proposal
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            catalog = parse_document(json.loads((ROOT / 'tests/fixtures/chain_openapi.json').read_text()))
            config = ProjectConfig.create('p', server.target, catalog, mode='TEST', resettable=True, operations={'createOrder': 'C'})
            goal = 'Create with the name chosen by the user'
            proposal = pet_proposal(catalog, config, goal)
            proposal['slots'][0].update(operation_id='createOrder', body={'name': {'input': 'chosen'}})
            proposal['conditions'][0].update(predicate='json_value_equals', parameters={'path': '/name', 'value': {'input': 'chosen'}})
            proposal['input_slots'] = [{'id': 'chosen', 'type': 'string'}]
            accepted = compile_goal(catalog, config, goal, proposal,
                confirmation={'proposal_hash': fingerprint(proposal), 'approved': True, 'complete': True})
            human = HumanInteraction(lambda request: {'request_id': request['id'], 'value': 'A'})
            workflow = GoalWorkflow(catalog, config, accepted, proposal=proposal, human=human, budget=None, preparation={})
            with self.module().WorkspaceBroker(workflow, history, artifact=build_sdk(catalog, config), session_id='s', message_id='m') as broker:
                api = broker.artifact.bind(broker.dispatch)
                with self.assertRaisesRegex(ValueError, 'clarification_required'): api.createOrder(body={'name': 'A'})
                self.assertEqual(api.ask_user('Name?', input_slot='chosen')['value'], 'A')
                api.createOrder(body={'name': 'A'})
                with self.assertRaisesRegex(ValueError, 'input_slot_locked'): api.ask_user('Change?', input_slot='chosen')
                self.assertEqual(api.check_goal()['verdict'], 'PASS')
                api.finish('Observed', claims=[], answer_fields={})
            self.assertEqual(broker.run['report']['goal_verdict'], 'PASS')
            self.assertEqual(len(server.journal), 1)

    def test_id_from_failed_source_is_not_a_verified_dependency(self):
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            broker = self.setup_broker(server, history)
            broker.workflow.conditions['createOrder'] = [status_condition(202)]
            with broker:
                api = broker.artifact.bind(broker.dispatch)
                a = api.createOrder(body={'name': 'A'})
                with self.assertRaisesRegex(ValueError, 'binding_source_denied'):
                    api.readOrder(path__order_id=a['body']['id'])
                self.assertEqual(len(server.journal), 1)

    def test_callback_exception_text_is_not_a_protocol_error_or_persisted(self):
        private = 'AuthoritySecret9153'
        def callback(_): raise ValueError(private)
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            with self.setup_broker(server, history, mode='SAFE', approval=callback) as broker:
                with self.assertRaises(ValueError) as caught: broker.artifact.bind(broker.dispatch).createOrder(body={'name': 'A'})
                self.assertNotIn(private, str(caught.exception))
                self.assertNotIn(private, '\n'.join(history.db.iterdump()))
                self.assertEqual(server.journal, [])

    def test_rejected_mutation_cannot_reask_but_independent_read_stays_available(self):
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            doc = json.loads((ROOT / 'tests/fixtures/chain_openapi.json').read_text())
            pet = json.loads((ROOT / 'tests/fixtures/demo_openapi.json').read_text())
            doc['paths'].update(pet['paths'])
            catalog = parse_document(doc)
            config = ProjectConfig.create('p', server.target, catalog, mode='SAFE', resettable=True,
                operations={'createOrder': 'C', 'getPet': 'A'})
            approvals = []
            def approve(p):
                approvals.append(p)
                return {'action_hash': p['action_hash'], 'config_version': p['config_version'], 'approved': len(approvals) > 1}
            wf = CatalogWorkflow(catalog, config, goal='Create and independent read', approval=approve,
                conditions={'createOrder': [status_condition(201)], 'getPet': [status_condition(200)]})
            with self.module().WorkspaceBroker(wf, history, artifact=build_sdk(catalog, config), session_id='s', message_id='m') as broker:
                api = broker.artifact.bind(broker.dispatch)
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, 'approval_rejected'): api.createOrder(body={'name': 'A'})
                self.assertEqual(api.getPet()['status'], 200)
                self.assertEqual(len(approvals), 1)
                self.assertEqual([e['method'] for e in server.journal], ['GET'])

    def test_fact_facade_validates_saved_snapshot_and_publishes_only_after_finalization(self):
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            with self.setup_broker(server, history) as broker:
                api = broker.artifact.bind(broker.dispatch)
                proposed = api.propose_fact({'type': 'declared_contract', 'source': 'DECLARED',
                    'operation_id': 'createOrder', 'verify': {'pointer': '/method'}})
                self.assertEqual(proposed['status'], 'pending_finalization')
                self.assertEqual(history.read_facts('p', catalog=broker.catalog), [])
                a = api.createOrder(body={'name': 'A'}); api.readOrder(path__order_id=a['body']['id'])
                api.finish('Observed', claims=[], answer_fields={})
            facts = history.read_facts('p', catalog=broker.catalog)
            self.assertTrue(any(f['value'] == 'POST' and f['status'] == 'VERIFIED' for f in facts))

    def test_accepted_occurrences_auth_epochs_and_hidden_bearer_use_same_kernel(self):
        from api_agent.goal_workflow import GoalWorkflow
        from api_agent.goals import compile_goal
        from api_agent.interaction import HumanInteraction
        from test_goal_auth import auth_catalog, auth_proposal
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            catalog = auth_catalog()
            config = ProjectConfig.create('p', server.target, catalog, mode='TEST', resettable=True,
                operations={'registerUser': 'C', 'loginUser': 'C', 'logoutUser': 'C', 'readProfile': 'A'})
            goal = 'Register/login/logout/old token 401/relogin/profile'
            proposal = auth_proposal(catalog, config, goal)
            from api_agent.validation import fingerprint
            accepted = compile_goal(catalog, config, goal, proposal,
                confirmation={'proposal_hash': fingerprint(proposal), 'approved': True, 'complete': True})
            password = 'explicit-private-password-9153'
            workflow = GoalWorkflow(catalog, config, accepted, proposal=proposal, human=HumanInteraction(), budget=None,
                preparation={}, auth={'login_operation': 'loginUser', 'token_path': '/token', 'body': {'login': 'new-user', 'password': password},
                    'public_body_fields': ['login'], 'logout_operations': ['logoutUser']},
                extraction={'loginUser': {'/token': {'type': 'string', 'secret': True}}}, private_bodies={'registerUser': {'password': password}})
            with self.module().WorkspaceBroker(workflow, history, artifact=build_sdk(catalog, config), session_id='s', message_id='m') as broker:
                api = broker.artifact.bind(broker.dispatch)
                with self.assertRaises(ValueError): api.readProfile()
                api.registerUser(body={'login': 'new-user'})
                self.assertNotEqual(broker.run['policy_decisions'][0]['arguments_hash'], fingerprint({
                    'operation_id': 'registerUser', 'occurrence_id': 's0', 'parameters': {},
                    'body': {'login': 'new-user', 'password': password}}), 'Обычный hash позволяет перебирать приватный пароль')
                login = api.loginUser(body=None)
                self.assertNotIn(server.auth_token, json.dumps(login))
                api.logoutUser()
                self.assertEqual(api.readProfile()['status'], 401)
                api.loginUser(body=None)
                self.assertEqual(api.readProfile()['status'], 200)
                checklist = api.check_goal()
                self.assertEqual(checklist['verdict'], 'PASS')
                api.finish('Observed', claims=[], answer_fields={})
            self.assertEqual([e['response']['status'] for e in broker.run['exchanges']], [201, 200, 200, 401, 200, 200])
            dump = '\n'.join(history.db.iterdump())
            self.assertNotIn(password, dump)
            self.assertNotIn(server.auth_token, dump)
            self.assertNotIn(password, json.dumps(broker.run))
            self.assertNotIn(server.auth_token, json.dumps(broker.run))

    def test_stale_approval_never_connects(self):
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            current = []
            def approve(proposal):
                old = current[0]
                current[0] = ProjectConfig.create('p', server.target, old.catalog, mode='SAFE', resettable=True,
                    operations={'createOrder': 'C', 'readOrder': 'A'}, version=2)
                return {'approved': True, 'action_hash': proposal['action_hash'], 'config_version': proposal['config_version']}
            with self.setup_broker(server, history, mode='SAFE', approval=approve,
                    current_config=lambda: current[0].config if isinstance(current[0], CatalogWorkflow) else current[0]) as broker:
                current.append(broker.workflow)
                with self.assertRaisesRegex(ValueError, 'stale_approval'):
                    broker.artifact.bind(broker.dispatch).createOrder(body={'name': 'A'})
                self.assertEqual(server.journal, [])

    def test_timeout_after_committed_write_survives_catch_and_new_message(self):
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            server.mode = 'timeout_after_commit'
            with self.setup_broker(server, history) as broker:
                broker.session.timeout = .1
                api = broker.artifact.bind(broker.dispatch)
                result = api.createOrder(body={'name': 'A'})
                self.assertEqual(result['outcome'], 'transport_error')
                for _ in range(2):
                    with self.assertRaisesRegex(ValueError, 'uncertain_write'): api.createOrder(body={'name': 'B'})
                run_id = broker.run['run_id']
            self.assertTrue(history.read_run('p', 's', run_id)['uncertain_write'])
            with self.setup_broker(server, history, message_id='new-message') as broker:
                with self.assertRaisesRegex(ValueError, 'unresolved_uncertain_write'):
                    broker.artifact.bind(broker.dispatch).createOrder(body={'name': 'B'})
            self.assertEqual(len(server.journal), 1)

    def test_same_message_cannot_replay_and_unknown_fact_cannot_be_verified(self):
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            with self.setup_broker(server, history) as broker:
                api = broker.artifact.bind(broker.dispatch)
                with self.assertRaises(ValueError): api.propose_fact({'status': 'VERIFIED', 'evidence_ids': ['foreign']})
                self.assertEqual(server.journal, [])
            with self.assertRaisesRegex(ValueError, 'message_already_executed'):
                with self.setup_broker(server, history): pass
            self.assertEqual(server.journal, [])

    def test_namespaces_reach_distinct_path_query_and_header_on_wire(self):
        journal = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                journal.append((self.path, self.headers.get('x-example')))
                status, body = ((401, b'{"error":"denied"}') if 'id=unauthorized' in self.path else
                    (204, b'') if 'id=empty' in self.path else (200, b'{') if 'id=invalid' in self.path else (200, b'{"ok":true}'))
                self.send_response(status); self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            doc = {'openapi': '3.0.3', 'info': {'title': 'Wire', 'version': '1'}, 'paths': {'/items/{id}': {'get': {
                'operationId': 'read', 'parameters': [
                    {'in': 'path', 'name': 'id', 'required': True, 'schema': {'type': 'integer'}},
                    {'in': 'query', 'name': 'id', 'schema': {'type': 'string'}},
                    {'in': 'header', 'name': 'x-example', 'schema': {'type': 'string'}}],
                'responses': {'200': {'description': 'ok', 'content': {'application/json': {'schema': {'type': 'object'}}}}}}}}}
            catalog = parse_document(doc)
            config = ProjectConfig.create('p', f'http://127.0.0.1:{server.server_port}', catalog, mode='READ_ONLY', resettable=False, operations={'read': 'A'})
            self.assertIn('parameter_namespaces', inspect.signature(CatalogWorkflow).parameters, 'Нет namespace admission')
            wf = CatalogWorkflow(catalog, config, goal='wire', conditions={'read': [status_condition(200)]}, parameter_namespaces=True)
            with tempfile.TemporaryDirectory() as directory, SQLiteHistory(Path(directory) / 'h.db') as history:
                with self.module().WorkspaceBroker(wf, history, artifact=build_sdk(catalog, config), session_id='s', message_id='m') as broker:
                    request = {'artifact_hash': broker.artifact.hash, 'kind': 'operation', 'operation_id': 'read',
                        'parameters': {'path': {'id': 7}, 'query': {'id': 'different'}, 'header': {'x-example': 'test'}}}
                    self.assertEqual(broker.dispatch(request)['status'], 200)
                    self.assertEqual(journal, [('/items/7?id=different', 'test')])
                    self.assertEqual(broker.run['policy_decisions'][-1]['preview'].get('parameters'),
                        {'path__id': 7, 'query__id': 'different', 'header__x-example': 'test'})
                    for location, key, value in [('header', 'host', 'elsewhere'), ('header', 'x-example', 'bad\r\nInjected: x'), ('path', 'id', '7')]:
                        attack = json.loads(json.dumps(request)); attack['parameters'][location][key] = value
                        with self.assertRaises(ValueError): broker.dispatch(attack)
                    self.assertEqual(len(journal), 1)
                    api = broker.artifact.bind(broker.dispatch)
                    denied = api.read(path__id=7, query__id='unauthorized')
                    self.assertEqual(denied['status'], 401)
                    self.assertEqual(denied['body'], {'error': 'denied'})
                    empty = api.read(path__id=7, query__id='empty')
                    self.assertEqual(empty['status'], 204)
                    self.assertIsNone(empty['body'])
                    self.assertFalse(empty['json_valid'])
                    invalid = api.read(path__id=7, query__id='invalid')
                    self.assertIsNone(invalid['body'])
                    self.assertFalse(invalid['json_valid'])
                    changed = json.loads(json.dumps(request)); changed['parameters']['header']['x-example'] = 'other'
                    self.assertEqual(broker.dispatch(changed)['status'], 200)
                    self.assertEqual(journal[-1], ('/items/7?id=different', 'other'))
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('api_agent.workspace_broker'), 'Нет внешнего broker S09d2')
        from api_agent import workspace_broker
        return workspace_broker

    def setup_broker(self, server, history, **options):
        catalog = parse_document(json.loads((ROOT / 'tests/fixtures/chain_openapi.json').read_text()))
        config = ProjectConfig.create('p', server.target, catalog, mode=options.pop('mode', 'TEST'), resettable=True,
            operations={'createOrder': 'C', 'readOrder': 'A'})
        workflow = CatalogWorkflow(catalog, config, goal='Create A and read it',
            conditions={'createOrder': [status_condition(201)], 'readOrder': [status_condition(200)]},
            extraction={'createOrder': {'/id': {'type': 'integer', 'secret': False}}},
            binding_rules={'readOrder': {'order_id': {'source_operation': 'createOrder', 'source_path': '/id'}}},
            approval=options.pop('approval', None), current_config=options.pop('current_config', None))
        return self.module().WorkspaceBroker(workflow, history, artifact=build_sdk(catalog, config), session_id='s', message_id=options.pop('message_id', 'm'), **options)

    def test_plain_id_is_bound_to_own_exchange_and_local_body_is_not_evidence(self):
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            with self.setup_broker(server, history) as broker:
                api = broker.artifact.bind(broker.dispatch)
                first = api.createOrder(body={'name': 'A'})
                first['body']['name'] = 'fake'
                second = api.readOrder(path__order_id=first['body']['id'])
                self.assertEqual(second['body']['name'], 'A')
                self.assertEqual(len(server.journal), 2)
                self.assertEqual(broker.run['exchanges'][0]['response']['json']['name'], 'A')
                self.assertEqual(broker.run['exchanges'][1]['bound_inputs']['order_id'], first['body']['id'])
                draft = api.finish('PASS', claims=[], answer_fields={})
                self.assertEqual(draft['kind'], 'draft_finished')
                self.assertEqual(broker.run['chain_verdict'], 'pass')
            saved = history.read_run('p', 's', broker.run['run_id'])
            self.assertEqual(len(saved['exchanges']), 2)
            self.assertTrue(any(e['kind'] == 'http_completed' for e in saved['events']))

    def test_forged_id_stale_artifact_and_protocol_authority_fields_never_connect(self):
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            with self.setup_broker(server, history) as broker:
                api = broker.artifact.bind(broker.dispatch)
                api.createOrder(body={'name': 'A'})
                with self.assertRaisesRegex(ValueError, 'binding_source_denied'):
                    api.readOrder(path__order_id=999)
                message = {'kind': 'operation', 'artifact_hash': broker.artifact.hash,
                    'operation_id': 'readOrder', 'parameters': {'path': {'order_id': 1}, 'query': {}, 'header': {}}}
                for changed in ({**message, 'artifact_hash': 'forged'}, {**message, 'target': 'http://elsewhere'},
                                {**message, 'credential_ref': 'forged'}, {**message, 'operation_id': 'reset'}):
                    with self.assertRaises(ValueError): broker.dispatch(changed)
                self.assertEqual(len(server.journal), 1)

    def test_caught_request_budget_and_cancel_remain_latched(self):
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            cancel = threading.Event()
            with self.setup_broker(server, history, limits=CatalogLimits(max_requests=1), cancel=cancel) as broker:
                api = broker.artifact.bind(broker.dispatch)
                value = api.createOrder(body={'name': 'A'})
                for _ in range(3):
                    with self.assertRaisesRegex(ValueError, 'request_limit'): api.readOrder(path__order_id=value['body']['id'])
                self.assertEqual(len(server.journal), 1)
            with self.setup_broker(server, history, message_id='m2', cancel=cancel) as broker:
                cancel.set()
                for index in range(2):
                    with self.assertRaisesRegex(ValueError, 'cancelled'): broker.artifact.bind(broker.dispatch).createOrder(body={'name': 'B'})
                    cancel.clear()
                self.assertEqual(len(server.journal), 1)

    def test_rejected_approval_allows_independent_read_but_not_mutation(self):
        with tempfile.TemporaryDirectory() as directory, DemoServer() as server, SQLiteHistory(Path(directory) / 'h.db') as history:
            with self.setup_broker(server, history, mode='SAFE', approval=lambda p: {'approved': False}) as broker:
                api = broker.artifact.bind(broker.dispatch)
                with self.assertRaisesRegex(ValueError, 'approval_rejected'): api.createOrder(body={'name': 'A'})
                with self.assertRaises(ValueError): api.readOrder(path__order_id=999)
                self.assertEqual(server.journal, [])
                self.assertTrue(any(e['kind'] == 'call_rejected' for e in broker.run['events']))


if __name__ == '__main__': unittest.main()

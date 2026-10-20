"""S08Rc: два применения операции сохраняют разные сущности и одну фазу tools."""
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.policy import ProjectConfig
from api_agent.openapi import load_source
from demo_api.server import DemoServer
from agent_support import reply, count_test_tokens
from test_catalog_execution import status_condition


def order_workflow(server, second='B'):
    catalog = load_source(str(ROOT / 'tests/fixtures/chain_openapi.json'))
    config = ProjectConfig.create('p', server.target, catalog, mode='TEST', resettable=True,
        operations={'createOrder': 'C', 'readOrder': 'A'})
    workflow = CatalogWorkflow(catalog, config, goal='Создай A, B и прочитай A',
        conditions={'createOrder': [status_condition(201)], 'readOrder': [status_condition(200)]},
        extraction={'createOrder': {'/id': {'type': 'integer', 'secret': False}}},
        binding_rules={'readOrder': {'order_id': {'source_operation': 'createOrder',
            'source_path': '/id', 'source_occurrence': 'a', 'response_path': '/id'}}},
        occurrences=[{'id': 'a', 'operation_id': 'createOrder', 'body': {'name': 'A'}},
                     {'id': 'b', 'operation_id': 'createOrder', 'body': {'name': second}},
                     {'id': 'read-a', 'operation_id': 'readOrder'}])
    return catalog, workflow


class OccurrenceTests(unittest.TestCase):
    def test_logout_relogin_invalidates_old_epoch_and_completes_new_occurrences(self):
        from api_agent.openapi import parse_document
        doc = json.loads((ROOT / 'tests/fixtures/auth_openapi.json').read_text(encoding='utf-8'))
        profile = doc['paths']['/users/profile']['get']
        profile['responses']['401'] = {'description': 'denied', 'content': {'application/json':
            {'schema': {'type': 'object', 'properties': {'error': {'type': 'string'}}}}}}
        doc['paths']['/users/logout'] = {'post': {'operationId': 'logoutUser',
            'security': [{'bearerAuth': []}], 'responses': {'200': {'description': 'ok',
            'content': {'application/json': {'schema': {'type': 'object',
                'properties': {'logged_out': {'type': 'boolean'}}}}}}}}}
        with DemoServer() as server:
            cat = parse_document(doc)
            config = ProjectConfig.create('p', server.target, cat, mode='TEST', resettable=True,
                operations={'loginUser': 'C', 'logoutUser': 'C', 'readProfile': 'A'})
            denied = {**status_condition(401), 'intent': 'negative'}
            wf = CatalogWorkflow(cat, config, goal='Войди, выйди, проверь отказ, войди и прочитай профиль',
                conditions={'loginUser': [status_condition(200)], 'logoutUser': [status_condition(200)],
                            'readProfile': [status_condition(200)]},
                extraction={'loginUser': {'/token': {'type': 'string', 'secret': True}}},
                auth={'login_operation': 'loginUser', 'token_path': '/token',
                      'logout_operations': ['logoutUser'],
                      'body': {'login': 'fixture', 'password': 'fixture-password'}},
                occurrences=[{'id': 'in1', 'operation_id': 'loginUser'},
                    {'id': 'out', 'operation_id': 'logoutUser'},
                    {'id': 'guest', 'operation_id': 'readProfile', 'auth_mode': 'anonymous_negative', 'conditions': [denied]},
                    {'id': 'in2', 'operation_id': 'loginUser'},
                    {'id': 'profile', 'operation_id': 'readProfile'}])
            def model(payload):
                observations = [json.loads(m['content'])['data'] for m in payload['messages'] if m['role'] == 'tool']
                exchanges = [o for o in observations if 'exchange_id' in o]
                index = len(exchanges)
                if index == 5:
                    return reply('finish_run', {'summary': 'Вход и отзыв проверены.', 'evidence_ids': exchanges[-1]['finish_evidence_ids']})
                slot = wf.occurrences[index]
                args = {'operation_id': slot['operation_id'], 'occurrence_id': slot['id']}
                if index in (1, 4):
                    args['credential_ref'] = exchanges[0 if index == 1 else 3]['runtime_values'][0]['ref']
                return reply('execute_operation', args)
            run = StartAgentRun(server.target, cat, workflow=wf, token_counter=count_test_tokens).start('p', 's', 'm', wf.goal, model)
            self.assertEqual(run['status'], 'completed', run['stop_reason'])
            self.assertEqual([e['response']['status'] for e in run['exchanges']], [200, 200, 401, 200, 200])
            self.assertGreater(run['exchanges'][-1]['request_principal']['auth_epoch'], run['exchanges'][1]['request_principal']['auth_epoch'])
            self.assertEqual(run['report']['checks_verdict'], 'PASS')

    def test_create_two_objects_then_read_first_and_keep_all_evidence(self):
        with DemoServer() as server:
            catalog, wf = order_workflow(server)
            payloads = []
            def model(payload):
                payloads.append(payload)
                observations = [json.loads(m['content'])['data'] for m in payload['messages'] if m['role'] == 'tool']
                exchanges = [o for o in observations if 'exchange_id' in o]
                if len(exchanges) < 2:
                    slot = 'a' if not exchanges else 'b'
                    return reply('execute_operation', {'operation_id': 'createOrder',
                        'occurrence_id': slot, 'body': {'name': slot.upper()}})
                if len(exchanges) == 2:
                    return reply('execute_operation', {'operation_id': 'readOrder',
                        'occurrence_id': 'read-a', 'bindings': {'order_id': exchanges[0]['runtime_values'][0]['ref']}})
                return reply('finish_run', {'summary': 'Созданы две заявки, прочитана A.',
                    'evidence_ids': exchanges[-1]['finish_evidence_ids']})
            run = StartAgentRun(server.target, catalog, workflow=wf, token_counter=count_test_tokens).start(
                'p', 's', 'm', wf.goal, model)
            self.assertEqual(run['status'], 'completed', run['stop_reason'])
            self.assertEqual(len(server.orders), 2)
            self.assertEqual(run['exchanges'][-1]['response']['json']['name'], 'A')
            self.assertEqual([e['occurrence_id'] for e in run['exchanges']], ['a', 'b', 'read-a'])
            second = next(t for t in payloads[1]['tools'] if t['function']['name'] == 'execute_operation')
            self.assertEqual(second['function']['parameters']['properties']['operation_id']['enum'], ['createOrder'])

    def test_partial_body_phase_never_forces_unoffered_http_tool(self):
        with DemoServer() as server:
            catalog, wf = order_workflow(server)
            run = {'steps': [{'action': {'name': 'inspect_operation', 'arguments': {'operation_id': 'readOrder'}},
                'observation': {'data': {'checks_complete': True, 'response_body_complete': False}}}]}
            decision = wf.phase_decision(run, wf.registry(catalog))
            names = {t['function']['name'] for t in decision.tools}
            self.assertEqual(names, {'read_response', 'finish_run'})
            self.assertIn(decision.tool_choice['function']['name'], names)

    def test_same_semantic_post_in_different_slot_is_still_rejected(self):
        from api_agent.agent.catalog import CatalogSession, CatalogLimits
        with DemoServer() as server:
            catalog, wf = order_workflow(server, second='A')
            session = CatalogSession(server.target, catalog, wf,
                {'run_id': 'run', 'project_id': 'p', 'expectations': []}, CatalogLimits())
            session.execute({'operation_id': 'createOrder', 'occurrence_id': 'a', 'body': {'name': 'A'}})
            with self.assertRaisesRegex(ValueError, 'duplicate_semantic_action'):
                session.execute({'operation_id': 'createOrder', 'occurrence_id': 'b', 'body': {'name': 'A'}})
            self.assertEqual(len(server.journal), 1)


if __name__ == '__main__':
    unittest.main()

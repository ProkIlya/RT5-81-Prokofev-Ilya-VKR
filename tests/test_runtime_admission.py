"""S08Rd: неизвестные witnesses и неверный entity запрещают HTTP до отправки."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent.catalog import CatalogWorkflow, CatalogSession, CatalogLimits
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig
from api_agent.validation import validate_exchange
from demo_api.server import DemoServer
from test_catalog_execution import status_condition


def admission_session(server, rules):
    doc = json.loads((ROOT / 'tests/fixtures/chain_openapi.json').read_text(encoding='utf-8'))
    catalog = parse_document(doc)
    config = ProjectConfig.create('p', server.target, catalog, mode='TEST', resettable=True,
        operations={'createOrder': 'C', 'readOrder': 'A'})
    wf = CatalogWorkflow(catalog, config, goal='Проверь целевую заявку',
        conditions={'createOrder': [status_condition(201)], 'readOrder': [status_condition(200)]},
        admission_rules=rules)
    run = {'run_id': 'r', 'project_id': 'p', 'expectations': [], 'validations': []}
    return wf, CatalogSession(server.target, catalog, wf, run, CatalogLimits())


def assessed_create(wf, session):
    exchange = session.execute({'operation_id': 'createOrder', 'body': {'name': 'A'}})
    prepared = wf.prepared(session.catalog, 'm', wf.goal)['createOrder']
    session.run['validations'].append(validate_exchange(session.catalog, exchange, prepared))
    return exchange


class RuntimeAdmissionTests(unittest.TestCase):
    def test_collection_composition_and_completeness_gate_real_mutation(self):
        for items, complete, allowed in [([{'id': 7}], True, True),
                                          ([{'id': 7}], False, False),
                                          ([{'id': 8}], True, False)]:
            with self.subTest(items=items, complete=complete), DemoServer() as server:
                server.pet = {'items': items, 'complete': complete}
                doc = json.loads((ROOT / 'tests/fixtures/chain_openapi.json').read_text(encoding='utf-8'))
                doc['paths']['/pets/1'] = {'get': {'operationId': 'inventory', 'responses': {'200':
                    {'description': 'ok', 'content': {'application/json': {'schema': {'type': 'object',
                        'properties': {'items': {'type': 'array', 'items': {'type': 'object',
                            'properties': {'id': {'type': 'integer'}}}}, 'complete': {'type': 'boolean'}}}}}}}}}
                cat = parse_document(doc)
                config = ProjectConfig.create('p', server.target, cat, mode='TEST', resettable=True,
                    operations={'inventory': 'A', 'createOrder': 'C'})
                wf = CatalogWorkflow(cat, config, goal='Создай при точном составе',
                    conditions={'inventory': [status_condition(200)], 'createOrder': [status_condition(201)]},
                    admission_rules={'createOrder': {'witnesses': [{'source_operation': 'inventory',
                        'path': '/items', 'equals': [{'id': 7}], 'complete_path': '/complete'}]}})
                session = CatalogSession(server.target, cat, wf,
                    {'run_id': 'r', 'project_id': 'p', 'expectations': [], 'validations': []}, CatalogLimits())
                exchange = session.execute({'operation_id': 'inventory'})
                session.run['validations'].append(validate_exchange(cat, exchange, wf.prepared(cat, 'm', wf.goal)['inventory']))
                if allowed:
                    session.execute({'operation_id': 'createOrder', 'body': {'name': 'A'}})
                else:
                    with self.assertRaisesRegex(ValueError, 'precondition_not_verified'):
                        session.execute({'operation_id': 'createOrder', 'body': {'name': 'A'}})
                self.assertEqual(len(server.orders), int(allowed))
                self.assertEqual(len(server.journal), 1 + int(allowed))

    def test_target_is_bound_to_actual_source_not_guessed_numeric_id(self):
        rules = {'readOrder': {'target': {'parameter': 'order_id',
            'source_operation': 'createOrder', 'source_path': '/id'}}}
        with DemoServer() as server:
            wf, session = admission_session(server, rules)
            source = assessed_create(wf, session)
            actual = source['response']['json']['id']
            before = server.connections
            with self.assertRaisesRegex(ValueError, 'target_not_applicable'):
                session.execute({'operation_id': 'readOrder', 'parameters': {'order_id': actual + 1}})
            self.assertEqual(server.connections, before)
            response = session.execute({'operation_id': 'readOrder', 'parameters': {'order_id': actual}})
            self.assertEqual(response['response']['status'], 200)

    def test_false_unknown_or_incomplete_witness_never_sends_dependent_request(self):
        for expected, corrupt in [('B', False), ('A', True)]:
            with self.subTest(expected=expected, corrupt=corrupt), DemoServer() as server:
                rules = {'readOrder': {'witnesses': [{'source_operation': 'createOrder',
                    'path': '/name', 'equals': expected}]}}
                wf, session = admission_session(server, rules)
                source = assessed_create(wf, session)
                if corrupt:
                    source['truncated'] = True
                before = server.connections
                with self.assertRaisesRegex(ValueError, 'precondition_not_verified'):
                    session.execute({'operation_id': 'readOrder', 'parameters': {'order_id': source['response']['json']['id']}})
                self.assertEqual(server.connections, before)

    def test_missing_source_does_not_turn_fixture_verified_into_runtime_proof(self):
        with DemoServer() as server:
            _, session = admission_session(server, {'createOrder': {'witnesses': [
                {'source_operation': 'readOrder', 'path': '/name', 'equals': 'A'}]}})
            with self.assertRaisesRegex(ValueError, 'precondition_not_verified'):
                session.execute({'operation_id': 'createOrder', 'body': {'name': 'B'}})
            self.assertEqual(server.connections, 0)

    def test_true_witness_permits_request_and_collection_needs_explicit_completeness(self):
        with DemoServer() as server:
            wf, session = admission_session(server, {'readOrder': {'witnesses': [
                {'source_operation': 'createOrder', 'path': '/name', 'equals': 'A'}]}})
            source = assessed_create(wf, session)
            response = session.execute({'operation_id': 'readOrder', 'parameters': {'order_id': source['response']['json']['id']}})
            self.assertEqual(response['response']['status'], 200)
        with DemoServer() as server:
            with self.assertRaisesRegex(ValueError, 'collection_completeness_required'):
                admission_session(server, {'readOrder': {'witnesses': [
                    {'source_operation': 'createOrder', 'path': '/items', 'equals': []}]}})


if __name__ == '__main__':
    unittest.main()

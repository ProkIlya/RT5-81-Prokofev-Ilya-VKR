"""Модель видит несовпадение связанного объекта, даже если HTTP и schema верны."""
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent import StartAgentRun
from agent_support import count_test_tokens
from demo_api.server import DemoServer
import test_catalog_execution as helpers
from api_agent.state import RunState


class ChainObservationTests(unittest.TestCase):
    def test_actual_binding_mismatch_reaches_model_with_expected_and_actual(self):
        for prefix in ('alpha', 'omega'):
            for mode in ('good', 'state_id_mismatch'):
                with self.subTest(prefix=prefix, mode=mode), DemoServer() as server:
                    server.mode = mode
                    cat, _, wf, create, read = helpers.GenericExecutionTests().workflow(server, prefix)
                    wf.binding_rules[read]['order_id']['response_path'] = '/id'
                    model = helpers.GenericModel(create, read)
                    observed = []
                    def capture(payload):
                        observed[:] = [json.loads(m['content']) for m in payload['messages'] if m['role'] == 'tool']
                        return model(payload)
                    run = StartAgentRun(server.target, cat, workflow=wf, token_counter=count_test_tokens).start(
                        'p', 's', 'm', wf.goal, capture)
                    data = observed[-1]['data']
                    wanted = 'pass' if mode == 'good' else 'fail'
                    self.assertEqual(run['chain_verdict'], wanted)
                    self.assertIn('chain_verdict', data['machine_checks'], 'Модель не получила результат проверки связанного объекта')
                    self.assertEqual(data['machine_checks']['chain_verdict'], wanted)
                    self.assertEqual(data['machine_checks']['expectation_verdict'], wanted)
                    comparison = data['chain_check']['comparisons'][0]
                    self.assertEqual(comparison['expected'], server.journal[0]['created_id'])
                    self.assertEqual(comparison['actual'], server.journal[0]['created_id'] + (mode != 'good'))
                    self.assertEqual(comparison['response_path'], '/id')
                    self.assertEqual(comparison['parameter'], 'order_id')
                    self.assertEqual(data['http_response']['status_code'], 200)
                    self.assertEqual(len(server.journal), 2)

    def test_binding_diagnostic_never_exposes_secret_response_value(self):
        with DemoServer() as server:
            _, _, wf, _, read = helpers.GenericExecutionTests().workflow(server)
            wf.binding_rules[read]['order_id']['response_path'] = '/id'
            state = RunState('p', 'r', {read: {'/id': {'type':'string','secret':True}}})
            exchange = dict(id='e',run_id='r',operation_id=read,bound_inputs={'order_id':7},
                response={'json':{'id':'private-response-value'}})
            state.protect(exchange)
            check = wf.binding_check(SimpleNamespace(state=state), exchange)
            self.assertEqual(check['verdict'], 'fail')
            self.assertEqual(check['comparisons'][0]['actual'], '[REDACTED]')
            self.assertNotIn('private-response-value', json.dumps(check))

    def test_missing_binding_response_path_is_explicit_failure(self):
        with DemoServer() as server:
            _, _, wf, _, read = helpers.GenericExecutionTests().workflow(server)
            wf.binding_rules[read]['order_id']['response_path'] = '/id'
            exchange = dict(id='e',operation_id=read,bound_inputs={'order_id':7},response={'json':{}})
            check = wf.binding_check(SimpleNamespace(state=RunState('p','r',{})), exchange)
            self.assertEqual(check['verdict'], 'fail')
            self.assertFalse(check['comparisons'][0]['actual_present'])


if __name__ == '__main__':
    unittest.main()

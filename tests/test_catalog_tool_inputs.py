"""Схема вызова не предлагает неприменимые body/auth и сохраняет нужные inputs."""

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.openapi import load_source
from api_agent.policy import ProjectConfig
from agent_support import count_test_tokens, reply
from demo_api.server import DemoServer
from test_catalog_execution import status_condition
import test_catalog_execution as execution
import test_catalog_prerequisites as prerequisites


def execution_inputs(workflow, catalog):
    return next(t['function']['parameters']['properties'] for t in workflow.registry(catalog)
                if t['function']['name'] == 'execute_operation')


class CatalogToolInputTests(unittest.TestCase):
    def test_read_without_inputs_does_not_advertise_body_auth_or_refs(self):
        with DemoServer() as server:
            cat = load_source(str(ROOT / 'tests/fixtures/demo_openapi.json'))
            cfg = ProjectConfig.create('p', server.target, cat, mode='READ_ONLY',
                                       resettable=True, operations={'getPet':'A'})
            wf = CatalogWorkflow(cat, cfg, goal='Observe', conditions={'getPet':[status_condition(200)]})
            self.assertEqual(set(execution_inputs(wf, cat)), {'operation_id'})
            calls = []
            def model(payload):
                calls.append(payload)
                if len(calls) == 1:
                    return reply('execute_operation', {'operation_id':'getPet', 'body':{}})
                if len(calls) == 2:
                    self.assertEqual(server.connections, 0)
                    return reply('execute_operation', {'operation_id':'getPet'})
                data = json.loads(payload['messages'][-1]['content'])['data']
                return reply('finish_run', {'summary':'Observed', 'evidence_ids':data['finish_evidence_ids']})
            run = StartAgentRun(server.target, cat, workflow=wf, token_counter=count_test_tokens).start('p','s','m',wf.goal,model)
            self.assertEqual(run['status'], 'completed')
            self.assertEqual(run['requests'], 1)

    def test_generic_write_and_reference_inputs_are_retained(self):
        with DemoServer() as server:
            cat, _, wf, _, _ = execution.GenericExecutionTests().workflow(server)
            self.assertEqual(set(execution_inputs(wf, cat)), {'operation_id','body','parameters','bindings'})

    def test_private_login_body_is_not_a_model_input(self):
        with DemoServer() as server:
            cat, wf = prerequisites.PrerequisiteTests().auth_workflow(server)
            inputs = execution_inputs(wf, cat)
            self.assertNotIn('body', inputs)
            self.assertIn('credential_ref', inputs)


if __name__ == '__main__':
    unittest.main()

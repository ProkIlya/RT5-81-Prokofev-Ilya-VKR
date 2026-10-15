"""В trace и payload записывается выбранная модель, а не прежний hardcoded alias."""

import inspect
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'),str(ROOT/'tests'),str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.openapi import load_source
from demo_api.server import DemoServer
from agent_support import ObserverModel, count_test_tokens


class ModelIdentityTests(unittest.TestCase):
    def test_plain_loop_uses_same_non_thinking_options_as_catalog(self):
        with DemoServer() as server:
            cat = load_source(str(ROOT/'tests/fixtures/demo_openapi.json'))
            delegate = ObserverModel()
            def model(payload):
                self.assertEqual(payload.get('chat_template_kwargs'), {'enable_thinking': False})
                self.assertIs(payload.get('parallel_tool_calls'), False)
                return delegate(payload)
            run = StartAgentRun(server.target, cat, token_counter=count_test_tokens).start('p','s','m','Check pet',model)
            self.assertEqual(run['status'], 'completed')
            self.assertEqual(run['model']['request_options']['chat_template_kwargs'], {'enable_thinking': False})

    def test_selected_model_reaches_native_payload_and_saved_trace(self):
        self.assertIn('model_name',inspect.signature(StartAgentRun).parameters,'Выбор модели не отражается в Run')
        with DemoServer() as server:
            cat=load_source(str(ROOT/'tests/fixtures/demo_openapi.json'))
            delegate=ObserverModel()
            def model(payload):
                self.assertEqual(payload['model'],'candidate-local')
                return delegate(payload)
            run=StartAgentRun(server.target,cat,model_name='candidate-local',token_counter=count_test_tokens).start('p','s','m','Check pet',model)
            self.assertEqual(run['status'],'completed')
            self.assertEqual(run['model']['name'],'candidate-local')


if __name__=='__main__':
    unittest.main()

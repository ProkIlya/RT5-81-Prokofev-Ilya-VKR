"""Модель выбирает evidence из текущего Run вместо переписывания UUID вручную."""

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.openapi import load_source
from agent_support import count_test_tokens, reply
from demo_api.server import DemoServer


class EvidenceChoiceTests(unittest.TestCase):
    def test_finish_schema_contains_only_observed_exchange_ids(self):
        with DemoServer() as server:
            cat = load_source(str(ROOT / 'tests/fixtures/demo_openapi.json'))
            captured = []
            def model(payload):
                captured.append(payload)
                if len(captured) == 1:
                    return reply('execute_operation', {'operation_id':'getPet'})
                observed = json.loads(payload['messages'][-1]['content'])['data']['exchange_id']
                return reply('finish_run', {'summary':'Observed', 'evidence_ids':[observed]})
            run = StartAgentRun(server.target, cat, token_counter=count_test_tokens).start('p','s','m','Check pet',model)
            self.assertEqual(run['status'], 'completed')
            finish = next(t for t in captured[-1]['tools'] if t['function']['name']=='finish_run')
            self.assertEqual(finish['function']['parameters']['properties']['evidence_ids']['items'].get('enum'), [run['exchanges'][0]['id']])
            first = next(t for t in captured[0]['tools'] if t['function']['name']=='finish_run')
            self.assertNotIn('enum', first['function']['parameters']['properties']['evidence_ids']['items'])


if __name__ == '__main__':
    unittest.main()

"""CLI S09b использует application preparation, без ручного goal adapter."""
from contextlib import redirect_stdout
import io
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent.__main__ import main
from api_agent.openapi import load_source
from api_agent.policy import ProjectConfig
from agent_support import count_test_tokens, reply
from demo_api.server import DemoServer
from test_goal_application import pet_proposal, PrimaryModel


class GoalCliTests(unittest.TestCase):
    def test_generated_goal_cli_and_duplicate_use_same_saved_run(self):
        self.assertIn('human_reader', inspect.signature(main).parameters, 'CLI S09b ещё не подключён')
        with DemoServer() as server, tempfile.TemporaryDirectory() as folder:
            cat = load_source(str(ROOT / 'tests/fixtures/demo_openapi.json'))
            cfg = ProjectConfig.create('p', server.target, cat, mode='READ_ONLY', resettable=True, operations={'getPet': 'A'})
            config_path = Path(folder) / 'config.json'; cfg.save(config_path)
            goal = 'Покажи имя питомца и убедись, что сервис отвечает успешно.'
            calls = []
            def model(payload):
                calls.append(payload)
                if payload['tools'][0]['function']['name'] == 'propose_goal': return reply('propose_goal', pet_proposal(cat, cfg, goal))
                return PrimaryModel()(payload)
            argv = [goal, '--generate-goal', '--project-config', str(config_path), '--target', server.target,
                    '--spec', str(ROOT / 'tests/fixtures/demo_openapi.json'), '--evidence-dir', folder, '--message-id', 'm']
            reader = lambda request: {'request_id': request['id'], 'value': 'yes'}
            with redirect_stdout(io.StringIO()) as output:
                code = main(argv, model=model, token_counter=count_test_tokens, human_reader=reader)
            self.assertEqual(code, 0, output.getvalue())
            first = json.loads(output.getvalue())
            self.assertEqual(first['report']['goal_verdict'], 'PASS')
            count = len(calls)
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(argv, model=model, token_counter=count_test_tokens, human_reader=reader), 0)
            self.assertEqual(json.loads(output.getvalue())['run_id'], first['run_id'])
            self.assertEqual(len(calls), count)
            self.assertEqual(len(server.journal), 1)


if __name__ == '__main__': unittest.main()

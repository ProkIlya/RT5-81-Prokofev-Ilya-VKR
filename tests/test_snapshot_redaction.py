"""S08Ra: публичный контракт неизменен, секреты ответа не становятся metadata."""

from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.agent.summary_review import FinalSummaryReviewer
from api_agent.openapi import load_source
from api_agent.policy import ProjectConfig
from api_agent.storage import SQLiteHistory
from agent_support import reply, count_test_tokens
from demo_api.server import DemoServer
from test_catalog_execution import status_condition


def auth_workflow(server):
    catalog = load_source(str(ROOT / 'tests/fixtures/auth_openapi.json'))
    config = ProjectConfig.create('p', server.target, catalog, mode='TEST',
        resettable=True, operations={'loginUser': 'C', 'readProfile': 'A'})
    workflow = CatalogWorkflow(catalog, config, goal='Войди и проверь профиль',
        conditions={'loginUser': [status_condition(200)], 'readProfile': [status_condition(200)]},
        extraction={'loginUser': {'/token': {'type': 'string', 'secret': True}}},
        auth={'login_operation': 'loginUser', 'token_path': '/token',
              'body': {'login': 'fixture', 'password': 'fixture-password'}})
    return catalog, workflow


class AuthDecisions:
    """Контролируемые решения; транспорт и SQLite настоящие, это не live LLM."""

    def __call__(self, payload):
        observations = [json.loads(m['content'])['data'] for m in payload['messages']
                        if m['role'] == 'tool']
        exchanges = [o for o in observations if 'exchange_id' in o]
        if not exchanges:
            return reply('execute_operation', {'operation_id': 'loginUser'})
        if len(exchanges) == 1:
            return reply('execute_operation', {'operation_id': 'readProfile',
                'credential_ref': exchanges[0]['runtime_values'][0]['ref']})
        return reply('finish_run', {'summary': 'Профиль прочитан, проверки пройдены.',
            'evidence_ids': exchanges[-1]['finish_evidence_ids']})


class SnapshotRedactionTests(unittest.TestCase):
    def test_auth_sqlite_reviewer_restart_preserves_snapshot_without_replay(self):
        with tempfile.TemporaryDirectory() as folder, DemoServer() as server:
            path = Path(folder) / 'history.sqlite3'
            catalog, workflow = auth_workflow(server)
            checkpoints, payloads = [], []
            decisions = AuthDecisions()

            def model(payload):
                payloads.append(deepcopy(payload))
                return decisions(payload)

            def reviewer(payload):
                payloads.append(deepcopy(payload))
                return reply('review_summary', {'model_verdict': 'PASS'})

            with SQLiteHistory(path) as history:
                run = StartAgentRun(server.target, catalog, workflow=workflow,
                    token_counter=count_test_tokens, history_store=history,
                    trace_sink=checkpoints.append,
                    summary_reviewer=FinalSummaryReviewer(reviewer)).start('p', 's', 'm', workflow.goal, model)
                self.assertEqual(run['status'], 'completed', run['stop_reason'])
                self.assertEqual(run['review_model_calls'], 1)
                self.assertEqual(run['requests'], 2)
                self.assertTrue(server.journal[-1]['auth_matched'])
                for snapshot in [run, *checkpoints]:
                    self.assertEqual(snapshot['contract'], catalog.snapshot)
                    self.assertEqual(snapshot['spec_hash'], catalog.spec_hash)
                self.assertEqual(history.snapshot('p', 's', run['run_id'])['contract'], catalog.snapshot)
                changed = deepcopy(run)
                changed['contract']['operations']['loginUser']['responses']['200']['content']['application/json']['schema']['properties']['token'] = {'type': 'integer'}
                with self.assertRaisesRegex(ValueError, 'run_identity_changed'):
                    history.save_snapshot(changed)
            before = (server.connections, len(payloads))
            with SQLiteHistory(path) as history:
                duplicate = StartAgentRun(server.target, catalog, workflow=workflow,
                    token_counter=count_test_tokens, history_store=history).start(
                        'p', 's', 'm', workflow.goal, lambda _: self.fail('Повтор LLM'))
                self.assertEqual(duplicate, run)
                self.assertEqual(history.run_for_message('p', 's', run['response_message_id']), run)
                self.assertIsNotNone(history.episode('p', 's', run['run_id']))
            self.assertEqual(before, (server.connections, len(payloads)))
            for secret in (server.auth_token, 'fixture-password'):
                self.assertNotIn(secret, json.dumps([run, checkpoints, payloads]))
                self.assertNotIn(secret.encode(), path.read_bytes())


if __name__ == '__main__':
    unittest.main()

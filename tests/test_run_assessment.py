"""S08Re: предыдущий дефект и uncertain write не теряются в позднем итоге."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.openapi import load_source
from api_agent.storage import SQLiteHistory
from api_agent.validation import report, prepare_expectations, validate_exchange
from demo_api.server import DemoServer
from agent_support import count_test_tokens
import test_catalog_execution as support


class RunAssessmentTests(unittest.TestCase):
    def test_early_intent_fail_survives_later_pass_and_incomplete_chain(self):
        cat = load_source(str(ROOT / 'tests/fixtures/demo_openapi.json'))
        conditions = prepare_expectations(cat, None, 'm', 'Check pet')
        exchanges = [{'id': str(status), 'run_id': 'r', 'operation_id': 'getPet', 'outcome': 'received',
            'response': {'status': status, 'content_type': 'application/json', 'json_valid': True,
                         'json': {'id': 1, 'name': 'Murka'}}} for status in (404, 200)]
        validations = [validate_exchange(cat, exchange, conditions) for exchange in exchanges]
        run = {'goal': 'Check pet', 'status': 'cancelled', 'stop_reason': 'cancelled', 'steps': [],
               'exchanges': exchanges, 'validations': validations, 'validation': validations[-1], 'chain_verdict': 'not_checked'}
        self.assertEqual(report(run)['checks_verdict'], 'FAIL')
        self.assertEqual(report(run)['intent_exchange_id'], '404')

    def test_catalog_read_only_returns_canonical_block_before_model_or_http(self):
        with DemoServer() as server:
            cat, _, wf, _, _ = support.GenericExecutionTests().workflow(server, mode='READ_ONLY')
            run = StartAgentRun(server.target, cat, workflow=wf, token_counter=count_test_tokens).start(
                'p', 's', 'm', wf.goal, lambda _: self.fail('LLM при доказанном запрете'))
            self.assertEqual(run['stop_reason'], 'policy_denied')
            self.assertEqual(run['status'], 'blocked')
            self.assertIn('READ_ONLY', run['report']['summary'])
            self.assertEqual(server.connections, 0)

    def test_known_occurrence_budget_shortfall_stops_before_mutation(self):
        import test_action_occurrences as occurrence_support
        from api_agent.agent.catalog import CatalogLimits
        with DemoServer() as server:
            cat, wf = occurrence_support.order_workflow(server)
            run = StartAgentRun(server.target, cat, workflow=wf, limits=CatalogLimits(max_requests=2),
                token_counter=count_test_tokens).start('p', 's', 'm', wf.goal,
                    lambda _: self.fail('LLM при недостаточном минимальном бюджете'))
            self.assertEqual(run['stop_reason'], 'request_limit')
            self.assertEqual(server.connections, 0)

    def test_recovery_keeps_uncertain_primary_cause_before_finalization(self):
        with DemoServer() as server, tempfile.TemporaryDirectory() as folder:
            server.mode = 'timeout_after_commit'
            cat, _, wf, create, read = support.GenericExecutionTests().workflow(server)
            original = wf.make_session
            def short_session(*args):
                session = original(*args)
                session.timeout = 0.05
                return session
            wf.make_session = short_session
            run = StartAgentRun(server.target, cat, workflow=wf, token_counter=count_test_tokens).start(
                'p', 's', 'crash', wf.goal, support.GenericModel(create, read))
            run.pop('primary_safety_cause', None)
            run['stop_reason'] = 'malformed_limit'
            path = Path(folder) / 'history.sqlite3'
            with SQLiteHistory(path) as history:
                history.begin_run('p', 's', 'crash', run, environment='default', configuration={'version': 1}, metadata={})
            before = server.connections
            with SQLiteHistory(path) as history:
                with history.execution():
                    restored = history.lookup('p', 's', 'crash')
                self.assertEqual(restored.get('primary_safety_cause'), 'uncertain_write')
                self.assertEqual(restored['stop_reason'], 'uncertain_write')
                self.assertIn('неизвестен', restored['report']['summary'])
            self.assertEqual(server.connections, before)

    def test_bad_then_good_preserves_contract_violations_without_double_count(self):
        cat = load_source(str(ROOT / 'tests/fixtures/demo_openapi.json'))
        conditions = prepare_expectations(cat, None, 'm', 'Check pet')
        exchanges, validations = [], []
        for identifier, value in [('bad', 'wrong'), ('good', 1)]:
            exchange = {'id': identifier, 'run_id': 'r', 'operation_id': 'getPet', 'outcome': 'received',
                'response': {'status': 200, 'content_type': 'application/json', 'json_valid': True,
                             'json': {'id': value, 'name': 'Murka'}}}
            exchanges.append(exchange)
            validations.append(validate_exchange(cat, exchange, conditions))
        run = {'goal': 'Check pet', 'status': 'partial', 'stop_reason': 'cancelled', 'steps': [],
               'exchanges': exchanges, 'validation': validations[-1], 'validations': validations}
        result = report(run)
        self.assertEqual(result['contract_verdict'], 'mismatch')
        self.assertEqual(result['confirmed_contract_violations'], 1)
        self.assertEqual(result['issues'][0]['exchange_id'], 'bad')
        # Чтение сохранённого ответа не создаёт новую оценку того же exchange.
        run['validations'].append(deepcopy(validations[0]))
        self.assertEqual(report(run)['confirmed_contract_violations'], 1)

    def test_timeout_after_commit_remains_primary_after_reviewer_failure_and_restart(self):
        class BrokenReviewer:
            def review(self, *args):
                raise RuntimeError('private-review-error')
        with DemoServer() as server, tempfile.TemporaryDirectory() as folder:
            server.mode = 'timeout_after_commit'
            cat, _, wf, create, read = support.GenericExecutionTests().workflow(server)
            original = wf.make_session
            def make_session(*args):
                session = original(*args)
                session.timeout = 0.05
                return session
            wf.make_session = make_session
            path = Path(folder) / 'history.sqlite3'
            with SQLiteHistory(path) as history:
                run = StartAgentRun(server.target, cat, workflow=wf, history_store=history,
                    token_counter=count_test_tokens, summary_reviewer=BrokenReviewer()).start(
                        'p', 's', 'm', wf.goal, support.GenericModel(create, read))
                self.assertEqual(run['primary_safety_cause'], 'uncertain_write')
                self.assertEqual(run['stop_reason'], 'uncertain_write')
                self.assertIn('неизвестен', run['report']['summary'])
                self.assertNotIn('private-review-error', json.dumps(run))
                self.assertEqual(run['requests'], 1)
                self.assertEqual(len(server.orders), 1)
            before = server.connections
            with SQLiteHistory(path) as history:
                restored = StartAgentRun(server.target, cat, workflow=wf, history_store=history,
                    token_counter=count_test_tokens).start('p', 's', 'm', wf.goal,
                        lambda _: self.fail('LLM после restart'))
                self.assertEqual(restored['primary_safety_cause'], 'uncertain_write')
            self.assertEqual(server.connections, before)

    def test_read_only_restriction_explains_missing_mutation(self):
        run = {'goal': 'Создай объект', 'status': 'blocked', 'stop_reason': 'policy_denied',
               'steps': [], 'exchanges': [], 'policy_config': {'mode': 'READ_ONLY'}}
        result = report(run)
        self.assertIn('READ_ONLY', result['summary'])
        self.assertEqual(result['goal_verdict'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()

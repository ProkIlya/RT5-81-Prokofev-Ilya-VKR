"""S09c через application/Core: HTTP, сохранённые drafts и replay guard."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.goals.application import GoalApplication
from api_agent.goals.interaction import HumanInteraction
from api_agent.openapi import load_source
from api_agent.policy import ProjectConfig
from api_agent.storage import SQLiteHistory
from agent_support import reply, count_test_tokens
from demo_api.server import DemoServer
from test_goal_application import pet_proposal


class AnswerWorkflowTests(unittest.TestCase):
    def run_case(self, *, diagnostic=False, correct=True, accurate=False):
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        path = Path(folder.name) / 'run.sqlite3'
        server = DemoServer(); server.__enter__(); self.addCleanup(server.__exit__, None, None, None)
        catalog = load_source(str(ROOT / 'tests/fixtures/demo_openapi.json'))
        config = ProjectConfig.create('p', server.target, catalog, mode='READ_ONLY', resettable=True, operations={'getPet': 'A'})
        goal = 'Проверь имя: ожидается Barsik.'
        proposal = pet_proposal(catalog, config, goal)
        proposal['conditions'].append({'id': 'c2', 'requirement_id': 'r1', 'slot': 's1',
            'predicate': 'json_value_equals', 'parameters': {'path': '/name', 'value': 'Barsik'}, 'source': 'MODEL_PROPOSED', 'basis': None})
        calls = []
        def model(payload):
            name = payload['tools'][0]['function']['name']
            if name in ('diagnose_answer', 'revise_answer'):
                calls.append(name)
                data = json.loads(payload['messages'][-1]['content'])
                if name == 'diagnose_answer':
                    s = data['signals'][0]
                    return reply(name, {'issues': [{**{k: s[k] for k in ('condition_id', 'evidence_ids')},
                        'feedback_id': s['id'], 'explanation': 'Фактическое имя Murka отличается от ожидаемого Barsik.',
                        'recommendation': 'Сообщить отличие.', 'hypothesis': None}]})
                return reply(name, {'claims': [{'condition_id': 'c1', 'verdict': 'PASS'},
                    {'condition_id': 'c2', 'verdict': 'FAIL' if correct else 'PASS'}], 'answer_fields': {'a1': 'Murka'}})
            observations = [json.loads(m['content']) for m in payload['messages'] if m['role'] == 'tool']
            exchange = next((o['data']['exchange_id'] for o in observations if (o.get('data') or {}).get('exchange_id')), None)
            if not exchange: return reply('execute_operation', {'operation_id': 'getPet', 'occurrence_id': 's1'})
            return reply('finish_run', {'summary': 'Все условия выполнены.', 'evidence_ids': [exchange],
                'claims': [{'condition_id': 'c1', 'verdict': 'PASS'}, {'condition_id': 'c2', 'verdict': 'FAIL' if accurate else 'PASS'}],
                'answer_fields': {'a1': 'Murka'}})
        with SQLiteHistory(path) as history:
            app = GoalApplication(server.target, catalog, config, history=history, generator=lambda p: reply('propose_goal', proposal),
                human=HumanInteraction(lambda q: {'request_id': q['id'], 'value': 'yes'}),
                token_counter=count_test_tokens, answer_diagnostic=diagnostic)
            run = app.start('p', 's', 'm', goal, model)
        before = len(server.journal)
        with SQLiteHistory(path) as history:
            def forbidden(p): raise AssertionError('Replay LLM')
            restored = GoalApplication(server.target, catalog, config, history=history, generator=forbidden,
                answer_diagnostic=diagnostic).start('p', 's', 'm', goal, forbidden)
        self.assertEqual(restored, run)
        self.assertEqual(len(server.journal), before)
        self.assertEqual(before, 1)
        return run, calls

    def test_false_pass_is_corrected_but_machine_fail_and_original_draft_persist(self):
        run, calls = self.run_case()
        self.assertEqual(run['status'], 'completed', run.get('stop_reason'))
        self.assertEqual(run['report']['goal_verdict'], 'FAIL')
        self.assertEqual(run['answer_feedback']['status'], 'corrected')
        self.assertEqual(calls, ['revise_answer'])
        self.assertEqual(run['report']['answer']['conditions'][1]['actual'], 'Murka')
        self.assertEqual(run['final']['claims'][1]['verdict'], 'FAIL')
        self.assertEqual(run['answer_feedback']['drafts'][0]['claims'][1]['verdict'], 'PASS')
        self.assertEqual(run['total_model_calls'], run['model_calls'] + 2)

    def test_enabled_diagnostic_then_only_one_revision_no_more_http(self):
        run, calls = self.run_case(diagnostic=True)
        self.assertEqual(calls, ['diagnose_answer', 'revise_answer'])
        self.assertEqual(run['report']['answer_review']['diagnostic'], 'advisory')
        self.assertEqual(run['report']['goal_verdict'], 'FAIL')
        self.assertEqual(run['total_model_calls'], run['model_calls'] + 3)

    def test_rejected_revision_falls_back_and_stays_rejected_after_restart(self):
        run, calls = self.run_case(correct=False)
        self.assertEqual(calls, ['revise_answer'])
        self.assertEqual(run['answer_feedback']['status'], 'canonical')
        self.assertEqual(run['final']['claims'][1]['verdict'], 'FAIL')
        self.assertEqual(len(run['answer_feedback']['drafts']), 2)

    def test_accurate_fail_claim_is_not_false_alarm_or_needless_diagnostic(self):
        run, calls = self.run_case(diagnostic=True, accurate=True)
        self.assertEqual(calls, [])
        self.assertEqual(run['answer_feedback']['status'], 'unchanged')
        self.assertEqual(run['report']['goal_verdict'], 'FAIL')

    def test_recomputed_report_does_not_trust_modified_advisory_or_candidate(self):
        from api_agent.validation import report
        run, _ = self.run_case()
        run['answer_feedback']['answer']['goal_verdict'] = 'PASS'
        run['answer_feedback']['revision']['candidate']['claims'][1]['verdict'] = 'PASS'
        fresh = report(run)
        self.assertEqual(fresh['goal_verdict'], 'FAIL')
        self.assertEqual(fresh['answer']['conditions'][1]['verdict'], 'FAIL')

    def test_expected_401_is_a_successful_condition_without_false_alarm(self):
        from test_goal_auth import GoalAuthTests
        from api_agent.goals.feedback import correct_answer
        import threading
        fixture = GoalAuthTests()
        try:
            _, source = fixture.run_auth()
            run = deepcopy(source)
            run.pop('answer_feedback', None)
            checks = run['report']['answer']['conditions']
            old_token = next(c for c in checks if c['expected'] == {'values': [401]})
            self.assertEqual(old_token['verdict'], 'PASS')
            args = next(s['action']['arguments'] for s in reversed(run['steps']) if (s.get('action') or {}).get('name') == 'finish_run')
            args['claims'] = [{'condition_id': c['condition_id'], 'verdict': c['verdict']} for c in checks]
            args['answer_fields'] = run['report']['answer']['answer_fields']
            result = correct_answer(run, lambda p: self.fail('False alarm on expected401'), None,
                deadline=10, clock=lambda: 1, cancel=threading.Event(), diagnostic=True)
            self.assertEqual(result['calls'], 0)
            self.assertEqual(result['status'], 'unchanged')
        finally: fixture.doCleanups()


if __name__ == '__main__': unittest.main()

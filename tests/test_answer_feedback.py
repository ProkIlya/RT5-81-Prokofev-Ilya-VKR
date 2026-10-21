"""S09c: реальная проверка claims, границ auxiliary и общего ledger."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent.catalog import CatalogLimits
from api_agent.agent.resilience import TokenBudget
from agent_support import reply, count_test_tokens


def example(verdict='FAIL', *, draft='PASS'):
    return {'goal': 'Проверь имя: ожидается Murka.', 'status': 'completed', 'model': {'name': 'test'},
        'stop_reason': 'finished', 'model_calls': 2, 'history': ['PRIVATE-HISTORY'],
        'admission_manifest': {'goal_contract': {'manifest': {
            'conditions': [{'id': 'c1', 'requirement_id': 'r1', 'slot': 's1', 'source': 'USER_DEFINED', 'basis': None,
                'predicate': 'json_value_equals', 'parameters': {'path': '/name', 'value': 'Murka'}}],
            'answer_fields': [{'id': 'a1', 'slot': 's1', 'path': '/name'}]}}},
        'exchanges': [{'id': 'e1', 'occurrence_id': 's1', 'response': {'json_valid': True,
            'status': 200, 'json': {'name': 'Barsik' if verdict == 'FAIL' else 'Murka', 'private': 'HIDDEN-BRANCH'}}}],
        'steps': [{'action': {'name': 'finish_run', 'arguments': {'summary': 'Всё правильно.',
            'claims': [{'condition_id': 'c1', 'verdict': draft}], 'answer_fields': {'a1': 'Barsik' if verdict == 'FAIL' else 'Murka'}}}}],
        'report': {'goal_verdict': verdict, 'checks_verdict': verdict, 'contract_verdict': 'pass',
            'goal_checklist': {'conditions': [{'condition_id': 'c1', 'requirement_id': 'r1', 'verdict': verdict,
                'expected': {'path': '/name', 'value': 'Murka'}, 'actual': 'Barsik' if verdict == 'FAIL' else 'Murka',
                'exchange_id': 'e1'}], 'answer_fields': {'a1': 'Barsik' if verdict == 'FAIL' else 'Murka'}}}}


class AnswerFeedbackTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('api_agent.answer_feedback'), 'Нет S09c feedback/correction')
        from api_agent import answer_feedback
        return answer_feedback

    def execute(self, run=None, *, diagnostic=False, model=None, limits=None, cancel=None, clock=lambda: 1,
                persist=lambda: None, safe=lambda value: value):
        run = deepcopy(run or example())
        budget = TokenBudget(limits or CatalogLimits(), count_test_tokens)
        budget.reserve({'max_tokens': 10}); budget.settle({'prompt_tokens': 100, 'completion_tokens': 3, 'total_tokens': 103}, 10)
        before = budget.upper
        result = self.api().correct_answer(run, model or (lambda p: reply('revise_answer', {
            'claims': [{'condition_id': 'c1', 'verdict': 'FAIL'}], 'answer_fields': {'a1': 'Barsik'}})),
            budget, deadline=10, clock=clock, cancel=cancel or threading.Event(), diagnostic=diagnostic, persist=persist, safe=safe)
        return run, result, budget, before

    def test_false_pass_gets_code_expected_actual_and_one_verified_correction(self):
        run, result, budget, before = self.execute()
        self.assertEqual(result['status'], 'corrected')
        self.assertEqual(result['revision']['calls'], 1)
        self.assertEqual(result['diagnostic']['status'], 'disabled')
        self.assertEqual(run['report']['goal_verdict'], 'FAIL')
        self.assertEqual(result['answer']['conditions'][0]['actual'], 'Barsik')
        self.assertEqual(result['answer']['conditions'][0]['source'], 'USER_DEFINED')
        self.assertEqual(result['drafts'][0]['claims'][0]['verdict'], 'PASS')
        self.assertGreater(budget.upper, before)

    def test_consistent_structured_draft_has_no_signal_no_model_calls(self):
        run, result, budget, before = self.execute(example('PASS', draft='PASS'))
        self.assertEqual(result['status'], 'unchanged')
        self.assertEqual(result['calls'], 0)
        self.assertEqual(result['signals'], [])
        self.assertEqual(budget.upper, before)

    def test_unknown_and_missing_fields_are_explicit_not_silently_passed(self):
        run = example('UNKNOWN', draft='UNKNOWN')
        run['report']['goal_checklist']['answer_fields'] = {}
        run['steps'][0]['action']['arguments']['answer_fields'] = {}
        run['exchanges'][0]['response']['json'] = {}
        row = run['report']['goal_checklist']['conditions'][0]
        row.update(actual=None, exchange_id=None)
        _, result, _, _ = self.execute(run)
        self.assertEqual(result['answer']['unanswered_fields'], ['a1'])
        self.assertFalse(result['answer']['conditions'][0]['actual_present'])
        self.assertEqual(result['answer']['goal_verdict'], 'UNKNOWN')
        self.assertEqual(result['calls'], 0)

    def test_diagnostic_is_advisory_scoped_and_revision_has_no_api_context(self):
        payloads = []
        def model(payload):
            payloads.append(deepcopy(payload))
            data = json.loads(payload['messages'][-1]['content'])
            if payload['tools'][0]['function']['name'] == 'diagnose_answer':
                signal = data['signals'][0]
                return reply('diagnose_answer', {'issues': [{'feedback_id': signal['id'], 'condition_id': 'c1',
                    'evidence_ids': ['e1'], 'explanation': 'Имя отличается.', 'recommendation': 'Сообщить отличие.', 'hypothesis': None}]})
            return reply('revise_answer', {'claims': [{'condition_id': 'c1', 'verdict': 'FAIL'}], 'answer_fields': {'a1': 'Barsik'}})
        _, result, _, _ = self.execute(model=model, diagnostic=True)
        self.assertEqual(result['status'], 'corrected')
        self.assertEqual(result['calls'], 2)
        self.assertEqual(result['diagnostic']['verification'], 'advisory')
        rendered = json.dumps(payloads)
        self.assertNotIn('PRIVATE-HISTORY', rendered)
        self.assertNotIn('HIDDEN-BRANCH', rendered)
        self.assertEqual([p['tools'][0]['function']['name'] for p in payloads], ['diagnose_answer', 'revise_answer'])
        self.assertTrue(all(len(p['tools']) == 1 for p in payloads))

    def test_bad_refs_new_verdict_or_http_tool_cannot_change_authority(self):
        for response in [reply('execute_operation', {'operation_id': 'deleteAll'}),
            reply('diagnose_answer', {'issues': [], 'verdict': 'PASS'}),
            reply('diagnose_answer', {'issues': [{'feedback_id': 'foreign', 'condition_id': 'c1',
                'evidence_ids': ['e1'], 'explanation': 'x', 'recommendation': 'x', 'hypothesis': None}]}),
            reply('diagnose_answer', {'issues': [{'feedback_id': 'f1', 'condition_id': 'c1',
                'evidence_ids': ['foreign'], 'explanation': 'x', 'recommendation': 'x', 'hypothesis': None}]})]:
            original = example()
            run, result, _, _ = self.execute(original, diagnostic=True, model=lambda p: response)
            self.assertEqual(result['status'], 'canonical')
            self.assertEqual(result['calls'], 1)
            self.assertEqual(run['report'], original['report'])
            self.assertEqual(run['admission_manifest'], original['admission_manifest'])

    def test_wrong_revision_is_saved_but_never_accepted_or_retried(self):
        _, result, _, _ = self.execute(model=lambda p: reply('revise_answer', {
            'claims': [{'condition_id': 'c1', 'verdict': 'PASS'}], 'answer_fields': {'a1': 'Murka'}}))
        self.assertEqual(result['status'], 'canonical')
        self.assertEqual(result['calls'], 1)
        self.assertEqual(len(result['drafts']), 2)
        self.assertEqual(result['answer']['goal_verdict'], 'FAIL')

    def test_budget_cancel_deadline_or_checkpoint_failure_prevents_auxiliary(self):
        event = threading.Event(); event.set()
        def broken(): raise OSError('PRIVATE-ERROR')
        for options in [{'limits': CatalogLimits(max_tokens_total=115)}, {'cancel': event},
                        {'clock': lambda: 11}, {'persist': broken}]:
            _, result, _, _ = self.execute(**options)
            self.assertEqual(result['status'], 'canonical')
            self.assertEqual(result['calls'], 0)
            self.assertNotIn('PRIVATE-ERROR', json.dumps(result))

    def test_exception_unknown_usage_and_late_cancel_do_not_free_failed_reserve(self):
        def broken(payload): raise OSError('PRIVATE-ERROR')
        _, result, budget, before = self.execute(model=broken)
        self.assertEqual(result['calls'], 1)
        self.assertEqual(result['status'], 'canonical')
        self.assertGreater(budget.upper, before)
        self.assertEqual(budget.snapshot()['usage_status'], 'unknown')
        self.assertNotIn('PRIVATE-ERROR', json.dumps(result))

    def test_existing_feedback_is_not_replayed(self):
        run = example(); run['answer_feedback'] = {'status': 'revision_requested', 'calls': 1}
        result = self.api().correct_answer(run, lambda p: self.fail('replay'), None,
            deadline=10, clock=lambda: 1, cancel=threading.Event())
        self.assertEqual(result, run['answer_feedback'])

    def test_private_canary_is_removed_before_model_and_storage(self):
        run = example(); run['steps'][0]['action']['arguments']['summary'] = 'secret-canary'
        captured = []
        def model(payload):
            captured.append(payload)
            return reply('revise_answer', {'claims': [{'condition_id': 'c1', 'verdict': 'FAIL'}],
                'answer_fields': {'a1': 'Barsik'}, 'private': 'secret-canary'})
        def safe(value): return json.loads(json.dumps(value).replace('secret-canary', '[REDACTED]'))
        _, result, _, _ = self.execute(run, model=model, safe=safe)
        self.assertNotIn('secret-canary', json.dumps([captured, result]))

    def test_late_cancel_or_elapsed_deadline_rejects_even_a_correct_revision(self):
        for reason in ('cancel', 'time'):
            event = threading.Event()
            current = [1]
            def model(payload):
                if reason == 'cancel': event.set()
                else: current[0] = 11
                return reply('revise_answer', {'claims': [{'condition_id': 'c1', 'verdict': 'FAIL'}], 'answer_fields': {'a1': 'Barsik'}})
            _, result, _, _ = self.execute(model=model, cancel=event, clock=lambda: current[0])
            self.assertEqual(result['status'], 'canonical')
            self.assertEqual(result['calls'], 1)

    def test_invalid_usage_never_becomes_zero_cost_or_a_verified_correction(self):
        def model(payload):
            value = reply('revise_answer', {'claims': [{'condition_id': 'c1', 'verdict': 'FAIL'}], 'answer_fields': {'a1': 'Barsik'}})
            value['usage'] = {'prompt_tokens': 99, 'completion_tokens': 1, 'total_tokens': 100}
            return value
        _, result, budget, before = self.execute(model=model)
        self.assertEqual(result['status'], 'canonical')
        self.assertEqual(result['revision']['reason'], 'token_accounting_error')
        self.assertGreater(budget.upper, before)
        self.assertEqual(budget.snapshot()['usage_status'], 'unknown')

    def test_actual_null_and_absent_are_different_and_large_values_are_marked(self):
        run = example()
        run['exchanges'][0]['response']['json']['name'] = None
        run['report']['goal_checklist']['conditions'][0]['actual'] = None
        self.assertTrue(self.api().build_feedback(run)['conditions'][0]['actual_present'])
        del run['exchanges'][0]['response']['json']['name']
        self.assertFalse(self.api().build_feedback(run)['conditions'][0]['actual_present'])
        run['report']['goal_checklist']['answer_fields']['a1'] = 'x' * 2000
        bounded = self.api().build_feedback(run)['answer_fields']['a1']
        self.assertTrue(bounded['omitted'])
        self.assertEqual(bounded['bytes'], 2002)

    def test_diagnostic_consumption_cannot_borrow_revision_budget(self):
        def model(payload):
            return reply('diagnose_answer', {'issues': [{'feedback_id': 'f1', 'condition_id': 'c1',
                'evidence_ids': ['e1'], 'explanation': 'Отличие', 'recommendation': 'Указать его', 'hypothesis': None}]})
        _, result, budget, _ = self.execute(diagnostic=True, model=model, limits=CatalogLimits(max_tokens_total=800))
        self.assertEqual(result['calls'], 1)
        self.assertEqual(result['diagnostic']['status'], 'advisory')
        self.assertEqual(result['revision']['reason'], 'token_limit')
        self.assertEqual(result['status'], 'canonical')
        self.assertLessEqual(budget.upper, 800)

    def test_changed_goal_data_does_not_change_static_role_prefix(self):
        first = self.api().payload_for('revision', {'goal': 'A'}, 'test')
        second = self.api().payload_for('revision', {'goal': 'B'}, 'test')
        self.assertEqual(first['messages'][0], second['messages'][0])
        self.assertEqual(first['tools'], second['tools'])
        self.assertNotEqual(first['messages'][1], second['messages'][1])


if __name__ == '__main__': unittest.main()

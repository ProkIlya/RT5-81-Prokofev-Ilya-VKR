"""S09c good0/bad2; сломанный oracle/подготовка дают1, не ожидаемый bad."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
F = 'test_answer_feedback.AnswerFeedbackTests.'
W = 'test_answer_workflow.AnswerWorkflowTests.'
CASES = {
    'good': [F + 'test_false_pass_gets_code_expected_actual_and_one_verified_correction',
        F + 'test_consistent_structured_draft_has_no_signal_no_model_calls',
        F + 'test_diagnostic_is_advisory_scoped_and_revision_has_no_api_context',
        F + 'test_unknown_and_missing_fields_are_explicit_not_silently_passed',
        F + 'test_changed_goal_data_does_not_change_static_role_prefix',
        W + 'test_false_pass_is_corrected_but_machine_fail_and_original_draft_persist',
        W + 'test_enabled_diagnostic_then_only_one_revision_no_more_http',
        W + 'test_accurate_fail_claim_is_not_false_alarm_or_needless_diagnostic',
        W + 'test_expected_401_is_a_successful_condition_without_false_alarm',
        'test_answer_evaluation.AnswerEvaluationTests.test_three_apis_arrays_wrappers_multiple_entities_and_holdout_use_http',
        'test_goal_cli.GoalCliTests.test_generated_goal_cli_and_duplicate_use_same_saved_run'],
    'bad': [F + 'test_bad_refs_new_verdict_or_http_tool_cannot_change_authority',
        F + 'test_wrong_revision_is_saved_but_never_accepted_or_retried',
        F + 'test_budget_cancel_deadline_or_checkpoint_failure_prevents_auxiliary',
        F + 'test_exception_unknown_usage_and_late_cancel_do_not_free_failed_reserve',
        F + 'test_late_cancel_or_elapsed_deadline_rejects_even_a_correct_revision',
        F + 'test_invalid_usage_never_becomes_zero_cost_or_a_verified_correction',
        F + 'test_private_canary_is_removed_before_model_and_storage',
        F + 'test_existing_feedback_is_not_replayed',
        F + 'test_diagnostic_consumption_cannot_borrow_revision_budget',
        W + 'test_rejected_revision_falls_back_and_stays_rejected_after_restart',
        W + 'test_recomputed_report_does_not_trust_modified_advisory_or_candidate']}


def live(directory):
    from scripts.evaluate_answer_feedback import collect
    from api_agent.agent.model import LocalModel
    from api_agent.agent.catalog import CatalogLimits
    from api_agent.agent.resilience import TokenBudget
    from api_agent.answer_feedback import correct_answer
    source = collect(directory, 'pet', 'object')
    source.pop('answer_feedback')
    source['steps'][-1]['action']['arguments']['claims'][0]['verdict'] = 'PASS'
    model = LocalModel('http://127.0.0.1:18081')
    budget = TokenBudget(CatalogLimits(), model.count_tokens)
    result = correct_answer(source, model, budget, deadline=time.monotonic() + 180,
        clock=time.monotonic, cancel=threading.Event())
    assert result['status'] == 'corrected' and result['calls'] == 1
    assert result['answer']['conditions'][0]['actual'] == 'Murka'
    assert result['answer']['goal_verdict'] == 'FAIL'
    assert budget.snapshot()['usage_status'] == 'measured'
    (directory / 'live-answer.json').write_text(json.dumps({'result': result, 'resources': budget.snapshot()}, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'kind': 'real HTTP source + real local LLM answer-only correction; source primary/generator controlled',
        'http': 1, 'http_in_correction': 0, 'calls': 1, 'status': result['status']}


def main(mode):
    directory = Path(os.environ.get('S09_EVIDENCE_DIR', ROOT / 'evidence/S09')) / ('S09c-' + mode + '-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    record = {'stage': 'S09c', 'mode': mode, 'exit_code': 1, 'independent_accept': False}
    with (directory / 'checks.log').open('w', encoding='utf-8') as stream, redirect_stdout(stream), redirect_stderr(stream):
        try:
            names = CASES[mode]
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
            record.update(tests_run=result.testsRun, skips=len(result.skipped))
            if result.wasSuccessful() and result.testsRun == len(names) and not result.skipped:
                if mode == 'good': record['live'] = live(directory)
                record['exit_code'] = 0 if mode == 'good' else 2
        except Exception as error: record['error_type'] = type(error).__name__
    record['head'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    record['branch'] = subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip()
    files = sorted(set((ROOT / 'src').rglob('*.py')) | set((ROOT / 'src').rglob('*.sql')) |
        set((ROOT / 'scripts').glob('*.py')) | set((ROOT / 'tests').rglob('*.py')) |
        set((ROOT / 'tests/fixtures').glob('*.json')) | set((ROOT / 'demo_api').glob('*.py')))
    record['source_hashes'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    (directory / 'tracked.patch').write_bytes(subprocess.check_output(['git', 'diff', '--binary'], cwd=ROOT))
    (directory / 'verification.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'stage': 'S09c', 'mode': mode, 'exit_code': record['exit_code'], 'evidence': str(directory)}))
    return record['exit_code']


if __name__ == '__main__': raise SystemExit(main(sys.argv[1]))

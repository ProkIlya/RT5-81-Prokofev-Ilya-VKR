"""S09b: отдельные gates compiler/interaction/generator и общий parent."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
C = 'test_goal_compiler.GoalCompilerTests.'
A = 'test_goal_application.GoalApplicationTests.'
H = 'test_goal_interaction.InteractionTests.'
AUTH = 'test_goal_auth.GoalAuthTests.'
CASES = {
    'S09b1': {
        'good': [C + n for n in ('test_three_unrelated_apis_auto_accept_independently_supported_form',
            'test_free_russian_goal_needs_explicit_interpretation_acceptance', 'test_rounding_uses_user_precision_decimal_half_up',
            'test_checklist_requires_exact_occurrence_actor_scope_and_all_answers')],
        'bad': [C + n for n in ('test_missing_requirement_or_answer_field_is_not_complete',
            'test_quotes_json_false_source_and_complete_do_not_grant_acceptance',
            'test_free_russian_goal_needs_explicit_interpretation_acceptance',
            'test_stale_goal_config_spec_and_confirmation_are_rejected',
            'test_foreign_slots_unsupported_path_unit_and_self_comparison_fail',
            'test_collection_cannot_claim_all_pages_from_full_body',
            'test_calculation_expectation_cannot_be_invented_by_generator',
            'test_create_read_identity_requires_dynamic_request_target',
            'test_unmapped_extra_mutation_and_unsupported_old_epoch_are_rejected',
            'test_full_response_missing_expected_field_is_fail_not_unknown')]},
    'S09b2': {
        'good': [H + 'test_phase_budgets_are_separate_and_approval_does_not_consume_ask',
            H + 'test_wait_time_is_recorded_separately_from_active_clock',
            H + 'test_private_input_is_only_in_vault_and_public_events_are_masked',
            AUTH + 'test_registration_logout_old_token_401_and_relogin_use_real_http',
            A + 'test_typed_input_is_asked_before_mutation_and_cannot_change_after_http'],
        'bad': [H + 'test_timeout_cancel_and_foreign_response_never_mean_consent',
            A + 'test_expected_input_must_be_fixed_before_http_and_cannot_be_replaced_after',
            AUTH + 'test_occupied_login_blocks_dependent_mutations_without_blind_retry',
            A + 'test_secret_literal_in_metadata_never_reaches_generator',
            A + 'test_change_after_http_uses_new_message_run_and_keeps_old_manifest']},
    'S09b3': {
        'good': [A + 'test_real_goal_compiles_runs_checks_answers_and_deduplicates_after_restart',
            A + 'test_compiled_create_read_keeps_s09a_fact_validators',
            A + 'test_bounded_metadata_discovery_precedes_single_generator_without_api_http',
            'test_goal_cli.GoalCliTests.test_generated_goal_cli_and_duplicate_use_same_saved_run'],
        'bad': [A + 'test_rejected_generator_never_repeats_on_duplicate_or_performs_http',
            A + 'test_durable_preparation_can_be_claimed_only_once_across_connections',
            A + 'test_natural_goal_requires_confirmation_before_http_and_false_json_never_auto_accepts',
            A + 'test_missing_calculation_expectation_is_asked_without_generator_replay',
            A + 'test_generator_prefix_stable_across_goals_and_budget_preflight_stops_before_model',
            A + 'test_primary_cannot_change_conditions_or_perform_extra_http_via_check_goal']},
}


def live(directory):
    """Настоящие LLM+HTTP+SQLite; semantic acceptance здесь controlled fixture."""
    from api_agent.agent.model import LocalModel
    from api_agent.goals.application import GoalApplication
    from api_agent.goals.interaction import HumanInteraction
    from api_agent.openapi import load_source
    from api_agent.policy import ProjectConfig
    from api_agent.storage import SQLiteHistory
    from demo_api.server import DemoServer
    lock = json.loads((ROOT / 'experiments/local_model/runtime-lock.json').read_text(encoding='utf-8'))
    with (ROOT / 'models/MiniCPM5-2B-Q4_K_M.gguf').open('rb') as weights:
        model_hash = hashlib.file_digest(weights, 'sha256').hexdigest()
    assert model_hash == lock['model']['sha256']
    model = LocalModel('http://127.0.0.1:18081')
    catalog = load_source(str(ROOT / 'tests/fixtures/demo_openapi.json'))
    goal = 'Покажи имя питомца и проверь доступность сведений.'
    with DemoServer() as server:
        config = ProjectConfig.create('p', server.target, catalog, mode='READ_ONLY', resettable=True, operations={'getPet': 'A'})
        with SQLiteHistory(directory / 'live.sqlite3') as history:
            human = HumanInteraction(lambda q: {'request_id': q['id'], 'value': 'yes'})
            run = GoalApplication(server.target, catalog, config, history=history, generator=model, human=human).start(
                'p', 's', 'live', goal, model)
            (directory / 'live-run.json').write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding='utf-8')
            (directory / 'live-journal.json').write_text(json.dumps(server.journal, indent=2), encoding='utf-8')
            assert run['status'] == 'completed' and run['report']['goal_verdict'] == 'PASS'
            assert run['preparation']['generator_calls'] == 1 and len(server.journal) == 1
            answers = run['report']['goal_checklist']['answer_fields']
            assert any(v == 'Murka' or isinstance(v, dict) and v.get('name') == 'Murka' for v in answers.values())
            assert run['resources']['usage_status'] == 'measured'
        unexpected = []
        def denied(_):
            unexpected.append(True)
            raise RuntimeError('replay forbidden')
        with SQLiteHistory(directory / 'live.sqlite3') as history:
            restored = GoalApplication(server.target, catalog, config, history=history, generator=denied).start(
                'p', 's', 'live', goal, denied)
            assert restored['run_id'] == run['run_id'] and not unexpected and len(server.journal) == 1
        return {'kind': 'real local LLM + HTTP + SQLite, controlled semantic acceptance', 'model_sha256': model_hash,
            'generator_calls': 1, 'primary_calls': run['model_calls'], 'http': 1,
            'active_seconds': run['wall_seconds'], 'restart_new_http_llm': 0}


def main(stage, mode):
    directory = Path(os.environ.get('S09_EVIDENCE_DIR', ROOT / 'evidence/S09')) / (stage + '-' + mode + '-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    record = {'stage': stage, 'mode': mode, 'exit_code': 1, 'independent_accept': False}
    with (directory / 'checks.log').open('w', encoding='utf-8') as stream, redirect_stdout(stream), redirect_stderr(stream):
        try:
            stages = list(CASES) if stage == 'S09b' else [stage]
            names = list(dict.fromkeys(name for part in stages for name in CASES[part][mode]))
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
            record.update(tests_run=result.testsRun, skips=len(result.skipped))
            if result.wasSuccessful() and result.testsRun == len(names) and not result.skipped:
                if mode == 'good' and stage in {'S09b', 'S09b3'}: record['live'] = live(directory)
                record['exit_code'] = 0 if mode == 'good' else 2
        except Exception as error:
            record['error_type'] = type(error).__name__
    record['head'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    record['branch'] = subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip()
    files = sorted(set((ROOT / 'src').rglob('*.py')) | set((ROOT / 'src').rglob('*.sql')) |
        set((ROOT / 'scripts').glob('*.py')) | set((ROOT / 'tests').rglob('*.py')) |
        set((ROOT / 'tests/fixtures').glob('*.json')) | set((ROOT / 'demo_api').glob('*.py')))
    record['source_hashes'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    (directory / 'tracked.patch').write_bytes(subprocess.check_output(['git', 'diff', '--binary'], cwd=ROOT))
    (directory / 'verification.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'stage': stage, 'mode': mode, 'exit_code': record['exit_code'], 'evidence': str(directory)}))
    return record['exit_code']


if __name__ == '__main__': raise SystemExit(main(sys.argv[1], sys.argv[2]))

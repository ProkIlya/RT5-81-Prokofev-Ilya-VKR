"""S08R: реальные отрицательные контроли, live LLM/HTTP отдельно от doubles."""
from contextlib import redirect_stdout, redirect_stderr
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

CASES = {
    'a': {
        'good': ['test_snapshot_redaction.SnapshotRedactionTests.test_auth_sqlite_reviewer_restart_preserves_snapshot_without_replay',
                 'test_s08r_review_regressions.RedactionOriginTests.test_narrow_trusted_inspect_keeps_token_schema'],
        'bad': ['test_snapshot_redaction.SnapshotRedactionTests.test_auth_sqlite_reviewer_restart_preserves_snapshot_without_replay',
                'test_s08r_review_regressions.RedactionOriginTests.test_http_metadata_lookalikes_never_expose_secret_values',
                'test_agent_contract_history.ContractHistoryTests.test_secret_enum_blocks_before_model_and_http',
                'test_agent_contract_history.ContractHistoryTests.test_hash_and_pointer_identity_cannot_be_masked',
                'test_catalog_public_schema.PublicSchemaTests.test_http_lookalike_catalog_envelope_does_not_disable_masking']},
    'b': {
        'good': ['test_request_principal.RequestPrincipalTests.test_explicit_authenticated_request_without_schema_security_uses_bearer'],
        'bad': ['test_request_principal.RequestPrincipalTests.test_stale_foreign_and_anonymous_autoauth_rejected_before_network',
                'test_request_principal.RequestPrincipalTests.test_auth_mapping_cannot_change_after_run_acceptance',
                'test_request_principal.RequestPrincipalTests.test_auth_revocation_during_approval_prevents_prepared_http',
                'test_catalog_anonymous.AnonymousTests.test_anonymous_negative_really_sends_profile_without_login']},
    'c': {
        'good': ['test_action_occurrences.OccurrenceTests.test_create_two_objects_then_read_first_and_keep_all_evidence',
                 'test_action_occurrences.OccurrenceTests.test_logout_relogin_invalidates_old_epoch_and_completes_new_occurrences'],
        'bad': ['test_action_occurrences.OccurrenceTests.test_same_semantic_post_in_different_slot_is_still_rejected',
                'test_action_occurrences.OccurrenceTests.test_partial_body_phase_never_forces_unoffered_http_tool']},
    'd': {
        'good': ['test_runtime_admission.RuntimeAdmissionTests.test_true_witness_permits_request_and_collection_needs_explicit_completeness'],
        'bad': ['test_runtime_admission.RuntimeAdmissionTests.test_target_is_bound_to_actual_source_not_guessed_numeric_id',
                'test_s08r_review_regressions.WitnessIdentityTests.test_wrong_response_entity_never_authorizes_dependent_mutation',
                'test_runtime_admission.RuntimeAdmissionTests.test_collection_composition_and_completeness_gate_real_mutation',
                'test_runtime_admission.RuntimeAdmissionTests.test_false_unknown_or_incomplete_witness_never_sends_dependent_request',
                'test_runtime_admission.RuntimeAdmissionTests.test_missing_source_does_not_turn_fixture_verified_into_runtime_proof']},
    'e': {
        'good': ['test_run_assessment.RunAssessmentTests.test_read_only_restriction_explains_missing_mutation',
                 'test_s08r_review_regressions.UncertainContinuationTests.test_known_completed_effect_can_be_explicitly_retested',
                 'test_s08r_review_regressions.UncertainContinuationTests.test_uncertain_project_allows_reads_and_other_projects',
                 'test_run_assessment.RunAssessmentTests.test_catalog_read_only_returns_canonical_block_before_model_or_http',
                 'test_run_assessment.RunAssessmentTests.test_known_occurrence_budget_shortfall_stops_before_mutation'],
        'bad': ['test_run_assessment.RunAssessmentTests.test_bad_then_good_preserves_contract_violations_without_double_count',
                'test_s08r_review_regressions.UncertainContinuationTests.test_restart_new_message_cannot_repeat_uncertain_post',
                'test_s08r_review_regressions.UncertainContinuationTests.test_same_process_new_message_also_preserves_uncertain_lock',
                'test_s08r_review_regressions.UncertainContinuationTests.test_crash_after_durable_prepare_blocks_new_mutation',
                'test_run_assessment.RunAssessmentTests.test_early_intent_fail_survives_later_pass_and_incomplete_chain',
                'test_run_assessment.RunAssessmentTests.test_recovery_keeps_uncertain_primary_cause_before_finalization',
                'test_run_assessment.RunAssessmentTests.test_timeout_after_commit_remains_primary_after_reviewer_failure_and_restart']},
}


def live_auth_create_read(directory):
    """Настоящая модель, 4 HTTP, reviewer, затем SQLite restart без replay."""
    from api_agent.agent import StartAgentRun
    from api_agent.agent.catalog import CatalogWorkflow
    from api_agent.agent.model import LocalModel
    from api_agent.agent.summary_review import FinalSummaryReviewer
    from api_agent.openapi import parse_document
    from api_agent.policy import ProjectConfig
    from api_agent.storage import SQLiteHistory
    from demo_api.server import DemoServer
    from test_catalog_execution import status_condition

    auth = json.loads((ROOT / 'tests/fixtures/auth_openapi.json').read_text(encoding='utf-8'))
    orders = json.loads((ROOT / 'tests/fixtures/chain_openapi.json').read_text(encoding='utf-8'))
    for item in orders['paths'].values():
        for operation in item.values():
            operation['security'] = [{'bearerAuth': []}]
    auth['paths'].update(orders['paths'])
    catalog = parse_document(auth)
    database = directory / 'live.sqlite3'
    goal = 'Log in using application credentials. Create order named A, read that created order using its issued binding reference, then read the authenticated profile. Finish with all evidence IDs.'
    model = LocalModel('http://127.0.0.1:18081')
    with (ROOT / 'models/MiniCPM5-2B-Q4_K_M.gguf').open('rb') as weights:
        model_hash = hashlib.file_digest(weights, 'sha256').hexdigest()
    lock = json.loads((ROOT / 'experiments/local_model/runtime-lock.json').read_text(encoding='utf-8'))
    if model_hash != lock['model']['sha256']:
        raise ValueError('model_hash_mismatch')
    checkpoints = []
    with DemoServer() as server:
        config = ProjectConfig.create('p', server.target, catalog, mode='TEST', resettable=True,
            operations={'loginUser': 'C', 'createOrder': 'C', 'readOrder': 'A', 'readProfile': 'A'})
        wf = CatalogWorkflow(catalog, config, goal=goal,
            conditions={name: [status_condition(code)] for name, code in
                [('loginUser', 200), ('createOrder', 201), ('readOrder', 200), ('readProfile', 200)]},
            extraction={'loginUser': {'/token': {'type': 'string', 'secret': True}},
                        'createOrder': {'/id': {'type': 'integer', 'secret': False}}},
            auth={'login_operation': 'loginUser', 'token_path': '/token',
                  'body': {'login': 'fixture', 'password': 'fixture-password'}},
            binding_rules={'readOrder': {'order_id': {'source_operation': 'createOrder',
                'source_occurrence': 'created-a', 'source_path': '/id', 'response_path': '/id'}}},
            admission_rules={'readOrder': {'target': {'parameter': 'order_id',
                'source_operation': 'createOrder', 'source_occurrence': 'created-a', 'source_path': '/id'}}},
            occurrences=[{'id': 'login', 'operation_id': 'loginUser'},
                {'id': 'created-a', 'operation_id': 'createOrder', 'body': {'name': 'A'}},
                {'id': 'read-a', 'operation_id': 'readOrder'},
                {'id': 'profile', 'operation_id': 'readProfile'}])
        with SQLiteHistory(database) as history:
            run = StartAgentRun(server.target, catalog, workflow=wf, history_store=history,
                trace_sink=checkpoints.append, summary_reviewer=FinalSummaryReviewer(model)).start(
                    'p', 's', 'live-message', goal, model)
        (directory / 'live-run.json').write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding='utf-8')
        (directory / 'live-journal.json').write_text(json.dumps(server.journal, indent=2), encoding='utf-8')
        if run['status'] != 'completed' or run['requests'] != 4 or run.get('review_model_calls') != 1:
            raise ValueError('live_scenario_not_completed')
        if run['report']['checks_verdict'] != 'PASS' or not server.journal[-1]['auth_matched']:
            raise ValueError('live_scenario_not_verified')
        if len(server.orders) != 1 or run['exchanges'][2]['response']['json']['name'] != 'A':
            raise ValueError('live_created_entity_mismatch')
        for checkpoint in [run, *checkpoints]:
            if checkpoint['contract'] != catalog.snapshot or checkpoint['spec_hash'] != catalog.spec_hash:
                raise ValueError('live_snapshot_changed')
        for secret in (server.auth_token, 'fixture-password'):
            if secret in json.dumps([run, checkpoints]) or secret.encode() in database.read_bytes():
                raise ValueError('live_secret_leak')
        before = server.connections
        with SQLiteHistory(database) as history:
            def forbidden(_):
                raise AssertionError('unexpected_model_replay')
            restored = StartAgentRun(server.target, catalog, workflow=wf, history_store=history).start(
                'p', 's', 'live-message', goal, forbidden)
            if restored != run or history.run_for_message('p', 's', run['response_message_id']) != run:
                raise ValueError('live_history_changed')
        if server.connections != before:
            raise ValueError('live_http_replay')
    return {'kind': 'real local LLM + Demo HTTP + SQLite + reviewer', 'requests': 4,
            'model_calls': run['model_calls'], 'run_id': run['run_id'],
            'model_sha256': model_hash, 'spec_hash': catalog.spec_hash, 'restart_no_replay': True}


def regressions(directory, mode):
    """Parent включает регрессии прошлых срезов, не только новые сценарии."""
    commands = []
    if mode == 'good':
        commands.append(('unit', [sys.executable, '-X', 'utf8', '-m', 'unittest', 'discover', '-s', 'tests', '-v'], 0))
    commands.extend((stage, [sys.executable, '-X', 'utf8', 'scripts/check_stage.py', stage, mode],
                     0 if mode == 'good' else 2) for stage in ('S02', 'S03', 'S04', 'S05', 'S06', 'S07', 'S08'))
    records = []
    env = {**os.environ, 'CATALOG_LIVE_TOKENIZER': '1'}
    for stage in ('S02', 'S03', 'S04', 'S05', 'S06', 'S07', 'S08'):
        env[stage + '_EVIDENCE_DIR'] = str((directory / 'legacy' / stage).resolve())
    for label, command, expected in commands:
        with (directory / (label + '.stdout.log')).open('w', encoding='utf-8') as stdout, \
             (directory / (label + '.stderr.log')).open('w', encoding='utf-8') as stderr:
            actual = subprocess.run(command, cwd=ROOT, env=env, stdout=stdout, stderr=stderr, timeout=600).returncode
        records.append({'stage': label, 'command': command, 'expected_exit': expected, 'exit_code': actual})
        (directory / 'regression-progress.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
        if actual != expected:
            return records, False
    return records, True


def main(stage, mode):
    directory = Path(os.environ.get('S08R_EVIDENCE_DIR', ROOT / 'evidence/S08R')) / (stage + '-' + mode + '-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    parts = 'abcde' if stage == 'S08R' else stage[-1]
    record = {'stage': stage, 'mode': mode, 'exit_code': 1,
        'kind': 'controlled decisions + real HTTP/SQLite; live LLM separately identified'}
    with (directory / 'checks.log').open('w', encoding='utf-8') as stream, redirect_stdout(stream), redirect_stderr(stream):
        try:
            names = [name for part in parts for name in CASES[part][mode]]
            suite = unittest.defaultTestLoader.loadTestsFromNames(names)
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
            record['tests_run'] = result.testsRun
            if result.wasSuccessful() and result.testsRun == len(names) and not result.skipped:
                if stage == 'S08R':
                    record['regressions'], passed = regressions(directory, mode)
                    if not passed:
                        raise ValueError('regression_failed')
                if mode == 'good' and 'a' in parts:
                    record['live'] = live_auth_create_read(directory)
                record['exit_code'] = 0 if mode == 'good' else 2
        except Exception as error:
            record['error_type'] = type(error).__name__
            # Текст исключения или HTTP/LLM body не является safe diagnostic.
    record['head'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    record['branch'] = subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip()
    sources = sorted(set((ROOT / 'src').rglob('*.py')) | set((ROOT / 'src').rglob('*.sql')) |
                     set((ROOT / 'scripts').glob('*.py')) | set((ROOT / 'tests').rglob('*.py')) |
                     set((ROOT / 'tests/fixtures').glob('*.json')))
    record['source_hashes'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    (directory / 'tracked.patch').write_bytes(subprocess.check_output(['git', 'diff', '--binary'], cwd=ROOT))
    (directory / 'verification.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'stage': stage, 'mode': mode, 'exit_code': record['exit_code'], 'evidence': str(directory)}))
    return record['exit_code']

"""S09d3 technical parity: Docker+HTTP, good0/bad2/error1; real LLM отдельно."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT)]
from scripts.check_workspace_sandbox import preflight
from scripts.evaluate_condition_trip_series import frozen_changes

AUTH='test_condition_auth_scope.ConditionAuthScopeTests.'
IDENTITY='experiments.condition_identity_application_probe.IdentityApplicationTests.'
TEMPORAL='experiments.condition_temporal_application_probe.TemporalApplicationTests.'
HTTP='experiments.condition_http_boundary_probe.HttpBoundaryTests.'
LATCH='experiments.condition_kernel_latches_probe.KernelLatchTests.'
STATE='test_goal_state_completion.GoalStateRuntimeTests.'
VISIBILITY='test_condition_visibility.ConditionVisibilityRuntimeTests.'
CASES={
    'good':[IDENTITY+'test_code',IDENTITY+'test_catalog',TEMPORAL+'test_code',TEMPORAL+'test_catalog',
        AUTH+'test_catalog_logout_revoked_credential_and_relogin_actual_http',
        AUTH+'test_code_logout_revoked_credential_and_relogin_actual_http',
        STATE+'test_catalog_recovers_and_finishes_without_another_model_call',
        STATE+'test_code_recovers_and_finishes_without_another_model_call',
        HTTP+'test_explicit_negative_status_returns_response_and_allows_next_operation',
        'test_workspace_pending_checks.PendingCheckContextTests.test_v2_context_binds_each_check_to_its_feedback_id',
        'test_workspace_pending_checks.PendingCheckRuntimeTests.test_automatic_feedback_keeps_pending_verdict_and_expected_value',
        'test_workspace_initial_check_ids.InitialCheckIdsTests.test_first_payload_keeps_expectations_and_feedback_binds_the_same_checks',
        'test_workspace_response_preflight.ResponsePreflightTests.test_reassignment_or_other_scope_is_not_a_false_sdk_result',
        'test_workspace_response_preflight.ResponsePreflightRuntimeTests.test_invalid_response_style_prevents_first_write_and_corrected_block_runs_once'],
    'bad':[LATCH+'test_uncertain_write_stays_latched_across_blocks',
        LATCH+'test_request_budget_cannot_be_restored_by_python_catch',
        LATCH+'test_clearing_cancel_flag_does_not_restore_latched_authority',
        HTTP+'test_unexpected_status_stops_dependent_writes_and_keeps_evidence',
        HTTP+'test_caught_error_cannot_resume_http_in_same_block',
        AUTH+'test_unsafe_or_ambiguous_scope_is_rejected_before_http',
        AUTH+'test_auth_mapping_is_required_for_scoped_checks',
        AUTH+'test_revoked_only_read_before_logout_has_no_http',
        VISIBILITY+'test_catalog_keeps_hidden_checker_after_feedback',
        VISIBILITY+'test_code_keeps_hidden_checker_after_feedback',
        TEMPORAL+'test_code_final_state_does_not_prove_before',
        TEMPORAL+'test_catalog_final_state_does_not_prove_before',
        'experiments.condition_private_scope_probe.PrivateScopeTests.test_nested_private_fields_cannot_be_accepted_even_with_confirmation',
        'test_workspace_response_preflight.ResponsePreflightTests.test_known_sdk_result_requires_mapping_access']}


def source_hashes():
    paths=set()
    for root in ('src','scripts','tests','demo_api'):
        paths.update((ROOT/root).rglob('*.py'))
        paths.update((ROOT/root).rglob('*.json'))
        paths.update((ROOT/root).rglob('*.sql'))
    paths.update((ROOT/'experiments').glob('*.py'))
    paths.update((ROOT/'docker/workspace').glob('*'))
    return {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(paths) if p.is_file()}


def main(mode, *, stage='S09d3'):
    if mode not in CASES or stage not in {'S09d','S09d3'}:return 1
    directory=Path(os.environ.get('S09_EVIDENCE_DIR',ROOT/'evidence/S09'))/(stage+'-'+mode+'-'+uuid.uuid4().hex)
    directory.mkdir(parents=True,exist_ok=False)
    record={'stage':stage,'mode':mode,'exit_code':1,'independent_accept':False,
        'scope':'technical conditions-v2 parity, fake primary; actual Docker+HTTP+SQLite',
        'real_primary_pilot':'separate evidence required; not run by this gate',
        'generator_stability':'not assessed; experimental/deferred D72',
        'parent_acceptance_complete':False}
    with (directory/'checks.log').open('w',encoding='utf-8') as stream,redirect_stdout(stream),redirect_stderr(stream):
        try:
            before=source_hashes();record['source_hashes_before']=before
            if frozen_changes():raise ValueError('frozen_changed')
            record['sandbox']=preflight();os.environ['WORKSPACE_DOCKER_TESTS']='1'
            names=CASES[mode]
            result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
            record.update(tests_run=result.testsRun,expected_tests=len(names),skips=len(result.skipped),
                failures=len(result.failures),errors=len(result.errors))
            after=source_hashes()
            record['source_changes']=sorted(k for k in before.keys()|after.keys() if before.get(k)!=after.get(k))
            record['frozen_changes']=frozen_changes()
            if (result.wasSuccessful() and result.testsRun==len(names) and not result.skipped
                and not record['source_changes'] and not record['frozen_changes']):
                record['exit_code']=0 if mode=='good' else 2
        except Exception as error:record['error_type']=type(error).__name__
    record['head']=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip()
    record['branch']=subprocess.check_output(['git','branch','--show-current'],cwd=ROOT,text=True).strip()
    (directory/'verification.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    (directory/'tracked.patch').write_bytes(subprocess.check_output(['git','diff','--binary','HEAD'],cwd=ROOT))
    print(json.dumps({'stage':stage,'mode':mode,'exit_code':record['exit_code'],'evidence':str(directory)}))
    return record['exit_code']


if __name__=='__main__':raise SystemExit(main(sys.argv[1]))

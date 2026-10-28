"""Gates S11: отдельные проверяемые slices и отрицательные мутации."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
MODULES = {'S11a': ['test_reuse_versions', 'test_selected_history', 'test_openapi_profile']}
MODULES['S11b'] = ['test_reuse_observations', 'test_reuse_versions', 'test_selected_history']
MODULES['S11c'] = ['test_continuation', 'test_continuation_guards', 'test_continuation_cli',
    'test_checkpoint_failures', 'test_recovery_runtime', 'test_health_preflight', 'test_auth_recovery_feedback',
    'test_acknowledged_write']
MODULES['S11c'].append('test_live_recovery_observer')
MODULES['S11c'] += ['test_recovery_binding', 'test_recovery_review_guards']
MODULES['S11c'].append('test_recovery_auth_versions')
MODULES['S11'] = list(dict.fromkeys(sum(MODULES.values(), []))) + ['test_code_broker', 'test_runtime_cleanup', 'test_module_size']


def source_identity():
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((ROOT / 'src').rglob('*')) if p.is_file() and p.suffix in {'.py', '.sql'}}


def run_suite(names, stream):
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromNames(names))
    return {'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
            'skips': len(result.skipped), 'passing': result.wasSuccessful() and not result.skipped}


def main(stage, kind):
    if stage not in MODULES or kind not in {'good', 'bad'}: return 3
    directory = ROOT / 'evidence/S11/implementation-20261027' / (stage + '-' + kind + '-' + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    record = {'stage': stage, 'kind': kind, 'source_hashes': source_identity()}
    started = time.monotonic()
    code = 1
    try:
        if stage in {'S11c', 'S11'} and (os.environ.get('WORKSPACE_DOCKER_TESTS') != '1' or not os.environ.get('WORKSPACE_IMAGE')):
            raise ValueError('docker_test_configuration_required')
        with (directory / 'tests.log').open('x', encoding='utf-8') as stream, redirect_stdout(stream), redirect_stderr(stream):
            record['baseline'] = run_suite(MODULES[stage], stream)
            if not record['baseline']['passing']: raise ValueError('baseline_failed')
            if kind == 'good':
                code = 0
            else:
                mutations = []
                specs_a = [('api_agent.goals.entry.manual_application.ManualApplication.attachment_status',
                           {'status': 'applicable', 'reasons': []}, 'test_reuse_versions.ReuseVersionTests.' + name)
                          for name in ('test_changed_dependency_blocks_preview_before_any_http',
                                       'test_security_and_profile_changes_are_stale_and_report_is_immutable')]
                specs_b = [
                             ('api_agent.storage.reuse.observe_attachments', None,
                              'test_reuse_observations.ReuseObservationTests.test_normal_read_records_current_values_and_keeps_old_report'),
                             ('api_agent.storage.fact_history.FactHistory._fact_policy',
                              {'reset_epoch': 0, 'response_observation': 3600, 'create_read_link': 3600},
                              'test_reuse_observations.ReuseObservationTests.test_known_reset_blocks_preview_without_a_probe')]
                specs_c = [('api_agent.goals.entry.continuation.check_service', {'ready': True},
                            'test_continuation.ContinuationTests.test_health_failure_does_not_create_run_or_invoke_worker_or_model'),
                           ('api_agent.storage.history.SQLiteHistory.unresolved_mutations', [],
                            'test_recovery_runtime.RecoveryRuntimeTests.test_uncertain_continuation_blocks_other_mutations_too'),
                           ('api_agent.storage.checkpoints.retained_scalars', {},
                            'test_checkpoint_failures.CheckpointFailureTests.test_public_id_outside_preview_is_recovered_with_its_pointer'),
                           ('api_agent.workspace.execution.recovered_sources.restore_sources', {},
                            'test_recovery_binding.RecoveryBindingTests.test_worker_uses_verified_source_without_repeating_create'),
                           ('api_agent.execution.auth_feedback.recovery_login_allowed', False,
                            'test_recovery_review_guards.RecoveryAuthTests.test_fresh_login_allows_protected_read_but_never_business_write'),
                           ('api_agent.storage.checkpoints.reserved_active_seconds', 0,
                            'test_recovery_review_guards.RecoveryTimeTests.test_durable_http_intent_carries_time_bound_before_response'),
                           ('api_agent.execution.auth_feedback.origin_allows_recovery_login', True,
                            'test_recovery_auth_versions.AuthVersionTests.test_unknown_login_is_not_replayed_after_operation_id_rename')]
                specs = {'S11a': specs_a, 'S11b': specs_b, 'S11c': specs_c, 'S11': specs_a + specs_b + specs_c}[stage]
                for target, value, name in specs:
                    with patch(target, return_value=value):
                        outcome = run_suite([name], stream)
                        mutations.append({'name': name, 'detected': outcome['failures'] > 0 and not outcome['errors']})
                record['mutations'] = mutations
                code = 2 if all(item['detected'] for item in mutations) else 1
    except Exception as error:
        record['error'] = str(error) if isinstance(error, ValueError) else type(error).__name__
        if isinstance(error, ValueError) and str(error) == 'docker_test_configuration_required': code = 3
    if source_identity() != record['source_hashes']:
        code = 1; record['source_changed'] = True
    record.update(exit_code=code, seconds=time.monotonic() - started)
    (directory / 'result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in record.items() if k != 'source_hashes'}, ensure_ascii=False))
    return code


if __name__ == '__main__': raise SystemExit(main(*sys.argv[1:]))

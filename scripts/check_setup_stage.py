"""Исполняемая квалификация S14a: positive baseline и независимые мутации."""
from contextlib import redirect_stderr, redirect_stdout
from decimal import Decimal, ROUND_HALF_EVEN
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import time
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / folder) for folder in ('src', 'tests', 'scripts')]
MODULES = ['test_setup_doctor', 'test_setup_continuation', 'test_setup_credentials',
    'test_setup_environment', 'test_setup_installation', 'test_setup_ui', 'test_desktop_models',
    'test_desktop_store', 'test_desktop_model_contract', 'test_continuation_guards',
    'test_continuation_cli', 'test_checkpoint_failures', 'test_health_preflight',
    'test_diagnostic_messages', 'test_module_size']
B_MODULES = ['test_stand_v2_owner', 'test_stand_v2_cases', 'test_stand_v2_observers',
             'test_stand_v2_fixtures', 'test_stand_v2_series', 'test_stand_v2_generic', 'test_stand_v2_history',
             'test_stand_v2_qualification']


def identities():
    paths = [p for folder in ('src', 'tests', 'scripts', 'docker/setup')
             for p in (ROOT / folder).rglob('*')
             if p.is_file() and ('__pycache__' not in p.parts) and p.suffix in {'.py', '.sql', '.json', '.tcss'}]
    paths += [ROOT / p for p in ('docker/setup/Dockerfile', 'requirements.txt', 'requirements-lock.txt', 'main.py')]
    return {p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(set(paths))}


def run(names, stream):
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
    return {'tests': result.testsRun, 'failures': len(result.failures), 'errors': len(result.errors),
            'skips': len(result.skipped), 'passing': result.wasSuccessful() and not result.skipped}


def live_evidence(stage):
    if stage == 'S14a': return {}
    from stand_v2.series import source_identity
    from stand_v2.client import Owner
    result = {}
    for key in ('S14_READINESS_EVIDENCE', 'S14_DIAGNOSTIC_EVIDENCE'):
        path = Path(os.environ.get(key, ''))
        if not path.is_dir(): raise ValueError('missing_' + key)
        summary = json.loads((path / 'summary.json').read_text(encoding='utf-8'))
        manifest = json.loads((path / 'source-manifest.json').read_text(encoding='utf-8'))
        current = source_identity()
        if manifest != current:
            from stand_v2.qualification import qualify_setup_evidence
            result[key + '_setup_delta'] = qualify_setup_evidence(path, current, ROOT)
        freeze = json.loads((path / 'freeze.json').read_text(encoding='utf-8'))
        if freeze['stand_images'] != Owner().images() or freeze['worker_image'] != os.environ.get('WORKSPACE_IMAGE'):
            raise ValueError('live_evidence_images_changed')
        if key == 'S14_READINESS_EVIDENCE':
            if summary.get('case_count') != 40 or not summary.get('ready'): raise ValueError('readiness_incomplete')
        elif (not summary.get('complete') or summary.get('recorded') != 120
              or summary.get('agent_started') != 117 or summary.get('agent_unstarted_errors')
              or summary.get('preparation_errors') or summary.get('evaluator_errors')):
            raise ValueError('diagnostic_incomplete')
        result[key] = str(path)
    return result


def controls(stage='S14a'):
    from api_agent.setup import doctor, environment, credentials
    from api_agent.agent.model import LocalModel
    from api_agent.desktop.views.project_setup import ProjectSetupDialog
    original = doctor.inspect_project
    def false_save(values, **kwargs):
        result = original(values, **kwargs); result['can_save'] = True; return result
    a = [
        (doctor, 'inspect_project', false_save,
         'test_setup_doctor.DoctorTests.test_unsupported_contract_is_blocked_and_does_not_echo_secret_literals'),
        (doctor, 'probe_candidates', lambda catalog, policy: ['listBooks'],
         'test_setup_doctor.DoctorTests.test_probe_cannot_call_protected_operation_or_bypass_policy'),
        (doctor, 'project_fingerprint', lambda values: 'unchanged',
         'test_setup_doctor.DoctorTests.test_source_bytes_change_invalidates_previous_check_even_with_same_execution_hash'),
        (environment, 'inspect_storage', lambda path: {'name': 'storage', 'state': 'ready'},
         'test_setup_environment.EnvironmentTests.test_future_schema_is_rejected_without_modifying_original_database'),
        (credentials, '_crypt', lambda raw, scope, **kwargs: raw,
         'test_setup_credentials.CredentialsTests.test_saved_credentials_are_encrypted_and_bound_to_project_scope'),
        (credentials, 'compile_connection', lambda *args: {'auth': {}, 'extraction': {}},
         'test_setup_credentials.CredentialsTests.test_missing_body_and_non_string_token_path_are_rejected'),
        (LocalModel, 'count_tokens', lambda self, payload: 0,
         'test_setup_environment.EnvironmentTests.test_model_payload_and_output_reserve_must_fit_the_window')]
    original_connection = credentials.project_connection
    def stale_connection(values, previous, connection, **kwargs):
        result = original_connection(values, previous, connection, **kwargs)
        if previous.get('connection_path') and connection is None:
            result['connection_path'] = previous['connection_path']
        return result
    a.append((credentials, 'project_connection', stale_connection,
        'test_setup_ui.ConnectionSourceTests.test_import_replaces_old_encrypted_account_in_actual_application'))
    from stand_v2 import observers, series, cases, client, session as stand_session
    from api_agent.goals.checks import selected_compile
    original_case = cases.build_case
    def leak_oracle(number, fixture):
        item = original_case(number, fixture)
        if number in {38, 39, 40}: item['document']['goal'] += '\nHidden expected result ID:8811'
        return item
    original_request = client.Owner._request
    def leak_key(self, *args, **kwargs):
        kwargs['control'] = True; return original_request(self, *args, **kwargs)
    original_broker = stand_session.WorkspaceBroker
    def wrong_history_environment(*args, **kwargs):
        kwargs['environment'] = 'local'
        return original_broker(*args, **kwargs)
    b = [
        (observers, 'unrelated_unchanged', lambda *args: True,
         'test_stand_v2_observers.ObserverTests.test_correct_target_does_not_hide_unrelated_state_damage'),
        (observers, 'answer_contradictions', lambda *args: [],
         'test_stand_v2_observers.ObserverTests.test_explicit_false_id_or_energy_is_rejected_even_when_state_is_correct'),
        (series, 'summarize', lambda rows: {'planned': len(rows), 'agent_planned': len(rows), 'complete': True, 'task_success': 1, 'unexecuted': 0},
         'test_stand_v2_series.SeriesTests.test_incomplete_suite_cannot_be_ready_or_complete'),
        (series, 'require_same_identity', lambda *args: None,
         'test_stand_v2_series.SeriesTests.test_changed_or_missing_identity_stops_before_next_goal'),
        (client.Owner, 'reset', lambda self: self.control('POST', '/reset', {'confirm': 'RESET_TEST_DATA'}),
         'test_stand_v2_owner.OwnerTests.test_reset_failure_is_preparation_failure_not_model_outcome'),
        (client.Owner, '_request', leak_key,
         'test_stand_v2_owner.OwnerTests.test_control_key_never_goes_to_api_and_reset_checks_postconditions'),
        (cases, 'build_case', leak_oracle,
         'test_stand_v2_cases.CaseMappingTests.test_research_mutations_never_receive_hidden_cards_or_observer_expectations'),
        (selected_compile, '_assertion', lambda *args: ('json_field_exists', {'path': '/login'}),
         'test_stand_v2_cases.CaseMappingTests.test_unknown_predicate_is_rejected_by_actual_constructor'),
        (stand_session, 'WorkspaceBroker', wrong_history_environment,
         'test_stand_v2_fixtures.FixtureTests.test_authenticated_history_fixture_produces_applicable_explicit_selection'),
        (observers, 'EXPECTED_REMAINING', cases.EXPECTED_REMAINING_EXACT,
         'test_stand_v2_cases.CaseMappingTests.test_moderation_observer_accepts_public_precision_but_rejects_wrong_result'),
        (cases, 'public_energy', lambda value: Decimal(str(value)).quantize(Decimal('0.01'), rounding=ROUND_HALF_EVEN),
         'test_stand_v2_cases.CaseMappingTests.test_public_energy_rounding_obeys_half_up_including_ties')]
    from stand_v2 import qualification
    b.append((qualification, 'validate_delta', lambda *args: {'execution_sources_unchanged': True},
        'test_stand_v2_qualification.QualificationTests.test_runtime_or_unknown_source_is_rejected'))
    return a if stage == 'S14a' else b if stage == 'S14b' else a + b


def main(stage, kind):
    if stage not in {'S14a', 'S14b', 'S14'} or kind not in {'good', 'bad'}: return 3
    directory = ROOT / 'evidence/S14' / (stage + '-' + kind + '-' + uuid.uuid4().hex[:8]); directory.mkdir(parents=True)
    before = identities(); started = time.monotonic(); record = {'stage': stage, 'kind': kind, 'source_and_tests': before}
    code = 1
    try:
        record['live_evidence'] = live_evidence(stage)
        with (directory / 'tests.log').open('x', encoding='utf-8') as stream, redirect_stdout(stream), redirect_stderr(stream):
            record['baseline'] = run(MODULES if stage == 'S14a' else B_MODULES if stage == 'S14b' else MODULES + B_MODULES, stream)
            if not record['baseline']['passing']: raise ValueError('baseline_failed')
            if kind == 'good': code = 0
            else:
                record['controls'] = []
                for owner, name, replacement, test in controls(stage):
                    consumer = importlib.import_module(test.split('.')[0])
                    try:
                        with patch.object(owner, name, replacement):
                            importlib.reload(consumer)
                            result = run([test], stream)
                    finally: importlib.reload(consumer)
                    record['controls'].append({'test': test, 'detected': result['failures'] > 0 and result['errors'] == 0,
                                               'result': result})
                code = 2 if all(c['detected'] for c in record['controls']) else 1
    except Exception as error: record['error'] = str(error) if isinstance(error, ValueError) else type(error).__name__
    record['source_unchanged'] = identities() == before
    if not record['source_unchanged']: code = 1
    record.update(exit_code=code, seconds=time.monotonic() - started)
    (directory / 'result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in record.items() if k != 'source_and_tests'}, ensure_ascii=False))
    print('evidence=' + str(directory)); return code


if __name__ == '__main__': raise SystemExit(main(*sys.argv[1:]))

"""S09d2: настоящие Docker+HTTP+SQLite, controlled code; good0/bad2/error1."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
B = 'test_workspace_broker.WorkspaceBrokerTests.'
S = 'test_workspace_sandbox.WorkspaceSandboxTests.'
CASES = {
    'good': [B + n for n in ('test_plain_id_is_bound_to_own_exchange_and_local_body_is_not_evidence',
        'test_namespaces_reach_distinct_path_query_and_header_on_wire',
        'test_accepted_occurrences_auth_epochs_and_hidden_bearer_use_same_kernel',
        'test_application_ask_fills_only_accepted_input_and_locks_after_http',
        'test_fact_facade_validates_saved_snapshot_and_publishes_only_after_finalization')] +
        [S + 'test_persistent_namespace_create_two_read_first_and_explicit_finish',
         S + 'test_large_json_remains_in_namespace_with_small_explicit_output'],
    'bad': [B + n for n in ('test_forged_id_stale_artifact_and_protocol_authority_fields_never_connect',
        'test_caught_request_budget_and_cancel_remain_latched', 'test_stale_approval_never_connects',
        'test_rejected_mutation_cannot_reask_but_independent_read_stays_available',
        'test_timeout_after_committed_write_survives_catch_and_new_message',
        'test_same_message_cannot_replay_and_unknown_fact_cannot_be_verified',
        'test_callback_exception_text_is_not_a_protocol_error_or_persisted',
        'test_private_literal_in_sdk_metadata_blocks_before_persistence_or_worker',
        'test_broker_requires_prebuilt_project_sdk_and_rejects_changed_configuration',
        'test_id_from_failed_source_is_not_a_verified_dependency')] + [S + n for n in (
        'test_malformed_done_frame_cannot_escape_protocol_validation',
        'test_caught_uncertain_write_in_python_cannot_retry',
        'test_caught_cancel_in_python_cannot_restore_authority',
        'test_direct_network_host_files_and_runtime_authority_are_absent',
        'test_docker_client_proxy_credentials_do_not_enter_worker',
        'test_monkeypatch_protocol_stdout_and_local_pass_cannot_forge_evidence',
        'test_caught_budget_inside_python_for_never_exceeds_one_http',
        'test_cpu_infinite_loop_is_killed_and_not_resumed', 'test_output_flood_is_bounded_at_pipe_and_stops_worker',
        'test_memory_flood_is_killed_by_environment', 'test_tmpfs_flood_and_fork_limit_do_not_reach_host',
        'test_private_code_output_exception_and_temp_never_reach_durable_artifacts',
        'test_three_corrections_without_progress_and_print_do_not_reset_counter')]}


def preflight():
    from api_agent.workspace.sandbox import DOCKER, DOCKER_HOST
    import re
    image = os.environ.get('WORKSPACE_IMAGE', '')
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image): raise ValueError('pinned_image_required')
    result = subprocess.run([DOCKER, '--host', DOCKER_HOST, 'image', 'inspect', image],
        capture_output=True, timeout=15, check=True)
    info = json.loads(result.stdout)[0]
    if info['Os'] != 'linux' or info['Config'].get('Labels', {}).get('org.api-agent.workspace') != 's09d2-python310-v1':
        raise ValueError('sandbox_image_required')
    return {'image': image, 'os': info['Os'], 'architecture': info['Architecture']}


def sample(directory):
    """Сохраняемый broker/adapter trace отдельно от временных unit fixtures."""
    from api_agent.storage.history import SQLiteHistory
    from api_agent.workspace.sandbox import WorkspaceSandbox
    from demo_api.server import DemoServer
    import test_workspace_broker as fixtures
    with DemoServer() as server, SQLiteHistory(directory / 'controlled.sqlite3') as history:
        with fixtures.WorkspaceBrokerTests().setup_broker(server, history) as broker:
            # Временный runtime не зависит от глубины durable evidence (Windows MAX_PATH).
            with tempfile.TemporaryDirectory(prefix='api-agent-gate-') as temporary:
                with WorkspaceSandbox(broker, image=os.environ['WORKSPACE_IMAGE'], root=Path(temporary) / 'work') as worker:
                    state = worker.inspect()
                    first = worker.execute("a=api.createOrder(body={'name':'A'})\nb=api.createOrder(body={'name':'B'})", call_id='controlled-1')
                    second = worker.execute("r=api.readOrder(path__order_id=a['body']['id'])\nprint(r['body']['name'])\napi.finish('Observed', claims=[], answer_fields={})", call_id='controlled-2')
                    if first['error'] or second['error'] or second['output'].strip() != 'A' or not broker.finished:
                        raise ValueError('controlled_sample_failed')
                if worker.exists(): raise ValueError('sandbox_cleanup_failed')
        if len(server.journal) != 3: raise ValueError('controlled_sample_http_count')
        (directory / 'controlled-run.json').write_text(json.dumps(broker.safe_run(), ensure_ascii=False, indent=2), encoding='utf-8')
        (directory / 'target-journal.json').write_text(json.dumps({'connections': server.connections, 'http': server.journal}, indent=2), encoding='utf-8')
        (directory / 'sandbox-inspect.json').write_text(json.dumps(state, indent=2), encoding='utf-8')
        return {'run_id': broker.run['run_id'], 'http': len(server.journal), 'code_blocks': 2,
            'cleanup_confirmed': True, 'kind': 'controlled code; no LLM calls'}


def main(mode):
    directory = Path(os.environ.get('S09_EVIDENCE_DIR', ROOT / 'evidence/S09')) / ('S09d2-' + mode + '-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    record = {'stage': 'S09d2', 'mode': mode, 'exit_code': 1, 'independent_accept': False,
        'kind': 'controlled code, real networkless Docker worker + HTTP + SQLite; no LLM pilot'}
    with (directory / 'checks.log').open('w', encoding='utf-8') as stream, redirect_stdout(stream), redirect_stderr(stream):
        try:
            record['sandbox'] = preflight()
            # До import тестового модуля: suite не может незаметно пропустить Docker.
            os.environ['WORKSPACE_DOCKER_TESTS'] = '1'
            names = CASES[mode]
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
            record.update(tests_run=result.testsRun, skips=len(result.skipped), failures=len(result.failures), errors=len(result.errors))
            if result.wasSuccessful() and result.testsRun == len(names) and not result.skipped:
                if mode == 'good': record['sample'] = sample(directory)
                record['exit_code'] = 0 if mode == 'good' else 2
        except Exception as error: record['error_type'] = type(error).__name__
    record['head'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    record['branch'] = subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip()
    files = sorted(set((ROOT / 'src').rglob('*.py')) | set((ROOT / 'src').rglob('*.sql')) |
        set((ROOT / 'scripts').glob('*.py')) | set((ROOT / 'tests').rglob('*.py')) |
        set((ROOT / 'tests/fixtures').glob('*.json')) | set((ROOT / 'demo_api').glob('*.py')) |
        set((ROOT / 'docker/workspace').glob('*')))
    record['source_hashes'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
    (directory / 'tracked.patch').write_bytes(subprocess.check_output(['git', 'diff', '--binary'], cwd=ROOT))
    (directory / 'verification.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'stage': 'S09d2', 'mode': mode, 'exit_code': record['exit_code'], 'evidence': str(directory)}))
    return record['exit_code']


if __name__ == '__main__': raise SystemExit(main(sys.argv[1]))

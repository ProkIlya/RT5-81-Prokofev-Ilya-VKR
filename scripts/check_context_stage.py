"""Проверки S13 с сохранением baseline, отрицательных контролей и состава дерева."""
from contextlib import redirect_stderr, redirect_stdout
import json
import os
from pathlib import Path
import time
import uuid
from unittest.mock import patch

from check_desktop_stage import identities, run


ROOT = Path(__file__).resolve().parents[1]
MODULES = {'S13a': ['test_s13_context', 'test_context_window', 'test_code_memory', 'test_manual_context_window',
    'test_model_tokenizer', 'test_runtime_cleanup', 'test_module_size'],
    'S13b': ['test_s13_handoff', 'test_reuse_versions', 'test_reuse_observations',
        'test_recovery_runtime.RecoveryRuntimeTests.test_uncertain_continuation_blocks_other_mutations_too',
        'test_continuation.ContinuationTests.test_recovered_worker_updates_same_created_object_without_replay'],
    'S13c': ['test_s13_modes', 'test_recheck_completion_repair', 'test_unverified_finish', 'test_s13_trip_observer']}
CONTROLS = {'S13a': [
    ('api_agent.workspace.context.context_window._project_results', set(),
     'test_s13_context.BoundedContextTests.test_projects_old_results_before_removing_groups'),
    ('api_agent.workspace.context.run_memory._deduplicate_reads', None,
     'test_code_memory.RunMemoryTests.test_repeated_read_references_latest_equal_projection_in_same_auth_scope'),
    ('api_agent.agent.runtime.TokenBudget.reserve', (7600, 8624),
     'test_s13_context.ContextRuntimeTests.test_oversized_protected_input_stops_before_generation'),
    ('api_agent.desktop.application.app.AgentApp.refresh_context_usage', None,
     'test_s13_context.ContextIndicatorTests.test_uses_final_window_measurement_and_clears_at_terminal_event'),
], 'S13b': [
    ('api_agent.workspace.context.run_memory._field_order', (),
     'test_s13_handoff.HandoffProjectionTests.test_large_observation_preserves_public_id_and_its_source'),
    ('api_agent.agent.runtime.project_recovery', {'observations': []},
     'test_s13_handoff.LinkedHandoffTests.test_resume_after_compaction_keeps_created_id_and_ledger_in_both_modes'),
    ('api_agent.storage.history.SQLiteHistory.unresolved_mutations', [],
     'test_recovery_runtime.RecoveryRuntimeTests.test_uncertain_continuation_blocks_other_mutations_too'),
], 'S13c': [
    ('evaluate_research_trips.answer_ok', True,
     'test_s13_trip_observer.TripObserverTests.test_misleading_or_incomplete_text_cannot_pass'),
    ('api_agent.agent.runtime.unsupported_passes', [],
     'test_unverified_finish.UnverifiedFinishTests.test_fabricated_success_gets_correction_then_real_api'),
]}
MODULES['S13'] = list(dict.fromkeys(sum(MODULES.values(), [])))
CONTROLS['S13'] = sum(CONTROLS.values(), [])


def main(stage, kind):
    if stage not in MODULES or kind not in {'good', 'bad'}:
        return 3
    directory = ROOT / 'evidence/S13' / f'{stage}-{kind}-{uuid.uuid4().hex[:8]}'
    directory.mkdir(parents=True)
    started = time.monotonic()
    before = identities()
    record = {'stage': stage, 'kind': kind, 'source_and_tests': before}
    code = 1
    with (directory / 'tests.log').open('x', encoding='utf-8') as stream, redirect_stdout(stream), redirect_stderr(stream):
        if (os.environ.get('WORKSPACE_DOCKER_TESTS') != '1' or not os.environ.get('WORKSPACE_IMAGE')
                or not os.environ.get('WORKSPACE_TOKENIZER_URL')):
            record['error'] = 'worker_and_tokenizer_configuration_required'
            code = 3
        else:
            record['baseline'] = run(MODULES[stage], stream)
            if record['baseline']['passing']:
                code = 0
                if kind == 'bad':
                    controls = []
                    for target, replacement, test in CONTROLS[stage]:
                        with patch(target, return_value=replacement):
                            outcome = run([test], stream)
                        detected = bool(outcome['failures'] and not outcome['errors'] and not outcome['skips'])
                        controls.append({'test': test, 'target': target, 'detected': detected})
                    record['controls'] = controls
                    code = 2 if all(item['detected'] for item in controls) else 1
    record['source_unchanged'] = identities() == before
    if not record['source_unchanged']:
        code = 1
    record.update(exit_code=code, seconds=time.monotonic() - started)
    (directory / 'result.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({**{key: value for key, value in record.items() if key != 'source_and_tests'},
        'evidence': str(directory)}, ensure_ascii=False))
    return code

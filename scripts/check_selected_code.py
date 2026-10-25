"""Проверки выбранного пути после удаления; native каталог не запускается."""
from contextlib import redirect_stderr,redirect_stdout
import hashlib
import json
from pathlib import Path
import time
import unittest
import os
import sys
import uuid
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/name) for name in ("src","tests","")]

root=Path(os.environ.get("S09_EVIDENCE_DIR", "evidence/S09/selected-code-"+uuid.uuid4().hex));root.mkdir(parents=True,exist_ok=True)
tests=['test_openapi_source','test_openapi_profile','test_goal_compiler','test_validation_oracle',
    'test_workspace_sdk','test_workspace_prompt','test_workspace_reference',
    'test_workspace_response_preflight','test_workspace_state_projection','test_workspace_model_timing',
    'test_workspace_context_window',
    'test_workspace_initial_check_ids','test_workspace_pending_checks',
    'test_context_window','test_run_memory','test_condition_reference_occurrence',
    'test_empty_collection_conditions','test_module_size',
    'test_constructor_expected_numbers.ConstructorExpectedNumbersTests',
    'test_constructor_expected_numbers.ExpectedNumbersApplicationTests.test_code_uses_separate_human_number_with_real_http',
    'test_constructor_expected_numbers.ExpectedNumbersApplicationTests.test_code_uses_absolute_tolerance_with_real_http',
    'test_workspace_broker','test_workspace_event_transaction','test_workspace_state',
    'test_workspace_stdout_origin']
runtime='test_workspace_runtime.WorkspaceRuntimeTests.'
tests += [runtime+n for n in (
    'test_persistent_code_only_loop_uses_same_goal_budget_feedback_and_deduplication',
    'test_final_trace_contains_same_canonical_answer_and_resources_as_history',
    'test_native_finish_cannot_skip_http_or_override_a_failed_condition',
    'test_token_preflight_and_cancel_after_model_prevent_http',
    'test_cli_selects_code_with_prebuilt_sdk_and_repeated_message_does_not_replay')]
started=time.monotonic()
before={p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('src').rglob('*.py')}
with (root/'post-checks.log').open('x',encoding='utf-8') as log,redirect_stdout(log),redirect_stderr(log):
    suite=unittest.defaultTestLoader.loadTestsFromNames(tests)
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
after={p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('src').rglob('*.py')}
data={'requested_tests':tests,'tests_run':result.testsRun,'failures':len(result.failures),
    'errors':len(result.errors),'skips':len(result.skipped),'seconds':time.monotonic()-started,
    'source_unchanged':before==after,'passing':result.wasSuccessful() and before==after and not result.skipped,
    'scope':'Техническая регрессия выбранного code/Core; scripted model, actual Docker/HTTP where required. Не real-LLM устойчивость.',
    'historical_native_tests':'Не изменены. Исполняются только на сохранённом дереве до удаления; каталог не тестировался по указанию владельца.'}
(root/'post-checks.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
print({k:v for k,v in data.items() if k not in {'requested_tests','historical_native_tests'}})
raise SystemExit(0 if data['passing'] else 1)

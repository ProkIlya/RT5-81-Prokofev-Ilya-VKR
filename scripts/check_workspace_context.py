"""S09e2 code-only D77: реальный tokenizer/Docker/HTTP, baseline каталога неизменен."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests'), str(ROOT)]
from scripts.check_condition_integration import source_hashes
from scripts.evaluate_condition_trip_series import frozen_changes
from scripts.check_workspace_sandbox import preflight

WINDOW = 'test_context_window.ContextWindowTests.'
MEMORY = 'test_run_memory.RunMemoryTests.'
CASES = {'good':[
    'test_workspace_context_window.WorkspaceContextWindowTests.test_measured_projection_preserves_two_objects_and_worker_variables',
    MEMORY+'test_two_entities_auth_scope_uncertainty_and_conditions_survive_projection',
    MEMORY+'test_hidden_checks_stay_hidden_while_real_api_values_remain_available',
    WINDOW+'test_below_threshold_is_byte_equivalent_without_memory_construction'],
    'bad':[WINDOW+'test_incomplete_native_group_is_never_silently_repaired',
    WINDOW+'test_unknown_token_count_is_not_replaced_by_a_byte_estimate',
    WINDOW+'test_projection_that_does_not_save_tokens_keeps_original_for_budget_guard',
    MEMORY+'test_large_response_keeps_retrieval_handle_and_explicit_incompleteness',
    MEMORY+'test_workspace_projection_does_not_offer_an_unavailable_response_tool',
    'test_goal_state_completion.GoalStateSelectionTests.test_cancel_and_uncertain_write_prevent_automatic_completion']}


def catalog_changes(baseline):
    hashes = json.loads(Path(baseline).read_text(encoding='utf-8'))['hashes']
    if not hashes or any(not p.startswith('src/api_agent/agent/') for p in hashes):
        raise ValueError('invalid_catalog_baseline')
    current={p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
        for p in Path('src/api_agent/agent').rglob('*') if p.is_file() and p.suffix in {'.py','.json','.sql'}}
    return sorted(p for p in hashes.keys()|current.keys() if hashes.get(p)!=current.get(p))


def main(mode):
    if mode not in CASES: return 1
    directory=Path(os.environ.get('S09_EVIDENCE_DIR',ROOT/'evidence/S09'))/('S09e2-'+mode+'-'+uuid.uuid4().hex)
    directory.mkdir(parents=True,exist_ok=False)
    record={'stage':'S09e2','mode':mode,'exit_code':1,'independent_accept':False,
        'scope':'code-only D77; scripted decisions, actual GGUF tokenizer/Docker/HTTP in good',
        'real_primary_generation':False,'catalog_modernized':False,'parent_acceptance_complete':False}
    with (directory/'checks.log').open('x',encoding='utf-8') as log, redirect_stdout(log),redirect_stderr(log):
        try:
            before=source_hashes(); record['source_hashes_before']=before
            baseline=os.environ['S09_CATALOG_BASELINE']; record['catalog_baseline']=baseline
            if catalog_changes(baseline) or frozen_changes(): raise ValueError('baseline_changed')
            if mode=='good':
                if not os.environ.get('WORKSPACE_TOKENIZER_URL'): raise ValueError('real_tokenizer_required')
                record['sandbox']=preflight(); os.environ['WORKSPACE_DOCKER_TESTS']='1'
            tests=unittest.defaultTestLoader.loadTestsFromNames(CASES[mode]); count=tests.countTestCases()
            result=unittest.TextTestRunner(stream=log,verbosity=2).run(tests)
            after=source_hashes()
            record.update(tests_run=result.testsRun,expected_tests=count,skips=len(result.skipped),
                failures=len(result.failures),errors=len(result.errors),
                source_changes=sorted(k for k in before.keys()|after.keys() if before.get(k)!=after.get(k)),
                catalog_changes=catalog_changes(baseline),frozen_changes=frozen_changes())
            if (result.wasSuccessful() and result.testsRun==count and not result.skipped
                    and not record['source_changes'] and not record['catalog_changes'] and not record['frozen_changes']):
                record['exit_code']=0 if mode=='good' else 2
        except Exception as error: record['error_type']=type(error).__name__
    (directory/'verification.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'stage':'S09e2','mode':mode,'exit_code':record['exit_code'],'evidence':str(directory)}))
    return record['exit_code']


if __name__=='__main__': raise SystemExit(main(sys.argv[1]))

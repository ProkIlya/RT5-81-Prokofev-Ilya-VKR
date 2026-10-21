"""S09d1 metadata/SDK offline gates: good0, обнаруженный bad2, сбой1."""
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
P = 'test_workspace_sdk.WorkspaceSdkTests.'
CASES = {
    'good': [P + name for name in (
        'test_three_unrelated_schemas_exact_signatures_and_distinct_path_query',
        'test_nested_body_and_optional_omission_do_not_coerce_json',
        'test_metadata_change_preserves_execution_hash_and_creates_new_sdk_version',
        'test_descriptions_from_refs_and_schema_fields_remain_untrusted_metadata',
        'test_paginated_index_search_and_sections_reach_beyond_first_page_without_rpc',
        'test_large_schema_is_read_by_pointer_with_explicit_omissions',
        'test_project_builder_cli_saves_artifact_and_reuses_identical_version',
        'test_shared_path_description_is_preserved_for_each_allowed_operation')],
    'bad': [P + name for name in (
        'test_untrusted_names_and_facade_collisions_cannot_become_code',
        'test_only_project_grants_are_generated_and_stale_config_fails',
        'test_immutable_artifact_roundtrip_and_tampering_is_rejected',
        'test_source_and_manifest_changes_cannot_be_loaded_with_old_expected_hash',
        'test_application_facade_exact_calls_cannot_supply_fact_evidence_or_verdict',
        'test_long_identifiers_and_small_index_budget_remain_navigable',
        'test_truncated_description_tail_still_changes_metadata_identity')]}


def main(mode):
    directory = Path(os.environ.get('S09_EVIDENCE_DIR', ROOT / 'evidence/S09')) / ('S09d1-' + mode + '-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    record = {'stage': 'S09d1', 'mode': mode, 'exit_code': 1, 'independent_accept': False,
        'kind': 'offline metadata/AST/SDK artifact; controlled dispatcher, no target HTTP or LLM'}
    with (directory / 'checks.log').open('w', encoding='utf-8') as stream, redirect_stdout(stream), redirect_stderr(stream):
        try:
            names = CASES[mode]
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
            record.update(tests_run=result.testsRun, skips=len(result.skipped))
            if result.wasSuccessful() and result.testsRun == len(names) and not result.skipped:
                if mode == 'good':
                    from test_workspace_sdk import WorkspaceSdkTests
                    fixture = WorkspaceSdkTests()
                    artifact, _, _ = fixture.build()
                    saved = artifact.save(directory / 'artifact')
                    record['artifact_hash'] = artifact.hash
                    record['artifact_directory'] = str(saved)
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
    print(json.dumps({'stage': 'S09d1', 'mode': mode, 'exit_code': record['exit_code'], 'evidence': str(directory)}))
    return record['exit_code']


if __name__ == '__main__': raise SystemExit(main(sys.argv[1]))

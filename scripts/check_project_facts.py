"""S09a: содержательные good/bad controls; подготовка/сбой проверки дают 1."""
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

PREFIX = 'test_project_facts.ProjectFactTests.'
CASES = {
    'good': [PREFIX + name for name in (
        'test_verified_facts_survive_restart_and_retrieve_in_other_session_without_http',
        'test_create_read_link_checks_actual_http_and_does_not_store_runtime_id_as_knowledge',
        'test_expiry_and_reset_exclude_observations_without_rewriting_history',
        'test_explicit_invalidation_requires_a_real_contradicting_exchange',
        'test_read_only_legacy_schema_never_migrates')],
    'bad': [PREFIX + name for name in (
        'test_existing_wrong_evidence_and_unknown_exchange_do_not_verify',
        'test_failed_predicate_cannot_be_promoted_by_cached_validation_pass',
        'test_foreign_project_and_session_cannot_supply_evidence',
        'test_inferred_or_user_provided_origin_never_becomes_verified',
        'test_expiry_and_reset_exclude_observations_without_rewriting_history',
        'test_reproposal_does_not_renew_evidence_time_and_invalid_policy_is_rejected',
        'test_increasing_ttl_does_not_resurrect_previously_expired_fact',
        'test_changed_operation_is_stale_but_unrelated_spec_change_is_not',
        'test_declaration_pointer_and_descriptor_cannot_be_forged',
        'test_wrong_response_id_does_not_verify_create_read_link',
        'test_conflicting_observations_are_not_resolved_by_newest_timestamp',
        'test_redacted_value_and_incomplete_response_never_verify',
        'test_fact_payload_cannot_claim_foreign_origin_or_unchecked_value',
        'test_explicit_invalidation_requires_a_real_contradicting_exchange',
        'test_401_observation_does_not_become_universal_auth_requirement')],
}


def smoke(directory):
    """Настоящие HTTP и SQLite, controlled decisions; без live LLM заявления."""
    from api_agent.agent import StartAgentRun
    from api_agent.storage import SQLiteHistory
    from demo_api.server import DemoServer
    from agent_support import count_test_tokens
    from test_catalog_execution import GenericExecutionTests, GenericModel
    with DemoServer() as server, SQLiteHistory(directory / 'smoke.sqlite3') as history:
        catalog, _, workflow, create, read = GenericExecutionTests().workflow(server)
        run = StartAgentRun(server.target, catalog, workflow=workflow, history_store=history,
            token_counter=count_test_tokens).start('p', 'A', 'smoke', workflow.goal, GenericModel(create, read))
        before = len(server.journal)
        facts = history.read_facts('p', catalog=catalog)
        assert {f['type'] for f in facts} == {'declared_contract', 'response_observation', 'create_read_link'}
        assert len(server.journal) == before == 2
        assert server.journal[1]['path'] == '/orders/' + str(server.journal[0]['created_id'])
        link = next(f for f in facts if f['type'] == 'create_read_link')
        assert str(server.journal[0]['created_id']) not in json.dumps(link)
        journal = list(server.journal)
        server.reset()
        history.advance_fact_epoch('p')
        after_reset = history.read_facts('p', catalog=catalog)
        assert all(f['type'] == 'declared_contract' for f in after_reset)
        assert not server.orders and not server.journal
        for name, value in [('run', run), ('facts', facts), ('after-reset', after_reset), ('journal', journal), ('reset', server.reset_log)]:
            (directory / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        return {'kind': 'controlled model + real HTTP + SQLite', 'http_requests': before,
                'fact_types': sorted({f['type'] for f in facts}), 'new_http_for_retrieval': 0}


def main(mode):
    directory = Path(os.environ.get('S09_EVIDENCE_DIR', ROOT / 'evidence/S09')) / ('S09a-' + mode + '-' + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    record = {'stage': 'S09a', 'mode': mode, 'exit_code': 1,
              'kind': 'controlled tests with real HTTP/SQLite; not full STAND-37 or live LLM'}
    with (directory / 'checks.log').open('w', encoding='utf-8') as stream, redirect_stdout(stream), redirect_stderr(stream):
        try:
            names = CASES[mode]
            result = unittest.TextTestRunner(stream=stream, verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
            record.update(tests_run=result.testsRun, skips=len(result.skipped))
            if result.wasSuccessful() and result.testsRun == len(names) and not result.skipped:
                if mode == 'good':
                    record['smoke'] = smoke(directory)
                record['exit_code'] = 0 if mode == 'good' else 2
        except Exception as error:
            record['error_type'] = type(error).__name__
    record['head'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    record['branch'] = subprocess.check_output(['git', 'branch', '--show-current'], cwd=ROOT, text=True).strip()
    sources = sorted(set((ROOT / 'src').rglob('*.py')) | set((ROOT / 'src').rglob('*.sql')) |
                     set((ROOT / 'scripts').glob('*.py')) | set((ROOT / 'tests').rglob('*.py')) |
                     set((ROOT / 'tests/fixtures').glob('*.json')))
    record['source_hashes'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sources}
    (directory / 'tracked.patch').write_bytes(subprocess.check_output(['git', 'diff', '--binary'], cwd=ROOT))
    (directory / 'verification.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'stage': 'S09a', 'mode': mode, 'exit_code': record['exit_code'], 'evidence': str(directory)}))
    return record['exit_code']


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))

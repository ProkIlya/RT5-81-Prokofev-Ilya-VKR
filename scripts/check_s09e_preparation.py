"""S09e1: карта35+2 и общий compiler; good0/bad2/error1, без LLM/HTTP."""
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from experiments.prepare_s09e_inventory import build_inventory
from scripts.readiness_inventory import validate_inventory
from scripts.check_condition_integration import source_hashes
from scripts.evaluate_condition_trip_series import frozen_changes

CASES = {'good': ['test_readiness_inventory.InventoryGoodTests',
    'test_constructor_contract.ConstructorFormTests'], 'bad': ['test_readiness_inventory.InventoryBadTests']}


def main(mode):
    if mode not in CASES: return 1
    directory = Path(os.environ.get('S09_EVIDENCE_DIR', ROOT / 'evidence/S09')) / ('S09e1-' + mode + '-' + uuid.uuid4().hex)
    directory.mkdir(parents=True, exist_ok=False)
    record = {'stage': 'S09e1', 'mode': mode, 'exit_code': 1, 'independent_accept': False,
        'scope': 'offline inventory audit and existing conditions compiler; no primary LLM or task HTTP',
        'comparison_ready': False, 'parent_acceptance_complete': False}
    with (directory / 'checks.log').open('x', encoding='utf-8') as log, redirect_stdout(log), redirect_stderr(log):
        try:
            before = source_hashes(); record['source_hashes_before'] = before
            if frozen_changes(): raise ValueError('frozen_changed')
            snapshot = os.environ['S09E_SOURCE_SNAPSHOT']
            inventory = build_inventory(snapshot)
            source = Path(inventory['source_suite'])
            suite = json.loads(source.read_text(encoding='utf-8'))
            record['inventory'] = validate_inventory(suite, inventory, source_file=source)
            (directory / 'inventory.json').write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding='utf-8')
            if mode == 'bad':
                missing = deepcopy(inventory)
                missing['cases'] = [case for case in missing['cases'] if case['id'] != 'STAND-33']
                try: validate_inventory(suite, missing, source_file=source)
                except ValueError: record['missing_real_case_detected'] = True
                else: raise AssertionError('missing_real_case_accepted')
            tests = unittest.defaultTestLoader.loadTestsFromNames(CASES[mode])
            count = tests.countTestCases()
            result = unittest.TextTestRunner(stream=log, verbosity=2).run(tests)
            after = source_hashes()
            record.update(tests_run=result.testsRun, expected_tests=count, skips=len(result.skipped),
                failures=len(result.failures), errors=len(result.errors),
                source_changes=sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k)),
                frozen_changes=frozen_changes())
            if (result.wasSuccessful() and result.testsRun == count and not result.skipped
                    and not record['source_changes'] and not record['frozen_changes']):
                record['exit_code'] = 0 if mode == 'good' else 2
        except Exception as error: record['error_type'] = type(error).__name__
    (directory / 'verification.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'stage': 'S09e1', 'mode': mode, 'exit_code': record['exit_code'], 'evidence': str(directory)}))
    return record['exit_code']


if __name__ == '__main__': raise SystemExit(main(sys.argv[1]))

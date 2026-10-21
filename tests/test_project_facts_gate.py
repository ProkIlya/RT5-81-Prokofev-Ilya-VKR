"""Gate S09a отличает пойманное нарушение от сбоя подготовки/сломавшегося oracle."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]


class ProjectFactGateTests(unittest.TestCase):
    def gate(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.check_project_facts'), 'Нет исполняемого S09a gate')
        from scripts import check_project_facts
        return check_project_facts

    def test_bad_gate_rejects_bypassed_predicate_validator(self):
        gate = self.gate()
        from api_agent.validation.facts import validate_exchange
        def accept_bad(*args, **kwargs):
            result = validate_exchange(*args, **kwargs)
            for check in result['checks']:
                check['verdict'] = 'pass'
            return result
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'S09_EVIDENCE_DIR': folder}):
            with patch('api_agent.validation.facts.validate_exchange', side_effect=accept_bad):
                self.assertEqual(gate.main('bad'), 1)

    def test_missing_test_is_preparation_error_not_expected_bad(self):
        gate = self.gate()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'S09_EVIDENCE_DIR': folder}), \
                patch.dict(gate.CASES, {'bad': ['missing_s09a_test.Case.test_missing']}):
            self.assertEqual(gate.main('bad'), 1)


if __name__ == '__main__':
    unittest.main()

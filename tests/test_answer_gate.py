"""Negative gate S09c должен ломаться вместе с проверкой claims."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]


class AnswerGateTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.check_answer_feedback'), 'Нет gate S09c')
        from scripts import check_answer_feedback
        return check_answer_feedback

    def test_blind_claim_acceptance_does_not_produce_expected_bad_two(self):
        gate = self.api()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'S09_EVIDENCE_DIR': folder}), \
                patch('api_agent.goals.feedback.check_claims', return_value=[]):
            self.assertEqual(gate.main('bad'), 1)

    def test_missing_test_is_execution_failure_not_bad_two(self):
        gate = self.api()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'S09_EVIDENCE_DIR': folder}), \
                patch.dict(gate.CASES, {'bad': ['missing_answer_test.Case.test_missing']}):
            self.assertEqual(gate.main('bad'), 1)


if __name__ == '__main__': unittest.main()

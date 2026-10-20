"""S09b gate не считает пропуск oracle или сбой подготовки ожидаемым bad."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]


class GoalGateTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.check_goals'), 'Нет исполняемых gates S09b')
        from scripts import check_goals
        return check_goals

    def test_auto_accepting_arbitrary_json_breaks_negative_gate(self):
        gate = self.api()
        from api_agent.goals import compile_goal
        from api_agent.validation import fingerprint
        def blind(catalog, config, goal, proposal, **kwargs):
            kwargs['confirmation'] = {'proposal_hash': fingerprint(proposal), 'approved': True, 'complete': True}
            kwargs['force_confirmation'] = False
            return compile_goal(catalog, config, goal, proposal, **kwargs)
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'S09_EVIDENCE_DIR': folder}), \
                patch('api_agent.goals.compile_goal', side_effect=blind):
            self.assertEqual(gate.main('S09b1', 'bad'), 1)

    def test_missing_test_module_does_not_count_as_bad_two(self):
        gate = self.api()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'S09_EVIDENCE_DIR': folder}), \
                patch.dict(gate.CASES['S09b1'], {'bad': ['missing_goal_module.Case.test_missing']}):
            self.assertEqual(gate.main('S09b1', 'bad'), 1)


if __name__ == '__main__': unittest.main()

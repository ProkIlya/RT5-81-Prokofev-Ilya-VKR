"""Набор качества не теряет неподдержанные цели и не подменяет их текст."""

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'src')]
try:
    from scripts.evaluate_stand import plan_attempts
except ImportError:
    plan_attempts = None


class StandMatrixTests(unittest.TestCase):
    def test_every_variant_has_three_attempts_including_blocked_goals(self):
        self.assertIsNotNone(plan_attempts, 'Нет полной матрицы попыток стенда')
        suite = {'cases': [
            {'id':'STAND-01','goal':'Найди и сравни две трассы','required_capabilities':['goal_oracle']},
            {'id':'STAND-37','goal':'Профиль без входа','variants':['good','auth_bypass'],'required_capabilities':['negative_auth']},
        ]}
        rows = plan_attempts(suite, {'STAND-37'})
        self.assertEqual(len(rows), 9)
        self.assertEqual([r['status'] for r in rows[:3]], ['blocked_capability']*3)
        self.assertEqual([r['rendered_goal'] for r in rows[:3]], ['Найди и сравни две трассы']*3)
        self.assertEqual([r['variant'] for r in rows[3:]], ['good']*3+['auth_bypass']*3)
        self.assertEqual([r['attempt'] for r in rows[3:]], [1,2,3,1,2,3])


if __name__ == '__main__':
    unittest.main()

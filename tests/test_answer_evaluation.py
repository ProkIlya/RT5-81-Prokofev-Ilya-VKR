"""Сборщик сравнения действительно читает разные API и формы ответа."""
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from scripts.evaluate_answer_feedback import collect


class AnswerEvaluationTests(unittest.TestCase):
    def test_three_apis_arrays_wrappers_multiple_entities_and_holdout_use_http(self):
        for domain, shape, multiple, actual in [('pet', 'object', False, 'Murka'),
            ('weather', 'wrapped', False, 18), ('stock', 'array', False, 5),
            ('pet', 'object', True, 'Murka'), ('holdout', 'nested', False, 11)]:
            with self.subTest(domain=domain, shape=shape, multiple=multiple), tempfile.TemporaryDirectory() as folder:
                source = collect(Path(folder), domain, shape, multiple=multiple)
                self.assertEqual(source['requests'], 2 if multiple else 1)
                self.assertEqual(source['report']['goal_verdict'], 'FAIL')
                self.assertEqual(source['report']['answer']['conditions'][0]['actual'], actual)
                self.assertEqual(source['answer_feedback']['calls'], 0)


if __name__ == '__main__': unittest.main()

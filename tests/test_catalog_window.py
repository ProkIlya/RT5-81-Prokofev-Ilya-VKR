"""Отбор учитывает объявленное окно Run, а не скрытую константу экспериментатора."""

import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
import test_catalog_discovery as support


class WindowTests(unittest.TestCase):
    def test_declared_larger_window_preserves_cards_that_fit_actual_preflight(self):
        cat, cfg, wf = support.DiscoveryTests().workflow("http://127.0.0.1:18080")
        wf.goal = "item000"
        run = {
            "run_id": "experimental",
            "limits": {"max_context_tokens": 8192, "max_output_tokens": 512},
        }
        wf.initial_context(run, lambda payload: 1900)
        self.assertEqual(run["selection_manifest"]["selected_ids"], ["item000"])
        self.assertEqual(run["selection_manifest"]["max_context_tokens"], 8192)


if __name__ == "__main__":
    unittest.main()

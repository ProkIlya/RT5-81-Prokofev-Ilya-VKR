"""Ожидание owner approval ограничено сроком и отменой Run."""

import importlib.util
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class ApprovalInputTests(unittest.TestCase):
    def approve(self, reader, cancel=None, timeout=0.02):
        self.assertIsNotNone(
            importlib.util.find_spec("api_agent.policy.approval"),
            "Отсутствует bounded CLI approval",
        )
        from api_agent.policy.approval import read_approval

        return read_approval(
            {"action_hash": "own", "config_version": 1},
            reader=reader,
            cancel=cancel or threading.Event(),
            timeout=timeout,
        )

    def test_exact_yes_approves_bound_action(self):
        self.assertEqual(
            self.approve(lambda: "yes\n"),
            {"action_hash": "own", "config_version": 1, "approved": True},
        )
        self.assertFalse(self.approve(lambda: "true\n")["approved"])

    def test_cancel_does_not_even_start_reader(self):
        cancel = threading.Event()
        cancel.set()
        self.assertFalse(
            self.approve(lambda: self.fail("reader started"), cancel=cancel)["approved"]
        )

    def test_waiting_input_times_out_without_approving(self):
        blocked = threading.Event()
        try:
            self.assertFalse(self.approve(lambda: blocked.wait(1))["approved"])
        finally:
            blocked.set()


if __name__ == "__main__":
    unittest.main()

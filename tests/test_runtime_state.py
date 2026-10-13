"""Проверки authority Run State: исходное значение, scope и секреты."""

import importlib.util
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class RuntimeStateTests(unittest.TestCase):
    def state(self, run="r"):
        self.assertIsNotNone(
            importlib.util.find_spec("api_agent.state"),
            "Типизированный Run State ещё не реализован",
        )
        from api_agent.state import RunState

        return RunState(
            "p", run, {"create": {"/id": {"type": "integer", "secret": False}}}
        )

    def exchange(self, value, run="r"):
        return {
            "id": "E1",
            "run_id": run,
            "operation_id": "create",
            "response": {"json": {"id": value}},
        }

    def test_public_value_keeps_original_type_and_provenance(self):
        state = self.state()
        record = state.remember(self.exchange(834721), "/id")
        self.assertEqual(state.resolve(record["ref"], "integer"), 834721)
        self.assertEqual(record["value"], 834721)
        self.assertEqual(record["exchange_id"], "E1")
        self.assertEqual(record["path"], "/id")

    def test_bool_is_not_integer_and_unknown_path_has_no_record(self):
        state = self.state()
        with self.assertRaises(ValueError):
            state.remember(self.exchange(True), "/id")
        with self.assertRaises(ValueError):
            state.remember(self.exchange(1), "/missing")
        self.assertEqual(state.public(), [])

    def test_foreign_run_and_reference_are_denied(self):
        state = self.state()
        with self.assertRaises(ValueError):
            state.remember(self.exchange(1, "foreign"), "/id")
        ref = state.remember(self.exchange(10), "/id")["ref"]
        with self.assertRaises(ValueError):
            self.state("other").resolve(ref, "integer")

    def test_two_objects_do_not_overwrite_first(self):
        state = self.state()
        a = state.remember(self.exchange(834721), "/id")
        b = self.exchange(123987)
        b["id"] = "E2"
        b = state.remember(b, "/id")
        self.assertEqual(state.resolve(a["ref"], "integer"), 834721)
        self.assertEqual(state.resolve(b["ref"], "integer"), 123987)

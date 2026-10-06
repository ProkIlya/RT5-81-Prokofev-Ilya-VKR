"""Принадлежность команды определяется исходными ID, а не публичной маской."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import itertools
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]
from api_agent.agent import StartAgentRun
from api_agent.openapi import parse_document
from demo_api.server import DemoServer
from agent_support import ObserverModel

CANARY = "R3-SCOPE-CANARY"


class CommandScopeTests(unittest.TestCase):
    def exercise(
        self, ids, *, secrets=(CANARY,), terminal="completed", concurrent=False
    ):
        """Настоящий Demo HTTP; fake model изолирует проверку владельца от LLM."""
        document = json.loads((ROOT / "tests/fixtures/demo_openapi.json").read_text())
        if terminal == "contract_redaction_conflict":
            document["paths"]["/pets/1"]["get"]["responses"]["200"]["content"][
                "application/json"
            ]["schema"]["properties"]["name"]["enum"] = [CANARY]
        snapshots, payloads = [], []
        observer = ObserverModel()
        cancel = threading.Event()
        if terminal == "cancelled":
            cancel.set()

        def model(payload):
            payloads.append(deepcopy(payload))
            return observer(payload)

        def sink(run):
            snapshots.append(deepcopy(run))
            if terminal == "trace_error":
                raise RuntimeError(CANARY)

        with DemoServer() as demo:
            app = StartAgentRun(
                demo.target, parse_document(document), secrets=secrets, trace_sink=sink
            )

            def start(scope=ids[:2], message=ids[2], goal=ids[3]):
                return app.start(*scope, message, goal, model, cancel=cancel)

            # Неожиданный внутренний отказ моделируем после реального HTTP:
            # проверяем дедупликацию терминального Run с сохранённым exchange.
            original = app._drive_loop

            def drive(*args):
                original(*args)
                if terminal == "runtime_error":
                    raise RuntimeError(CANARY)

            with patch.object(app, "_drive_loop", side_effect=drive):
                if concurrent:
                    barrier = threading.Barrier(4)

                    def delivery(_):
                        barrier.wait(timeout=5)
                        return start()

                    with ThreadPoolExecutor(max_workers=4) as pool:
                        runs = list(pool.map(delivery, range(4)))
                    run = runs[0]
                    self.assertTrue(all(r == run for r in runs))
                else:
                    run = start()

            expected_status = {
                "completed": "completed",
                "contract_redaction_conflict": "blocked",
                "cancelled": "cancelled",
                "runtime_error": "failed",
                "trace_error": "failed",
            }[terminal]
            self.assertEqual(run["status"], expected_status)
            if terminal != "completed":
                self.assertEqual(run["stop_reason"], terminal)
            counts = (len(payloads), len(demo.journal))
            self.assertEqual(run["requests"], counts[1])
            self.assertEqual(start(goal="Changed goal"), run)
            # Даже изменяемый публичный результат не меняет кэш и authority.
            altered = start()
            altered["project_id"] = "foreign"
            altered["steps"].append({"injected": True})
            altered["limits"]["max_requests"] = 99
            self.assertEqual(start(), run)
            foreign_scopes = [("foreign", ids[1]), (ids[0], "foreign")]
            for index in (0, 1):
                if CANARY in ids[index]:
                    alias = list(ids[:2])
                    alias[index] = alias[index].replace(CANARY, "[REDACTED]")
                    foreign_scopes.append(tuple(alias))
            errors = []

            def reject(scope):
                with self.assertRaisesRegex(
                    ValueError, "^message scope mismatch$"
                ) as ctx:
                    start(scope=scope)
                return str(ctx.exception)

            if concurrent:
                with ThreadPoolExecutor(max_workers=4) as pool:
                    errors.extend(pool.map(reject, foreign_scopes))
            else:
                errors.extend(reject(scope) for scope in foreign_scopes)
            self.assertEqual(counts, (len(payloads), len(demo.journal)))
            if secrets:
                self.assertNotIn(CANARY, json.dumps([run, snapshots, payloads, errors]))
            return counts

    def test_all_command_fields_and_mask_collisions(self):
        # Все 16 сочетаний: secret может находиться независимо в каждом поле.
        for flags in itertools.product((False, True), repeat=4):
            with self.subTest(flags=flags):
                ids = tuple(
                    name + ("-" + CANARY if flag else "")
                    for name, flag in zip(
                        ("project", "session", "message", "goal"), flags
                    )
                )
                self.assertEqual(self.exercise(ids)[1], 1)

    def test_concurrent_duplicate_and_foreign_scope(self):
        self.assertEqual(self.exercise((CANARY,) * 4, concurrent=True)[1], 1)

    def test_terminal_runs_keep_original_scope(self):
        for terminal in (
            "contract_redaction_conflict",
            "runtime_error",
            "trace_error",
            "cancelled",
        ):
            for concurrent in (False, True):
                with self.subTest(terminal=terminal, concurrent=concurrent):
                    counts = self.exercise(
                        (CANARY,) * 4, terminal=terminal, concurrent=concurrent
                    )
                    self.assertEqual(counts[1], int(terminal == "runtime_error"))

    def test_no_secret_configuration_control(self):
        self.assertEqual(self.exercise((CANARY,) * 4, secrets=())[1], 1)

    def test_distinct_messages_with_same_public_ids_create_distinct_runs(self):
        """Коллизия отображения не объединяет разные исходные action-messages."""
        document = json.loads((ROOT / "tests/fixtures/demo_openapi.json").read_text())
        with DemoServer() as demo:
            app = StartAgentRun(
                demo.target, parse_document(document), secrets=(CANARY,)
            )
            first_ids = (CANARY,) * 3
            alias_ids = ("[REDACTED]",) * 3
            first = app.start(*first_ids, "Check pet", ObserverModel())
            second = app.start(*alias_ids, "Check pet", ObserverModel())
            self.assertNotEqual(first["run_id"], second["run_id"])
            for field in ("project_id", "session_id", "message_id"):
                self.assertEqual(first[field], second[field])
            self.assertEqual(app.start(*first_ids, "Changed", ObserverModel()), first)
            self.assertEqual(app.start(*alias_ids, "Changed", ObserverModel()), second)
            self.assertEqual(len(demo.journal), 2)


if __name__ == "__main__":
    unittest.main()

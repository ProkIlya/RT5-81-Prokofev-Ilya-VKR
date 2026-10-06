"""Регрессии R3: недоверенные структуры и transport всегда дают итоговый Run."""

import json
import math
import sys
import threading
import unittest
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from api_agent.agent import Limits, StartAgentRun
from api_agent.agent.model import LocalModel
from api_agent.openapi import parse_document
from agent_support import ObserverModel, reply
from test_transport_deadlines import serve_raw


class FailureBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.catalog = parse_document(
            json.loads((ROOT / "tests/fixtures/demo_openapi.json").read_text())
        )

    def assert_terminal(self, app, model, snapshots, **kwargs):
        run = app.start("p", "s", "m", "Check pet", model, **kwargs)
        self.assertTrue(run["stop_reason"])
        self.assertEqual(snapshots[-1], run)
        self.assertEqual(app.start("p", "s", "m", "Check pet", model), run)
        return run

    def test_deep_http_json_has_terminal_trace_and_duplicate_without_http(self):
        # Реальный HTTP меньше body budget: прежний json.loads падал до сохранения Run.
        for depth in (1500, 3000, 100):
            with self.subTest(depth=depth):
                body = b"[" * depth + b"0" + b"]" * depth
                header = f"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode()
                port, thread = serve_raw([(header + body, 0)])
                snapshots = []
                app = StartAgentRun(
                    f"http://127.0.0.1:{port}",
                    self.catalog,
                    trace_sink=snapshots.append,
                )
                run = self.assert_terminal(app, ObserverModel(), snapshots)
                thread.join(3)
                self.assertEqual(run["requests"], 1)
                self.assertEqual(len(run["exchanges"]), 1)
                self.assertNotEqual(run["exchanges"][0]["outcome"], "received")
                self.assertIsNone(run["exchanges"][0]["response"]["json"])
                self.assertEqual(run["status"], "partial")

    def test_deep_model_metadata_stops_before_network_without_canary(self):
        for kind in (
            "deep",
            "nodes",
            "type",
            "nonfinite",
            "string",
            "unknown",
            "null",
            "size",
            "nested",
        ):
            with self.subTest(kind=kind):
                value = "CANARY"
                if kind == "deep":
                    for _ in range(10000):
                        value = [value]
                elif kind == "nodes":
                    value = ["CANARY"] * 20000
                elif kind == "type":
                    value = {"wall_seconds": ("CANARY",)}
                elif kind == "nonfinite":
                    value = {"wall_seconds": math.inf}
                elif kind == "string":
                    value = {"wall_seconds": "CANARY"}
                elif kind == "null":
                    value = {"wall_seconds": None}
                elif kind == "size":
                    value = {"wall_seconds": "CANARY" + "x" * 1048577}
                elif kind == "nested":
                    value = {"wall_seconds": {"value": "CANARY"}}
                else:
                    value = {"unexpected_metric": 3}
                response = reply("execute_operation", {"operation_id": "getPet"})
                response["_timing"] = value
                snapshots = []
                app = StartAgentRun(
                    "http://127.0.0.1:1",
                    self.catalog,
                    secrets=["CANARY"],
                    trace_sink=snapshots.append,
                    limits=Limits(malformed_retries=0),
                )
                run = self.assert_terminal(app, lambda p: response, snapshots)
                self.assertEqual(run["requests"], 0)
                self.assertEqual(run["stop_reason"], "malformed_limit")
                self.assertNotIn("CANARY", json.dumps(snapshots))

    def test_shallow_numeric_json_within_budgets_is_preserved(self):
        # Ограничитель структуры не должен путать короткие float с длинными
        # строками: этот настоящий HTTP body заметно меньше 64 KiB.
        body = b"[" + b",".join([b"1.0"] * 4000) + b"]"
        header = f"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode()
        port, thread = serve_raw([(header + body, 0)])
        snapshots = []
        app = StartAgentRun(
            f"http://127.0.0.1:{port}", self.catalog, trace_sink=snapshots.append
        )
        run = self.assert_terminal(app, ObserverModel(), snapshots)
        thread.join(3)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["exchanges"][0]["response"]["json"], [1.0] * 4000)

    def test_real_model_run_deadline_preserves_time_limit(self):
        header = b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
        port, thread = serve_raw([(header, 0), (b"data: " + b"x" * 100, 0.003)])
        snapshots = []
        app = StartAgentRun(
            "http://127.0.0.1:1",
            self.catalog,
            trace_sink=snapshots.append,
            limits=Limits(max_seconds=0.06),
        )
        run = self.assert_terminal(
            app, LocalModel(f"http://127.0.0.1:{port}", timeout=2), snapshots
        )
        thread.join(3)
        self.assertEqual(run["stop_reason"], "time_limit")
        self.assertEqual(run["requests"], 0)

    def test_generation_length_is_diagnostic_and_never_executed(self):
        event = {
            "choices": [
                {"finish_reason": "length", "delta": {"reasoning_content": "CANARY"}}
            ]
        }
        body = b"data: " + json.dumps(event).encode() + b"\n\ndata: [DONE]\n\n"
        port, thread = serve_raw([(b"HTTP/1.0 200 OK\r\n\r\n" + body, 0)])
        snapshots = []
        app = StartAgentRun(
            "http://127.0.0.1:1",
            self.catalog,
            trace_sink=snapshots.append,
            secrets=["CANARY"],
        )
        run = self.assert_terminal(
            app, LocalModel(f"http://127.0.0.1:{port}", timeout=2), snapshots
        )
        thread.join(3)
        self.assertEqual(run["stop_reason"], "model_generation_limit")
        self.assertEqual(run["steps"][0]["finish_reason"], "length")
        self.assertIn("wall_seconds", run["steps"][0]["timing"])
        self.assertEqual(run["requests"], 0)
        self.assertNotIn("CANARY", json.dumps(snapshots))

    def test_cancel_on_model_exception_wins_over_model_error(self):
        cancel = threading.Event()

        def model(payload):
            cancel.set()
            raise RuntimeError("CANARY")

        snapshots = []
        app = StartAgentRun(
            "http://127.0.0.1:1",
            self.catalog,
            trace_sink=snapshots.append,
            secrets=["CANARY"],
        )
        run = self.assert_terminal(app, model, snapshots, cancel=cancel)
        self.assertEqual(run["stop_reason"], "cancelled")
        self.assertNotIn("CANARY", json.dumps(snapshots))

    def test_unexpected_error_after_real_http_retains_exchange_and_duplicate(self):
        from api_agent.first_http import Session

        body = b'{"id":1,"name":"CANARY"}'
        header = f"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n".encode()
        port, thread = serve_raw([(header + body, 0)])
        snapshots = []
        app = StartAgentRun(
            f"http://127.0.0.1:{port}",
            self.catalog,
            secrets=["CANARY"],
            trace_sink=snapshots.append,
        )
        original = Session.execute

        def broken(session, args):
            original(session, args)
            raise RecursionError("CANARY")

        with patch.object(Session, "execute", broken):
            run = self.assert_terminal(app, ObserverModel(), snapshots)
        thread.join(3)
        self.assertEqual(run["requests"], 1)
        self.assertEqual(len(run["exchanges"]), 1)
        self.assertEqual(run["stop_reason"], "runtime_error")
        self.assertNotIn("CANARY", json.dumps(snapshots))

    def test_adapter_timeout_is_distinct_from_run_deadline(self):
        port, thread = serve_raw(
            [(b"HTTP/1.0 200 OK\r\n\r\n", 0), (b"data: " + b"x" * 100, 0.003)]
        )
        snapshots = []
        app = StartAgentRun(
            "http://127.0.0.1:1",
            self.catalog,
            trace_sink=snapshots.append,
            limits=Limits(max_seconds=2),
        )
        run = self.assert_terminal(
            app, LocalModel(f"http://127.0.0.1:{port}", timeout=0.06), snapshots
        )
        thread.join(3)
        self.assertEqual(run["stop_reason"], "model_timeout")
        self.assertEqual(run["requests"], 0)

    def test_deep_sse_json_is_classified_without_raw_canary(self):
        body = b"data: " + b"[" * 1500 + b'"CANARY"' + b"]" * 1500 + b"\n\n"
        port, thread = serve_raw([(b"HTTP/1.0 200 OK\r\n\r\n" + body, 0)])
        snapshots = []
        app = StartAgentRun(
            "http://127.0.0.1:1",
            self.catalog,
            trace_sink=snapshots.append,
            secrets=["CANARY"],
        )
        run = self.assert_terminal(
            app, LocalModel(f"http://127.0.0.1:{port}", timeout=2), snapshots
        )
        thread.join(3)
        self.assertEqual(run["stop_reason"], "model_malformed_stream")
        self.assertEqual(run["requests"], 0)
        self.assertNotIn("CANARY", json.dumps(snapshots))


if __name__ == "__main__":
    unittest.main()

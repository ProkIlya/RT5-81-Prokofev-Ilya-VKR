"""Контроль адаптивности и запрета побочных действий на настоящем Demo API."""

import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from demo_api.server import DemoServer
from api_agent.openapi import parse_document


from agent_support import ObserverModel, reply


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        # Отсутствие нового use case даёт явную красную фазу до реализации.
        from api_agent.agent import StartAgentRun, Limits

        self.StartAgentRun, self.Limits = StartAgentRun, Limits
        self.catalog = parse_document(
            json.loads((ROOT / "tests/fixtures/demo_openapi.json").read_text())
        )
        self.demo = DemoServer().__enter__()
        self.addCleanup(self.demo.__exit__, None, None, None)

    def app(self, **kwargs):
        return self.StartAgentRun(self.demo.target, self.catalog, **kwargs)

    def test_observation_changes_next_action_with_same_goal(self):
        traces = []
        for mode in ("good", "invalid_json"):
            self.demo.reset()
            self.demo.mode = mode
            run = self.app().start("p", "s", "m", "Check pet", ObserverModel())
            traces.append([step["action"]["name"] for step in run["steps"]])
            self.assertEqual(len(self.demo.journal), 1)
            self.assertEqual(run["requests"], 1)
        self.assertEqual(
            traces,
            [
                ["execute_operation", "finish_run"],
                ["execute_operation", "inspect_operation", "finish_run"],
            ],
        )

    def test_duplicate_message_returns_existing_run_without_http(self):
        app = self.app()
        run = app.start("p", "s", "m", "Check pet", ObserverModel())
        again = app.start("p", "s", "m", "Changed goal", ObserverModel())
        self.assertEqual(run, again)
        self.assertEqual(len(self.demo.journal), 1)
        self.assertEqual(run["goal"], "Check pet")

    def test_expectation_and_trace_saved_before_network(self):
        snapshots = []

        def sink(run):
            snapshots.append((json.loads(json.dumps(run)), len(self.demo.journal)))

        run = self.app(trace_sink=sink).start(
            "p", "s", "m", "Check pet", ObserverModel()
        )
        before = [r for r, n in snapshots if n == 0 and r["expectations"]]
        self.assertTrue(before)
        self.assertEqual(before[0]["expectations"][0]["source"], "CONTRACT")
        self.assertEqual(before[0]["expectations"], run["expectations"])
        self.assertEqual(run["exchanges"][0]["run_id"], run["run_id"])

    def test_repeated_inspection_stops_without_network(self):
        model = lambda p: reply("inspect_operation", {"operation_id": "getPet"})
        run = self.app().start("p", "s", "m", "Check pet", model)
        self.assertEqual(run["stop_reason"], "no_progress")
        self.assertEqual(self.demo.connections, 0)

    def test_request_limit_stops_second_execution(self):
        model = lambda p: reply("execute_operation", {"operation_id": "getPet"})
        run = self.app().start("p", "s", "m", "Check pet", model)
        self.assertEqual(run["stop_reason"], "request_limit")
        self.assertEqual(len(self.demo.journal), 1)

    def test_unknown_tool_and_url_have_bounded_correction(self):
        for name, args in [
            ("fetch_url", {"url": "http://example.com"}),
            ("execute_operation", {"operation_id": "getPet", "url": self.demo.target}),
            ("execute_operation", {"operation_id": "deletePet"}),
        ]:
            with self.subTest(name=name, args=args):
                run = self.app().start(
                    "p", "s", "m", "Check pet", lambda p: reply(name, args)
                )
                self.assertEqual(run["stop_reason"], "malformed_limit")
                self.assertEqual(self.demo.connections, 0)

    def test_malformed_call_can_be_corrected(self):
        good = ObserverModel()
        used = False

        def model(payload):
            nonlocal used
            if not used:
                used = True
                return {"choices": []}
            return good(payload)

        run = self.app().start("p", "s", "m", "Check pet", model)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["malformed_calls"], 1)

    def test_cancel_and_time_after_decision_prevent_http(self):
        for reason in ("cancelled", "time_limit"):
            event = threading.Event()
            now = [0.0]

            def model(payload):
                if reason == "cancelled":
                    event.set()
                else:
                    now[0] = 1000.0
                return reply("execute_operation", {"operation_id": "getPet"})

            run = self.app(clock=lambda: now[0]).start(
                "p", "s", "m", "Check pet", model, cancel=event
            )
            self.assertEqual(run["stop_reason"], reason)
            self.assertEqual(self.demo.connections, 0)

    def test_step_limit_prevents_next_model_call(self):
        run = self.app(limits=self.Limits(max_steps=1)).start(
            "p", "s", "m", "Check pet", ObserverModel()
        )
        self.assertEqual(run["stop_reason"], "step_limit")
        self.assertEqual(run["model_calls"], 1)

    def test_foreign_evidence_cannot_finish(self):
        run = self.app().start(
            "p",
            "s",
            "m",
            "Check pet",
            lambda p: reply(
                "finish_run", {"summary": "done", "evidence_ids": ["foreign"]}
            ),
        )
        self.assertEqual(run["stop_reason"], "malformed_limit")
        self.assertEqual(run["status"], "blocked")

    def test_secrets_are_absent_in_prompt_trace_and_exception(self):
        secret = "CANARY-TOKEN-123"
        self.demo.pet["name"] = secret
        payloads = []

        def model(payload):
            payloads.append(payload)
            if len(payloads) == 1:
                return reply("execute_operation", {"operation_id": "getPet"})
            raise ValueError(secret)

        run = self.app(secrets=(secret,)).start("p", "s", "m", "Check " + secret, model)
        self.assertNotIn(secret, json.dumps([run, payloads]))
        self.assertEqual(run["stop_reason"], "model_error")

    def test_sink_failure_stops_before_http_and_duplicate_does_not_retry(self):
        def sink(run):
            if run["expectations"]:
                raise OSError("disk error")

        app = self.app(trace_sink=sink)
        run = app.start("p", "s", "m", "Check pet", ObserverModel())
        self.assertEqual(run["stop_reason"], "trace_error")
        app.start("p", "s", "m", "Check pet", ObserverModel())
        self.assertEqual(self.demo.connections, 0)

    def test_invalid_limits_and_catalog_are_rejected_before_network(self):
        for kwargs in (
            {"max_steps": True},
            {"max_seconds": float("nan")},
            {"max_requests": 2},
            {"malformed_retries": -1},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.Limits(**kwargs)
        document = json.loads((ROOT / "tests/fixtures/demo_openapi.json").read_text())
        document["paths"]["/pets/1"]["get"]["parameters"] = [
            {"name": "api-key", "in": "header", "schema": {"type": "string"}}
        ]
        with self.assertRaises(ValueError):
            self.StartAgentRun(self.demo.target, parse_document(document))
        self.assertEqual(self.demo.connections, 0)

    def test_prompt_budget_stops_before_model(self):
        run = self.app(limits=self.Limits(max_prompt_bytes=1)).start(
            "p", "s", "m", "Check pet", ObserverModel()
        )
        self.assertEqual(run["stop_reason"], "prompt_limit")
        self.assertEqual(run["model_calls"], 0)

    def test_concurrent_duplicate_executes_once_and_foreign_scope_is_denied(self):
        app = self.app()
        results = []

        def start():
            results.append(app.start("p", "s", "m", "Check pet", ObserverModel()))

        threads = [threading.Thread(target=start) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(self.demo.journal), 1)
        with self.assertRaises(ValueError):
            app.start("foreign", "s", "m", "Check pet", ObserverModel())

    def test_duplicate_snapshot_isolated_and_tools_stable(self):
        payloads = []
        observer = ObserverModel()

        def model(payload):
            payloads.append(payload)
            return observer(payload)

        app = self.app()
        run = app.start("p", "s", "m", "Check pet", model)
        run["exchanges"].clear()
        again = app.start("p", "s", "m", "Check pet", model)
        self.assertEqual(len(again["exchanges"]), 1)
        self.assertEqual(payloads[0]["tools"], payloads[1]["tools"])
        self.assertEqual(payloads[1]["messages"][:2], payloads[0]["messages"])

    def test_cancel_during_http_prevents_following_model_call(self):
        event = threading.Event()
        calls = []

        def model(payload):
            calls.append(payload)
            return reply("execute_operation", {"operation_id": "getPet"})

        def sink(run):
            if run["exchanges"]:
                event.set()

        run = self.app(trace_sink=sink).start(
            "p", "s", "m", "Check pet", model, cancel=event
        )
        self.assertEqual(run["stop_reason"], "cancelled")
        self.assertEqual(len(calls), 1)

    def test_malformed_nested_response_stops_safely(self):
        response = {"choices": []}
        response["recursive"] = response
        run = self.app().start("p", "s", "m", "Check pet", lambda p: response)
        self.assertEqual(run["stop_reason"], "malformed_limit")

    def test_run_records_immutable_model_and_contract_configuration(self):
        run = self.app().start("p", "s", "m", "Check pet", ObserverModel())
        self.assertEqual(run["model"]["name"], "MiniCPM5-2B-Q4_K_M")
        self.assertEqual(run["model"]["sampling"]["temperature"], 0.0)
        self.assertEqual(run["contract"]["profile_version"], "rest-json-v1")
        self.assertEqual(run["spec_hash"], self.catalog.spec_hash)


if __name__ == "__main__":
    unittest.main()

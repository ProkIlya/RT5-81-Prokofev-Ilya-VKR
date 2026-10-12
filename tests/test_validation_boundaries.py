"""S05: неполное тело и подмена сведений не становятся доказанным результатом."""

import json, sys, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]
from api_agent.agent import StartAgentRun
from api_agent.agent.runtime import tools
from agent_support import ObserverModel, count_test_tokens
from test_response_validation import catalog
from test_transport_deadlines import serve_raw


class ValidationBoundaryTests(unittest.TestCase):
    def test_incomplete_body_is_not_missing_required_finding(self):
        body = b'{"id":1}'
        port, thread = serve_raw(
            [
                (
                    b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: 40\r\n\r\n"
                    + body,
                    0,
                )
            ]
        )
        run = StartAgentRun(
            "http://127.0.0.1:" + str(port), catalog(), token_counter=count_test_tokens
        ).start("p", "s", "m", "Check pet", ObserverModel())
        thread.join(3)
        self.assertEqual(run["validation"]["contract_verdict"], "not_checked")
        self.assertEqual(run["validation"]["confirmed_contract_violations"], 0)
        self.assertEqual(run["status"], "partial")
        first = run["steps"][0]["observation"]
        self.assertTrue(first["truncated"])
        self.assertEqual(first["status"], "error")

    def test_goal_and_expectation_snapshot_survive_caller_mutation(self):
        from demo_api.server import DemoServer

        conditions = [
            {
                "source": "USER_DEFINED",
                "intent": "positive",
                "predicate": "json_value_equals",
                "parameters": {"path": "/name", "value": "Milk"},
                "basis": {"message_id": "m"},
                "preconditions": "verified",
            }
        ]
        observer = ObserverModel()

        def model(payload):
            conditions[0]["parameters"]["value"] = "Murka"
            return observer(payload)

        with DemoServer() as demo:
            run = StartAgentRun(
                demo.target, catalog(), token_counter=count_test_tokens
            ).start("p", "s", "m", "Check pet", model, conditions=conditions)
        self.assertEqual(run["expectations"][0]["parameters"]["value"], "Milk")
        self.assertEqual(run["validation"]["intent_verdict"], "fail")

    def test_substituted_validation_evidence_does_not_finish(self):
        from demo_api.server import DemoServer
        from api_agent.validation import validate_exchange

        def forged(*args):
            result = validate_exchange(*args)
            result["exchange_id"] = "foreign"
            return result

        with DemoServer() as demo, patch(
            "api_agent.agent.runtime.validate_exchange", side_effect=forged
        ):
            run = StartAgentRun(
                demo.target, catalog(), token_counter=count_test_tokens
            ).start("p", "s", "m", "Check pet", ObserverModel())
        self.assertNotEqual(run["status"], "completed")

    def test_closed_tools_define_matching_string_and_array_bounds(self):
        schema = next(
            t["function"]["parameters"]
            for t in tools()
            if t["function"]["name"] == "finish_run"
        )
        self.assertEqual(schema["properties"]["summary"].get("maxLength"), 2000)
        self.assertEqual(
            schema["properties"]["evidence_ids"]["items"].get("maxLength"), 200
        )


class MediaIndependenceTests(unittest.TestCase):
    def test_json_condition_is_independent_from_wrong_content_type(self):
        body = b'{"id":1,"name":"Murka"}'
        port, thread = serve_raw(
            [
                (
                    b"HTTP/1.0 200 OK\r\nContent-Type: text/plain\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body,
                    0,
                )
            ]
        )
        condition = {
            "source": "USER_DEFINED",
            "intent": "positive",
            "predicate": "json_value_equals",
            "parameters": {"path": "/name", "value": "Murka"},
            "basis": {"message_id": "m"},
            "preconditions": "verified",
        }
        run = StartAgentRun(
            "http://127.0.0.1:" + str(port), catalog(), token_counter=count_test_tokens
        ).start("p", "s", "m", "Check pet", ObserverModel(), conditions=[condition])
        thread.join(3)
        self.assertEqual(run["validation"]["intent_verdict"], "pass")
        self.assertEqual(run["validation"]["contract_verdict"], "mismatch")
        self.assertTrue(
            any(i["code"] == "content_type" for i in run["validation"]["issues"])
        )


class FinalizerTests(unittest.TestCase):
    def test_finish_snapshot_failure_is_recorded_as_failure_in_journal(self):
        from demo_api.server import DemoServer

        def sink(run):
            if run["stop_reason"] == "finished":
                raise OSError("CANARY")

        with DemoServer() as demo:
            run = StartAgentRun(
                demo.target,
                catalog(),
                token_counter=count_test_tokens,
                trace_sink=sink,
                secrets=("CANARY",),
            ).start("p", "s", "m", "Check pet", ObserverModel())
        self.assertEqual(run["status"], "failed")
        finish = next(e for e in reversed(run["events"]) if e["kind"] == "run_finished")
        self.assertEqual(finish["details"]["status"], "failed")
        self.assertEqual(run["report"]["execution_status"], "failed")
        self.assertNotIn("CANARY", json.dumps(run))


class GoalScopeTests(unittest.TestCase):
    def test_arbitrary_goal_is_not_silently_replaced_with_contract_check(self):
        from demo_api.server import DemoServer

        with DemoServer() as demo:
            run = StartAgentRun(
                demo.target, catalog(), token_counter=count_test_tokens
            ).start(
                "p",
                "s",
                "m",
                "Add three products and submit the order",
                ObserverModel(),
            )
            self.assertEqual(demo.journal, [])
        self.assertEqual(run["stop_reason"], "clarification_required")
        self.assertEqual(run["model_calls"], 0)

    def test_ambiguous_correctness_requires_condition_before_http(self):
        from demo_api.server import DemoServer

        with DemoServer() as demo:
            run = StartAgentRun(
                demo.target, catalog(), token_counter=count_test_tokens
            ).start("p", "s", "m", "Check that the pet is correct", ObserverModel())
            self.assertEqual(demo.journal, [])
        self.assertEqual(run["stop_reason"], "clarification_required")


class StrictJsonTests(unittest.TestCase):
    def test_nan_literal_is_invalid_json_not_supported_schema_value(self):
        body = b'{"id":NaN,"name":"Murka"}'
        port, thread = serve_raw(
            [
                (
                    b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode()
                    + b"\r\n\r\n"
                    + body,
                    0,
                )
            ]
        )
        run = StartAgentRun(
            "http://127.0.0.1:" + str(port), catalog(), token_counter=count_test_tokens
        ).start("p", "s", "m", "Check pet", ObserverModel())
        thread.join(3)
        self.assertEqual(run["exchanges"][0]["outcome"], "invalid_json")
        self.assertEqual(run["validation"]["contract_verdict"], "mismatch")
        self.assertTrue(any(i["code"] == "json" for i in run["validation"]["issues"]))

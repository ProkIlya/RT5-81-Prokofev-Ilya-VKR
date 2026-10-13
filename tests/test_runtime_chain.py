"""Сквозные проверки S06a через существующий StartAgentRun и настоящий HTTP."""

import importlib.util, json, sys, unittest, http.client
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from demo_api.server import DemoServer
from api_agent.agent import StartAgentRun
from api_agent.openapi import parse_document
from agent_support import reply, count_test_tokens


def contract():
    obj = {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
        "required": ["id", "name"],
    }
    body = {
        "type": "object",
        "properties": {"name": {"type": "string", "enum": ["A", "B"]}},
        "required": ["name"],
        "additionalProperties": False,
    }

    def response(schema):
        return {
            "description": "ok",
            "content": {"application/json": {"schema": schema}},
        }

    return parse_document(
        {
            "openapi": "3.0.3",
            "info": {"title": "Orders", "version": "1"},
            "paths": {
                "/orders": {
                    "post": {
                        "operationId": "createOrder",
                        "requestBody": {
                            "required": True,
                            "content": {"application/json": {"schema": body}},
                        },
                        "responses": {"201": response(obj)},
                    }
                },
                "/orders/{order_id}": {
                    "get": {
                        "operationId": "readOrder",
                        "parameters": [
                            {
                                "name": "order_id",
                                "in": "path",
                                "required": True,
                                "schema": {"type": "integer"},
                            }
                        ],
                        "responses": {"200": response(obj)},
                    }
                },
            },
        }
    )


class CreateReadModel:
    def __init__(self, invented=False, two=False):
        self.invented, self.two = invented, two

    def __call__(self, payload):
        obs = [
            json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"
        ]
        calls = [m for m in payload["messages"] if m["role"] == "assistant"]
        created = [o for o in obs if o.get("data", {}).get("runtime_values")]
        if not created:
            return reply(
                "execute_operation",
                {"operation_id": "createOrder", "body": {"name": "A"}},
            )
        if self.two and len(created) == 1 and len(calls) == 1:
            return reply(
                "execute_operation",
                {"operation_id": "createOrder", "body": {"name": "B"}},
            )
        if len(calls) == (2 if self.two else 1):
            ref = created[0]["data"]["runtime_values"][0]["ref"]
            return reply(
                "execute_read",
                {
                    "operation_id": "readOrder",
                    "value_ref": "invented" if self.invented else ref,
                },
            )
        ids = [
            o["data"]["exchange_id"]
            for o in obs
            if o.get("data", {}).get("exchange_id")
        ]
        return reply("finish_run", {"summary": "Observed chain", "evidence_ids": ids})


class RuntimeChainTests(unittest.TestCase):
    def app(self, demo):
        self.assertIsNotNone(
            importlib.util.find_spec("api_agent.agent.chains"),
            "Профиль S06a ещё не реализован",
        )
        from api_agent.agent.chains import CreateRead

        return StartAgentRun(
            demo.target,
            contract(),
            workflow=CreateRead(),
            token_counter=count_test_tokens,
        )

    def test_create_read_uses_actual_id(self):
        with DemoServer() as demo:
            run = self.app(demo).start(
                "p", "s", "m", "Create and read order", CreateReadModel()
            )
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["requests"], 2)
            self.assertEqual(run["chain_verdict"], "pass")
            self.assertEqual(
                demo.journal[1]["path"], "/orders/" + str(demo.journal[0]["created_id"])
            )

    def test_second_create_does_not_replace_first_reference(self):
        with DemoServer() as demo:
            run = self.app(demo).start(
                "p", "s", "m", "Create and read order", CreateReadModel(two=True)
            )
            self.assertEqual(run["status"], "completed")
            self.assertEqual(
                demo.journal[2]["path"], "/orders/" + str(demo.journal[0]["created_id"])
            )

    def test_invented_reference_never_reaches_server(self):
        with DemoServer() as demo:
            run = self.app(demo).start(
                "p", "s", "m", "Create and read order", CreateReadModel(invented=True)
            )
            self.assertNotEqual(run["status"], "completed")
            self.assertEqual(len(demo.journal), 1)

    def test_correct_schema_wrong_id_fails_chain(self):
        with DemoServer() as demo:
            demo.mode = "state_id_mismatch"
            run = self.app(demo).start(
                "p", "s", "m", "Create and read order", CreateReadModel()
            )
            self.assertEqual(run["chain_verdict"], "fail")
            self.assertEqual(run["report"]["contract_verdict"], "pass")
            self.assertEqual(run["report"]["intent_verdict"], "fail")

    def test_unknown_goal_is_not_replaced_by_chain(self):
        with DemoServer() as demo:
            run = self.app(demo).start(
                "p", "s", "m", "Delete everything", CreateReadModel()
            )
            self.assertEqual(run["stop_reason"], "clarification_required")
            self.assertNotIn("getPet", run["report"]["summary"])
            self.assertEqual(demo.connections, 0)

    def test_explicit_conditions_are_not_silently_ignored(self):
        with DemoServer() as demo:
            run = self.app(demo).start(
                "p", "s", "m", "Create and read order", CreateReadModel(), conditions=[]
            )
            self.assertEqual(run["stop_reason"], "invalid_expectation")
            self.assertEqual(demo.connections, 0)

    def test_timeout_after_commit_does_not_repeat_post(self):
        from unittest.mock import patch
        from api_agent.agent.chains import ChainSession

        class FastDeadlineSession(ChainSession):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.timeout = 0.1

        class RepeatingModel:
            def __call__(self, payload):
                return reply(
                    "execute_operation",
                    {"operation_id": "createOrder", "body": {"name": "A"}},
                )

        with DemoServer() as demo:
            demo.mode = "timeout_after_commit"
            with patch("api_agent.agent.chains.ChainSession", FastDeadlineSession):
                run = self.app(demo).start(
                    "p", "s", "m", "Create and read order", RepeatingModel()
                )
            self.assertNotEqual(run["status"], "completed")
            self.assertEqual(len(demo.orders), 1)
            self.assertEqual(len(demo.journal), 1)
            self.assertEqual(run["requests"], 1)

    def test_dispatch_rejects_parameters_not_in_published_schema(self):
        from api_agent.agent.chains import CreateRead

        with self.assertRaises(ValueError):
            CreateRead().decision(
                "execute_operation",
                {"operation_id": "readOrder", "path": {"order_id": {"ref": "any"}}},
            )

    def test_nonfinite_budget_is_rejected_before_run(self):
        from api_agent.agent.chains import ChainLimits

        with self.assertRaises(ValueError):
            ChainLimits(max_seconds=float("inf"))
        with self.assertRaises(ValueError):
            ChainLimits(max_output_tokens=0)

    def test_trace_records_the_actual_nonsensitive_request_body(self):
        with DemoServer() as demo:
            run = self.app(demo).start(
                "p", "s", "body-trace", "Create and read order", CreateReadModel()
            )
            self.assertEqual(run["exchanges"][0]["request"].get("json"), {"name": "A"})

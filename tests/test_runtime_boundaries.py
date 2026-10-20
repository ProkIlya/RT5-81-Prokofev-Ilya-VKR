"""Регрессии границ секретов, опубликованных аргументов и исправления finish.

Проверки проходят через настоящий Demo HTTP и публичные sinks. Невалидный
секрет не должен извлекаться, даже если его публичное представление скрыто.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from demo_api.server import DemoServer
from api_agent.agent import StartAgentRun
from api_agent.agent.chains import CreateRead, AuthRead
from api_agent.state import RunState
from api_agent.validation import schema_issues
from agent_support import reply, count_test_tokens
from test_runtime_auth import auth_contract
from test_runtime_chain import contract, CreateReadModel


class SecretBoundaryTests(unittest.TestCase):
    def test_inspect_preserves_public_token_type_schema(self):
        state = RunState(
            "p", "r", {"loginUser": {"/token": {"type": "string", "secret": True}}}
        )
        operation = {
            "operation_id": "loginUser",
            **auth_contract().operations["loginUser"],
        }
        # Одинаковая форма сама по себе не даёт metadata authority.
        untrusted = state.redact({'operation': operation})
        self.assertEqual(untrusted['operation']['responses']['200']['content']['application/json']['schema']['properties']['token'], '[REDACTED]')
        # Как в runtime inspect: приложение явно регистрирует exact DTO,
        # полученный от pinned каталога, до публикации schema.
        state.trust_public_catalog(operation)
        public = state.redact({"operation": operation})
        schema = public["operation"]["responses"]["200"]["content"]["application/json"][
            "schema"
        ]
        self.assertEqual(schema["properties"]["token"], {"type": "string"})

    def test_static_secret_matching_field_name_cannot_bypass_path_mask(self):
        with DemoServer() as demo:
            app = StartAgentRun(
                demo.target,
                auth_contract(),
                workflow=AuthRead(),
                token_counter=count_test_tokens,
                secrets=("token",),
            )
            state = RunState(
                "p", "r", {"loginUser": {"/token": {"type": "string", "secret": True}}}
            )
            exchange = {
                "id": "E1",
                "run_id": "r",
                "operation_id": "loginUser",
                "response": {"json": {"token": 98765432123456789}},
            }
            state.protect(exchange)
            app._active_state = state
            public = app.safe(exchange)
            self.assertNotIn("98765432123456789", json.dumps(public))
            self.assertEqual(public["response"]["json"]["[REDACTED]"], "[REDACTED]")

    def test_entire_secret_path_and_diagnostic_are_hidden_for_every_json_type(self):
        for token in (
            98765432123456789,
            9876543.125,
            False,
            None,
            {"key-canary-8132": [17, {"inner": "value-canary-8132"}]},
        ):
            with self.subTest(token_type=type(token).__name__):
                state = RunState(
                    "p",
                    "r",
                    {"loginUser": {"/token": {"type": "string", "secret": True}}},
                )
                exchange = {
                    "id": "E1",
                    "run_id": "r",
                    "operation_id": "loginUser",
                    "response": {"json": {"id": 17, "token": token}},
                }
                state.protect(exchange)
                with self.assertRaises(ValueError):
                    state.remember(exchange, "/token")
                public = state.redact(
                    {"exchange": exchange, "issue": {"path": "/token", "actual": token}}
                )
                self.assertEqual(
                    public["exchange"]["response"]["json"]["token"], "[REDACTED]"
                )
                self.assertEqual(public["issue"]["actual"], "[REDACTED]")
                self.assertEqual(public["exchange"]["response"]["json"]["id"], 17)

    def test_malformed_secret_cannot_escape_any_public_channel(self):
        for token, marker in (
            (98765432123456789, "98765432123456789"),
            ({"key-canary-9246": {"secret": 98765432123456789}}, "key-canary-9246"),
        ):
            with self.subTest(marker=marker), DemoServer() as demo:
                handler = demo.server.RequestHandlerClass
                original = handler.send_json

                def send(this, status, data):
                    if this.path == "/users/login" and status == 200:
                        data = {**data, "token": token}
                    return original(this, status, data)

                payloads, traces, events = [], [], []

                def model(payload):
                    payloads.append(payload)
                    return reply("execute_operation", {"operation_id": "loginUser"})

                app = StartAgentRun(
                    demo.target,
                    auth_contract(),
                    workflow=AuthRead(),
                    token_counter=count_test_tokens,
                    trace_sink=lambda r: traces.append(r),
                    event_sink=lambda e: events.append(e),
                )
                with patch.object(handler, "send_json", send):
                    run = app.start("p", "s", "m", "Login and read profile", model)
                self.assertNotEqual(run["status"], "completed")
                self.assertEqual(run["requests"], 1)
                self.assertEqual(run["report"]["contract_verdict"], "mismatch")
                self.assertEqual(
                    run["exchanges"][0]["response"]["json"]["token"], "[REDACTED]"
                )
                for channel in (
                    payloads,
                    traces,
                    events,
                    run["history"],
                    run,
                    run["report"],
                ):
                    self.assertNotIn(marker, json.dumps(channel))
                    self.assertNotIn("98765432123456789", json.dumps(channel))


class ToolContractTests(unittest.TestCase):
    def test_runtime_uses_flat_exact_execution_tools_without_union(self):
        for workflow, catalog in (
            (CreateRead(), contract()),
            (AuthRead(), auth_contract()),
        ):
            registry = {
                t["function"]["name"]: t["function"]["parameters"]
                for t in workflow.registry(catalog)
            }
            self.assertEqual(
                set(registry),
                {
                    "execute_operation",
                    "execute_read",
                    "inspect_operation",
                    "finish_run",
                },
            )
            write = {"operation_id": workflow.create}
            read = {"operation_id": workflow.read}
            if type(workflow) is CreateRead:
                write["body"] = {"name": "A"}
                read["value_ref"] = "value-existing"
            else:
                read["credential_ref"] = "value-existing"
            for name, args in (("execute_operation", write), ("execute_read", read)):
                self.assertNotIn("oneOf", registry[name])
                self.assertFalse(schema_issues(args, registry[name]))
                workflow.decision(name, args)

    def test_operation_dependent_fields_are_rejected_by_published_schema(self):
        for workflow, catalog in (
            (CreateRead(), contract()),
            (AuthRead(), auth_contract()),
        ):
            schemas = {
                t["function"]["name"]: t["function"]["parameters"]
                for t in workflow.registry(catalog)
            }
            invalid = (
                [
                    ("execute_operation", {"operation_id": workflow.create}),
                    ("execute_read", {"operation_id": workflow.read}),
                    (
                        "execute_operation",
                        {
                            "operation_id": workflow.create,
                            "body": {"name": "A"},
                            "value_ref": "extra",
                        },
                    ),
                    (
                        "execute_read",
                        {
                            "operation_id": workflow.read,
                            "value_ref": "ref",
                            "body": {"name": "A"},
                        },
                    ),
                ]
                if type(workflow) is CreateRead
                else [
                    ("execute_read", {"operation_id": workflow.read}),
                    (
                        "execute_operation",
                        {"operation_id": workflow.create, "credential_ref": "extra"},
                    ),
                ]
            )
            for name, args in invalid:
                with self.subTest(profile=type(workflow).__name__, args=args):
                    self.assertTrue(schema_issues(args, schemas[name]))
                    with self.assertRaises(ValueError):
                        workflow.decision(name, args)

    def test_finish_schema_publishes_summary_and_evidence_limits(self):
        for workflow, catalog in (
            (CreateRead(), contract()),
            (AuthRead(), auth_contract()),
        ):
            schema = workflow.registry(catalog)[-1]["function"]["parameters"]
            for summary, ids in (
                ("", ["E1"]),
                ("  \n", ["E1"]),
                ("x" * 2001, ["E1"]),
                ("ok", []),
                ("ok", ["E"] * 5),
            ):
                with self.subTest(
                    profile=type(workflow).__name__, length=len(summary), ids=len(ids)
                ):
                    self.assertTrue(
                        schema_issues({"summary": summary, "evidence_ids": ids}, schema)
                    )

    def test_dispatcher_checks_published_types_before_side_effect(self):
        with DemoServer() as demo:
            app = StartAgentRun(
                demo.target,
                contract(),
                workflow=CreateRead(),
                token_counter=count_test_tokens,
            )
            for name, args in (
                ("execute_read", {"operation_id": "readOrder", "value_ref": 17}),
                (
                    "execute_operation",
                    {"operation_id": "createOrder", "body": {"name": 17}},
                ),
            ):
                with self.assertRaises(ValueError):
                    app._decision(reply(name, args))
            self.assertEqual(demo.connections, 0)


class FinishRecoveryTests(unittest.TestCase):
    def test_empty_arguments_receive_required_fields_without_new_http(self):
        with DemoServer() as demo:
            base = CreateReadModel()
            calls = []

            def model(payload):
                calls.append(payload)
                if len(calls) == 1:
                    return reply("execute_operation", {})
                if len(calls) == 2:
                    feedback = [
                        m["content"] for m in payload["messages"] if m["role"] == "user"
                    ][-1]
                    self.assertIn("operation_id", feedback)
                    self.assertIn("body", feedback)
                return base(payload)

            app = StartAgentRun(
                demo.target,
                contract(),
                workflow=CreateRead(),
                token_counter=count_test_tokens,
            )
            run = app.start("p", "s", "m", "Create and read order", model)
            self.assertEqual(run["status"], "completed")
            hint = run["steps"][0]["observation"]["error"]["hint"]
            self.assertIn("operation_id", hint)
            self.assertIn("body", hint)
            self.assertEqual(run["malformed_calls"], 1)
            self.assertEqual(len(demo.journal), 2)

    def test_incomplete_finish_gets_exact_ids_and_repairs_without_http(self):
        with DemoServer() as demo:
            base = CreateReadModel()
            finishes, required = [], []

            def model(payload):
                answer = base(payload)
                call = answer["choices"][0]["message"]["tool_calls"][0]
                if call["function"]["name"] != "finish_run":
                    return answer
                args = json.loads(call["function"]["arguments"])
                finishes.append(args)
                if len(finishes) == 1:
                    args["evidence_ids"] = args["evidence_ids"][-1:]
                    return reply("finish_run", args)
                corrections = [
                    m["content"] for m in payload["messages"] if m["role"] == "user"
                ]
                self.assertIn(json.dumps(finishes[-1]["evidence_ids"]), corrections[-1])
                observations = [
                    json.loads(m["content"])
                    for m in payload["messages"]
                    if m["role"] == "tool"
                ]
                required.extend(observations[-1]["data"]["finish_evidence_ids"])
                return answer

            app = StartAgentRun(
                demo.target,
                contract(),
                workflow=CreateRead(),
                token_counter=count_test_tokens,
            )
            run = app.start("p", "s", "m", "Create and read order", model)
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["chain_verdict"], "pass")
            self.assertEqual(run["malformed_calls"], 1)
            self.assertEqual(required, [e["id"] for e in run["exchanges"]])
            self.assertEqual(len(demo.journal), 2)

    def test_persistently_incomplete_finish_never_passes(self):
        with DemoServer() as demo:
            base = CreateReadModel()

            def model(payload):
                answer = base(payload)
                call = answer["choices"][0]["message"]["tool_calls"][0]
                if call["function"]["name"] == "finish_run":
                    args = json.loads(call["function"]["arguments"])
                    return reply(
                        "finish_run",
                        {**args, "evidence_ids": args["evidence_ids"][-1:]},
                    )
                return answer

            app = StartAgentRun(
                demo.target,
                contract(),
                workflow=CreateRead(),
                token_counter=count_test_tokens,
            )
            run = app.start("p", "s", "m", "Create and read order", model)
            self.assertNotEqual(run["status"], "completed")
            self.assertEqual(run["stop_reason"], "malformed_limit")
            self.assertEqual(len(demo.journal), 2)

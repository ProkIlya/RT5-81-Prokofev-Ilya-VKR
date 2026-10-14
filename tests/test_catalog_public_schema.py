"""Exact публичные schema общего inspect не отменяют redaction HTTP values."""

import sys, json, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.policy import ProjectConfig
from api_agent.openapi import load_source
from api_agent.state import RunState
from agent_support import reply, count_test_tokens
from demo_api.server import DemoServer
from test_catalog_execution import status_condition


class PublicSchemaTests(unittest.TestCase):
    def test_common_inspect_preserves_token_declaration_in_trace_and_next_payload(self):
        with DemoServer() as server:
            cat = load_source(str(ROOT / "tests/fixtures/auth_openapi.json"))
            cfg = ProjectConfig.create(
                "p",
                server.target,
                cat,
                mode="TEST",
                resettable=True,
                operations={"loginUser": "C", "readProfile": "A"},
            )
            wf = CatalogWorkflow(
                cat,
                cfg,
                goal="Проверь схему",
                conditions={"loginUser": [status_condition(200)]},
                extraction={
                    "loginUser": {"/token": {"type": "string", "secret": True}}
                },
                auth={
                    "login_operation": "loginUser",
                    "token_path": "/token",
                    "body": {"login": "fixture", "password": "fixture-password"},
                },
            )
            payloads = []

            def model(p):
                payloads.append(p)
                return reply(
                    "inspect_operation",
                    {"operation_id": "loginUser", "section": "responses"},
                )

            r = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start("p", "s", "schema", wf.goal, model)
            schema = r["steps"][0]["observation"]["data"]["catalog_operation"][
                "responses"
            ]["200"]["content"]["application/json"]["schema"]
            self.assertEqual(schema["properties"]["token"], {"type": "string"})
            tool = json.loads(
                next(
                    m["content"] for m in payloads[1]["messages"] if m["role"] == "tool"
                )
            )
            self.assertEqual(
                tool["data"]["catalog_operation"]["responses"]["200"]["content"][
                    "application/json"
                ]["schema"]["properties"]["token"],
                {"type": "string"},
            )
            self.assertEqual(server.connections, 0)
            self.assertNotIn("fixture-password", json.dumps(r))

    def test_http_lookalike_catalog_envelope_does_not_disable_masking(self):
        state = RunState(
            "p", "r", {"loginUser": {"/token": {"type": "string", "secret": True}}}
        )
        fake = {
            "catalog_operation": {
                "operation_id": "loginUser",
                "responses": {"token": "canary"},
            }
        }
        safe = state.redact(fake)
        self.assertNotIn("canary", json.dumps(safe))

    def test_public_schema_after_literal_mask_retains_type_on_repeated_redaction(self):
        state = RunState(
            "p", "r", {"loginUser": {"/token": {"type": "string", "secret": True}}}
        )
        state.remember(
            {
                "run_id": "r",
                "id": "E",
                "operation_id": "loginUser",
                "response": {"json": {"token": "private-canary"}},
            },
            "/token",
        )
        schema = {
            "operation_id": "loginUser",
            "responses": {
                "200": {
                    "schema": {
                        "properties": {
                            "token": {"type": "string", "enum": ["private-canary"]}
                        }
                    }
                }
            },
        }
        state.trust_public_catalog(schema)
        public = state.redact({"catalog_operation": schema})
        public = state.redact(public)
        actual = public["catalog_operation"]["responses"]["200"]["schema"][
            "properties"
        ]["token"]
        self.assertEqual(actual, {"type": "string", "enum": ["[REDACTED]"]})
        self.assertNotIn("private-canary", json.dumps(public))

"""Секретные значения и авторизация: canary не виден модели и trace."""

import json, sys, unittest, importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.openapi import parse_document
from demo_api.server import DemoServer
from agent_support import reply, count_test_tokens

CANARY = "S06-secret-token-canary-2f37d926"


def auth_contract():
    def response(schema):
        return {
            "description": "ok",
            "content": {"application/json": {"schema": schema}},
        }

    login = {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "token": {"type": "string"}},
        "required": ["id", "token"],
    }
    profile = {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
        "required": ["id", "name"],
    }
    body = {
        "type": "object",
        "properties": {"login": {"type": "string"}, "password": {"type": "string"}},
        "required": ["login", "password"],
        "additionalProperties": False,
    }
    return parse_document(
        {
            "openapi": "3.0.3",
            "info": {"title": "Auth", "version": "1"},
            "components": {
                "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}
            },
            "paths": {
                "/users/login": {
                    "post": {
                        "operationId": "loginUser",
                        "requestBody": {
                            "required": True,
                            "content": {"application/json": {"schema": body}},
                        },
                        "responses": {"200": response(login)},
                    }
                },
                "/users/profile": {
                    "get": {
                        "operationId": "readProfile",
                        "security": [{"bearerAuth": []}],
                        "responses": {"200": response(profile)},
                    }
                },
            },
        }
    )


class AuthModel:
    def __init__(self, stolen=None):
        self.payloads = []
        self.stolen = stolen

    def __call__(self, payload):
        self.payloads.append(payload)
        obs = [
            json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"
        ]
        data = [o["data"] for o in obs if o.get("data")]
        if not data:
            return reply("execute_operation", {"operation_id": "loginUser"})
        refs = [v for d in data for v in d.get("runtime_values", []) if v.get("secret")]
        if len(data) == 1:
            return reply(
                "execute_read",
                {
                    "operation_id": "readProfile",
                    "credential_ref": self.stolen or refs[0]["ref"],
                },
            )
        return reply(
            "finish_run",
            {
                "summary": "Authenticated observation",
                "evidence_ids": [d["exchange_id"] for d in data],
            },
        )


class AuthStateTests(unittest.TestCase):
    def state(self):
        from api_agent.state import RunState

        return RunState(
            "p", "r", {"loginUser": {"/token": {"type": "string", "secret": True}}}
        )

    def test_secret_is_classified_by_program_and_not_public(self):
        state = self.state()
        exchange = {
            "id": "E1",
            "run_id": "r",
            "operation_id": "loginUser",
            "response": {"json": {"token": CANARY}},
        }
        try:
            record = state.remember(exchange, "/token")
        except ValueError as e:
            self.fail("Secret capture отсутствует: " + str(e))
        self.assertNotIn(CANARY, json.dumps(state.public()))
        self.assertNotIn("value", record)
        self.assertEqual(state.resolve(record["ref"], "string", secret=True), CANARY)
        with self.assertRaises(ValueError):
            state.resolve(record["ref"], "string")

    def test_invalid_secret_type_is_redacted_before_error(self):
        state = self.state()
        exchange = {
            "id": "E1",
            "run_id": "r",
            "operation_id": "loginUser",
            "response": {"json": {"token": [CANARY]}},
        }
        self.assertTrue(
            hasattr(state, "protect"),
            "Безопасная классификация ответа ещё не реализована",
        )
        state.protect(exchange)
        with self.assertRaises(ValueError):
            state.remember(exchange, "/token")
        self.assertNotIn(CANARY, json.dumps(state.redact(exchange)))


class AuthLoopTests(unittest.TestCase):
    def app(self, demo, traces):
        import api_agent.agent.chains as chains

        self.assertTrue(hasattr(chains, "AuthRead"), "S06b профиль ещё не реализован")
        return StartAgentRun(
            demo.target,
            auth_contract(),
            workflow=chains.AuthRead(password="fixture-password"),
            token_counter=count_test_tokens,
            trace_sink=lambda r: traces.append(r),
        )

    def test_real_header_without_token_in_model_or_history(self):
        with DemoServer() as demo:
            demo.auth_token = CANARY
            traces = []
            model = AuthModel()
            app = self.app(demo, traces)
            run = app.start("p", "s", "m", "Login and read profile", model)
            self.assertEqual(run["chain_verdict"], "pass")
            self.assertEqual(run["status"], "completed")
            self.assertNotIn(CANARY, json.dumps([model.payloads, traces, run]))
            self.assertTrue(
                all(
                    payload.get("parallel_tool_calls") is False
                    for payload in model.payloads
                )
            )
            self.assertNotIn(
                "fixture-password", json.dumps([model.payloads, traces, run])
            )
            self.assertTrue(demo.journal[-1]["auth_matched"])

    def test_foreign_credential_reference_does_not_send_profile(self):
        with DemoServer() as demo:
            traces = []
            run = self.app(demo, traces).start(
                "p", "s", "one", "Login and read profile", AuthModel()
            )
            ref = next(
                v["ref"]
                for v in run["steps"][0]["observation"]["data"]["runtime_values"]
                if v.get("secret")
            )
            demo.reset()
            run = self.app(demo, []).start(
                "p", "s", "two", "Login and read profile", AuthModel(stolen=ref)
            )
            self.assertNotEqual(run["status"], "completed")
            self.assertEqual(len(demo.journal), 1)

    def test_invalid_token_container_cannot_leak_or_authorize(self):
        with DemoServer() as demo:
            demo.auth_token = CANARY
            demo.mode = "invalid_token_type"
            traces = []
            model = AuthModel()
            run = self.app(demo, traces).start(
                "p", "s", "bad-type", "Login and read profile", model
            )
            self.assertNotEqual(run["status"], "completed")
            self.assertEqual(len(demo.journal), 1)
            self.assertNotIn(CANARY, json.dumps([model.payloads, traces, run]))

    def test_raw_token_cannot_replace_scoped_reference(self):
        with DemoServer() as demo:
            demo.auth_token = CANARY
            traces = []
            model = AuthModel(stolen=CANARY)
            run = self.app(demo, traces).start(
                "p", "s", "raw-token", "Login and read profile", model
            )
            self.assertNotEqual(run["status"], "completed")
            self.assertEqual(len(demo.journal), 1)
            self.assertNotIn(CANARY, json.dumps([traces, run]))

    def test_secret_literal_in_contract_is_rejected_before_model(self):
        from api_agent.agent.chains import AuthRead

        document = json.loads((ROOT / "tests/fixtures/auth_openapi.json").read_text())
        document["paths"]["/users/login"]["post"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["properties"]["token"]["enum"] = [CANARY]
        with DemoServer() as demo:
            with self.assertRaises(ValueError):
                StartAgentRun(
                    demo.target, parse_document(document), workflow=AuthRead()
                )
            self.assertEqual(demo.connections, 0)

    def test_undeclared_extraction_path_is_rejected_before_login(self):
        from api_agent.agent.chains import AuthRead

        with DemoServer() as demo:
            with self.assertRaises(ValueError):
                StartAgentRun(
                    demo.target,
                    auth_contract(),
                    workflow=AuthRead(id_path="/unknown/id"),
                )
            self.assertEqual(demo.connections, 0)

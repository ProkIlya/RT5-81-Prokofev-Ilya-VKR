"""Общий Bearer executor сохраняет приватность token во всех наблюдаемых выходах."""

import sys, json, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.policy import ProjectConfig
from api_agent.openapi import load_source
from demo_api.server import DemoServer
from agent_support import reply, count_test_tokens
from test_catalog_execution import status_condition


class AuthCatalogTests(unittest.TestCase):
    def test_bearer_ref_is_private_and_profile_uses_issued_token(self):
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
            goal = "Войди и проверь профиль"
            wf = CatalogWorkflow(
                cat,
                cfg,
                goal=goal,
                conditions={
                    "loginUser": [status_condition(200)],
                    "readProfile": [status_condition(200)],
                },
                extraction={
                    "loginUser": {"/token": {"type": "string", "secret": True}}
                },
                auth={
                    "login_operation": "loginUser",
                    "token_path": "/token",
                    "body": {"login": "fixture", "password": "fixture-password"},
                },
            )

            def model(payload):
                self.assertNotIn(server.auth_token, json.dumps(payload))
                self.assertNotIn("fixture-password", json.dumps(payload))
                obs = [
                    json.loads(h["content"])["data"]
                    for h in payload["messages"]
                    if h["role"] == "tool"
                ]
                if not obs:
                    return reply("execute_operation", {"operation_id": "loginUser"})
                if len(obs) == 1:
                    return reply(
                        "execute_operation",
                        {
                            "operation_id": "readProfile",
                            "credential_ref": obs[-1]["runtime_values"][0]["ref"],
                        },
                    )
                return reply(
                    "finish_run",
                    {
                        "summary": "Checked",
                        "evidence_ids": obs[-1]["finish_evidence_ids"],
                    },
                )

            run = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start("p", "s", "m", goal, model)
            self.assertEqual(run["status"], "completed")
            self.assertEqual(len(server.journal), 2)
            self.assertNotIn(server.auth_token, json.dumps(run))
            self.assertNotIn("fixture-password", json.dumps(run))
            self.assertTrue(run["exchanges"][0]["request"]["credential_body_omitted"])


if __name__ == "__main__":
    unittest.main()

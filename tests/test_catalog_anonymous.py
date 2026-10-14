"""Anonymous negative-auth — явное намерение приложения, не отсутствие readiness."""

import sys, json, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.policy import ProjectConfig
from api_agent.openapi import load_source
from demo_api.server import DemoServer
from agent_support import count_test_tokens, reply


class AnonymousTests(unittest.TestCase):
    def test_anonymous_negative_really_sends_profile_without_login(self):
        with DemoServer() as server:
            cat = load_source(str(ROOT / "tests/fixtures/auth_openapi.json"))
            cfg = ProjectConfig.create(
                "p",
                server.target,
                cat,
                mode="READ_ONLY",
                resettable=True,
                operations={"readProfile": "A"},
            )
            goal = "Проверь профиль без входа: ожидается отказ"
            try:
                wf = CatalogWorkflow(
                    cat,
                    cfg,
                    goal=goal,
                    conditions={
                        "readProfile": [
                            {
                                "predicate": "status_in",
                                "parameters": {"values": [401]},
                                "intent": "negative",
                                "preconditions": "verified",
                            }
                        ]
                    },
                    anonymous_operations=["readProfile"],
                )
            except TypeError:
                self.fail("Отсутствует явный anonymous negative-auth")
            self.assertEqual(wf.inspect(cat, "readProfile")["input_readiness"], "ready")

            def model(payload):
                obs = [
                    json.loads(h["content"])
                    for h in payload["messages"]
                    if h["role"] == "tool"
                ]
                if not obs:
                    return reply("execute_operation", {"operation_id": "readProfile"})
                return reply(
                    "finish_run",
                    {
                        "summary": "Expected rejection",
                        "evidence_ids": obs[-1]["data"]["finish_evidence_ids"],
                    },
                )

            run = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start("p", "s", "m", goal, model)
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["exchanges"][0]["response"]["status"], 401)
            self.assertEqual(
                server.journal,
                [{"method": "GET", "path": "/users/profile", "auth_matched": False}],
            )


if __name__ == "__main__":
    unittest.main()

"""Нет HTTP evidence: correction должна требовать действие, не ложный finish."""

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
from test_catalog_execution import status_condition


class FinishHintsTests(unittest.TestCase):
    def test_finish_without_http_is_corrected_towards_execute(self):
        with DemoServer() as server:
            cat = load_source(str(ROOT / "tests/fixtures/demo_openapi.json"))
            cfg = ProjectConfig.create(
                "p",
                server.target,
                cat,
                mode="READ_ONLY",
                resettable=True,
                operations={"getPet": "A"},
            )
            wf = CatalogWorkflow(
                cat,
                cfg,
                goal="Check pet",
                conditions={"getPet": [status_condition(200)]},
            )
            calls = []

            def model(payload):
                calls.append(payload)
                if len(calls) == 1:
                    return reply(
                        "finish_run", {"summary": "Claim", "evidence_ids": ["invented"]}
                    )
                if len(calls) == 2:
                    self.assertIn(
                        "execute_operation", payload["messages"][-1]["content"]
                    )
                    self.assertNotIn(
                        "Do not send another HTTP", payload["messages"][-1]["content"]
                    )
                    return reply("execute_operation", {"operation_id": "getPet"})
                obs = [
                    json.loads(h["content"])
                    for h in payload["messages"]
                    if h["role"] == "tool"
                ][-1]["data"]
                return reply(
                    "finish_run",
                    {"summary": "Checked", "evidence_ids": obs["finish_evidence_ids"]},
                )

            run = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start("p", "s", "m", wf.goal, model)
            self.assertEqual(run["status"], "completed")
            self.assertEqual(len(server.journal), 1)

    def test_completed_checks_offer_finish_but_model_must_call_it(self):
        with DemoServer() as server:
            cat = load_source(str(ROOT / "tests/fixtures/demo_openapi.json"))
            cfg = ProjectConfig.create(
                "p",
                server.target,
                cat,
                mode="READ_ONLY",
                resettable=True,
                operations={"getPet": "A"},
            )
            wf = CatalogWorkflow(
                cat,
                cfg,
                goal="Check pet",
                conditions={"getPet": [status_condition(200)]},
            )
            calls = []

            def model(payload):
                calls.append(payload)
                if len(calls) == 1:
                    return reply("execute_operation", {"operation_id": "getPet"})
                observation = json.loads(payload["messages"][-1]["content"])["data"]
                self.assertTrue(observation["checks_complete"])
                self.assertEqual(payload["tool_choice"]["function"]["name"], "finish_run")
                return reply(
                    "finish_run",
                    {
                        "summary": "Checked",
                        "evidence_ids": observation["finish_evidence_ids"],
                    },
                )

            run = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start("p", "s", "done", wf.goal, model)
            self.assertEqual(run["status"], "completed", run["stop_reason"])
            self.assertEqual(len(calls), 2)
            self.assertEqual(server.journal, [{"method": "GET", "path": "/pets/1"}])
            self.assertEqual(run["steps"][-1]["action"]["name"], "finish_run")


if __name__ == "__main__":
    unittest.main()

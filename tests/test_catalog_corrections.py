"""Две коррекции формы ограничены общим бюджетом и не создают HTTP-повторов."""

import sys
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from test_catalog_execution import catalog_variant, status_condition
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.agent import StartAgentRun
from api_agent.policy import ProjectConfig
from demo_api.server import DemoServer
from agent_support import count_test_tokens, reply


class CorrectionTests(unittest.TestCase):
    def test_two_invalid_forms_then_valid_action_finish_without_duplicate_http(self):
        with DemoServer() as server:
            cat = catalog_variant("r")
            cfg = ProjectConfig.create(
                "p",
                server.target,
                cat,
                mode="TEST",
                resettable=True,
                operations={"rcreateOrder": "C"},
            )
            wf = CatalogWorkflow(
                cat,
                cfg,
                goal="Создай A",
                conditions={"rcreateOrder": [status_condition(201)]},
            )
            calls = []

            def model(payload):
                calls.append(payload)
                if len(calls) <= 2:
                    return reply("execute_operation", {"operation_id": "rcreateOrder"})
                if len(calls) == 3:
                    return reply(
                        "execute_operation",
                        {"operation_id": "rcreateOrder", "body": {"name": "A"}},
                    )
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
            ).start("p", "s", "m", "Создай A", model)
            self.assertEqual(run["status"], "completed")
            self.assertEqual(run["malformed_calls"], 2)
            self.assertEqual(len(server.journal), 1)
            self.assertEqual(run["model_calls"], 4)


if __name__ == "__main__":
    unittest.main()

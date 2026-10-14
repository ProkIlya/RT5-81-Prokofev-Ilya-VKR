"""Policy проверяется по реальному сетевому эффекту, а не тексту правил."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from demo_api.server import DemoServer
from api_agent.agent import StartAgentRun
from api_agent.agent.chains import CreateRead
from test_runtime_chain import contract, CreateReadModel
from agent_support import count_test_tokens


class PolicyTests(unittest.TestCase):
    def policy(self, server, mode="TEST", risk="C", approve=None):
        self.assertIsNotNone(
            importlib.util.find_spec("api_agent.policy"),
            "Отсутствует программная policy S07",
        )
        from api_agent.policy import ProjectConfig, PolicyWorkflow

        catalog = contract()
        config = ProjectConfig.create(
            "p",
            server.target,
            catalog,
            mode=mode,
            resettable=True,
            operations={"createOrder": risk, "readOrder": "A"},
        )
        return catalog, config, PolicyWorkflow(CreateRead(), config, approval=approve)

    def run_case(self, server, mode, risk="C", approve=None):
        catalog, config, workflow = self.policy(server, mode, risk, approve)
        app = StartAgentRun(
            server.target, catalog, workflow=workflow, token_counter=count_test_tokens
        )
        return app.start("p", "s", "m", "Create and read order", CreateReadModel())

    def test_read_only_write_has_zero_connections(self):
        with DemoServer() as server:
            run = self.run_case(server, "READ_ONLY")
            self.assertNotEqual(run["status"], "completed")
            self.assertEqual(server.connections, 0)
            self.assertEqual(server.journal, [])

    def test_test_mode_executes_allowed_change(self):
        with DemoServer() as server:
            run = self.run_case(server, "TEST")
            self.assertEqual(run["status"], "completed")
            self.assertEqual([r["method"] for r in server.journal], ["POST", "GET"])
            self.assertEqual(run["policy_decisions"][0]["decision"], "ALLOW")

    def test_safe_requires_action_bound_approval(self):
        for approved in (False, True):
            with self.subTest(approved=approved), DemoServer() as server:
                seen = []

                def callback(proposal):
                    seen.append(proposal)
                    self.assertEqual(server.connections, 0)
                    return {
                        "action_hash": proposal["action_hash"],
                        "config_version": proposal["config_version"],
                        "approved": approved,
                    }

                run = self.run_case(server, "SAFE", approve=callback)
                self.assertEqual(len(seen), 1)
                self.assertEqual(len(server.journal), 2 if approved else 0)
                self.assertEqual(run["status"] == "completed", approved)

    def test_high_risk_still_requires_approval_in_test(self):
        with DemoServer() as server:
            run = self.run_case(server, "TEST", risk="D")
            self.assertEqual(server.connections, 0)
            self.assertNotEqual(run["status"], "completed")

    def test_unknown_risk_and_control_target_denied(self):
        with DemoServer() as server:
            catalog, config, workflow = self.policy(server)
            from api_agent.policy import ProjectConfig

            for target, risk in (
                (server.target, "unknown"),
                ("http://127.0.0.1:18084", "C"),
            ):
                with self.assertRaises(ValueError):
                    ProjectConfig.create(
                        "p",
                        target,
                        catalog,
                        mode="TEST",
                        resettable=True,
                        operations={"createOrder": risk},
                    )


if __name__ == "__main__":
    unittest.main()

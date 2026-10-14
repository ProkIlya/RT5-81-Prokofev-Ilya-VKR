"""Подтверждение/отмена проверяются на границе реального соединения."""

import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from test_runtime_chain import contract, CreateReadModel
from api_agent.agent import StartAgentRun
from api_agent.agent.chains import CreateRead, ChainLimits
from api_agent.policy import ProjectConfig, PolicyWorkflow, PolicySession
from demo_api.server import DemoServer
from agent_support import count_test_tokens


class ApprovalTests(unittest.TestCase):
    def setup_policy(self, server, mode="SAFE", approval=None, current=None):
        catalog = contract()
        config = ProjectConfig.create(
            "p",
            server.target,
            catalog,
            mode=mode,
            resettable=True,
            operations={"createOrder": "C", "readOrder": "A"},
        )
        workflow = PolicyWorkflow(
            CreateRead(), config, approval=approval, current_config=current
        )
        return catalog, config, workflow

    def test_preview_shows_prepared_body_without_http(self):
        with DemoServer() as server:

            def approval(proposal):
                self.assertEqual(proposal["preview"]["json"], {"name": "A"})
                self.assertEqual(proposal["preview"]["method"], "POST")
                self.assertEqual(server.connections, 0)
                return {
                    "action_hash": proposal["action_hash"],
                    "config_version": proposal["config_version"],
                    "approved": True,
                }

            cat, cfg, wf = self.setup_policy(server, approval=approval)
            run = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start("p", "s", "m", "Create and read order", CreateReadModel())
            self.assertEqual(run["status"], "completed")

    def test_denial_has_explicit_audit_without_connections(self):
        with DemoServer() as server:
            cat, cfg, wf = self.setup_policy(server, mode="READ_ONLY")
            run = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start("p", "s", "m", "Create and read order", CreateReadModel())
            self.assertTrue(
                any(d["decision"] == "DENY" for d in run["policy_decisions"])
            )
            self.assertEqual(server.connections, 0)

    def test_cancel_during_approval_prevents_send(self):
        with DemoServer() as server:
            cancel = threading.Event()

            def approval(p):
                cancel.set()
                return {
                    "action_hash": p["action_hash"],
                    "config_version": p["config_version"],
                    "approved": True,
                }

            cat, cfg, wf = self.setup_policy(server, approval=approval)
            run = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start(
                "p", "s", "m", "Create and read order", CreateReadModel(), cancel=cancel
            )
            self.assertEqual(run["status"], "cancelled")
            self.assertEqual(server.connections, 0)

    def test_revoke_during_approval_prevents_send(self):
        with DemoServer() as server:
            cat = contract()
            configs = [
                ProjectConfig.create(
                    "p",
                    server.target,
                    cat,
                    mode="SAFE",
                    resettable=True,
                    operations={"createOrder": "C", "readOrder": "A"},
                )
            ]

            def approval(p):
                configs[0] = ProjectConfig.create(
                    "p",
                    server.target,
                    cat,
                    mode="SAFE",
                    resettable=True,
                    version=2,
                    operations={"readOrder": "A"},
                )
                return {
                    "action_hash": p["action_hash"],
                    "config_version": p["config_version"],
                    "approved": True,
                }

            wf = PolicyWorkflow(
                CreateRead(),
                configs[0],
                approval=approval,
                current_config=lambda: configs[0],
            )
            run = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start("p", "s", "m", "Create and read order", CreateReadModel())
            self.assertEqual(run["stop_reason"], "policy_denied")
            self.assertEqual(server.connections, 0)

    def test_stale_proof_rejected(self):
        with DemoServer() as server:
            cat, cfg, wf = self.setup_policy(
                server,
                approval=lambda p: {
                    "action_hash": "old",
                    "config_version": p["config_version"],
                    "approved": True,
                },
            )
            run = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start("p", "s", "m", "Create and read order", CreateReadModel())
            self.assertEqual(run["stop_reason"], "approval_rejected")
            self.assertEqual(server.connections, 0)

    def test_timeout_after_committed_post_marks_uncertain_and_never_repeats(self):
        with DemoServer() as server:
            server.mode = "timeout_after_commit"
            cat, cfg, wf = self.setup_policy(server, mode="TEST")
            run = {"run_id": "r", "project_id": "p"}
            session = PolicySession(server.target, cat, wf, run, ChainLimits())
            session.timeout = 0.1
            exchange = session.execute(
                {"operation_id": "createOrder", "body": {"name": "A"}}
            )
            self.assertEqual(exchange.get("effect_status"), "uncertain")
            self.assertTrue(run["uncertain_write"])
            with self.assertRaises(ValueError):
                session.execute({"operation_id": "createOrder", "body": {"name": "B"}})
            self.assertEqual(len(server.orders), 1)
            self.assertEqual(len(server.journal), 1)


if __name__ == "__main__":
    unittest.main()

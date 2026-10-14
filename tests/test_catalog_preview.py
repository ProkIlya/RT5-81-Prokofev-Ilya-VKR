"""Preview общего executor должен показывать эффект, сохраняя приватность auth."""

import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
import test_catalog_execution as support
from api_agent.agent import StartAgentRun
from demo_api.server import DemoServer
from agent_support import count_test_tokens


class GenericPreviewTests(unittest.TestCase):
    def test_non_auth_body_is_visible_in_owner_preview(self):
        with DemoServer() as server:
            seen = []

            def approve(p):
                seen.append(p)
                return {
                    "action_hash": p["action_hash"],
                    "config_version": p["config_version"],
                    "approved": True,
                }

            cat, cfg, wf, create, read = support.GenericExecutionTests().workflow(
                server, mode="SAFE", approval=approve
            )
            run = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start("p", "s", "m", wf.goal, support.GenericModel(create, read))
            self.assertEqual(run["status"], "completed")
            self.assertEqual(seen[0]["preview"].get("json"), {"name": "A"})
            self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()

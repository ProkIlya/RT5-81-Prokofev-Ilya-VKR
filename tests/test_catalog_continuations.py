"""Продолжения показывают доступные refs, но не выбирают объект вместо модели."""

import sys, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from api_agent.agent import StartAgentRun
import test_catalog_execution as support
from demo_api.server import DemoServer
from agent_support import count_test_tokens


class ContinuationTests(unittest.TestCase):
    def test_create_observation_exposes_ready_read_with_explicit_ref(self):
        with DemoServer() as server:
            cat, cfg, wf, create, read = support.GenericExecutionTests().workflow(
                server
            )
            run = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start("p", "s", "m", wf.goal, support.GenericModel(create, read))
            data = run["steps"][0]["observation"]["data"]
            hints = data.get("continuation_hints", [])
            self.assertTrue(hints, "Нет подсказки trusted продолжения")
            hint = next(h for h in hints if h["operation_id"] == read)
            self.assertEqual(hint["readiness"], "ready")
            self.assertEqual(
                hint["bindings"]["order_id"], [data["runtime_values"][0]["ref"]]
            )
            self.assertEqual(run["run_catalog"]["allowed_ids"], sorted([create, read]))


if __name__ == "__main__":
    unittest.main()

"""Полная допустимая цель не ограничивается лимитом короткого tool search query."""

import sys, json, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
import test_catalog_discovery as fixtures
from api_agent.agent import StartAgentRun
from agent_support import count_test_tokens
from demo_api.server import DemoServer


class LongGoalTests(unittest.TestCase):
    def test_long_goal_retains_late_terms_and_executes_real_http(self):
        with DemoServer() as server:
            cat, cfg, wf = fixtures.DiscoveryTests().workflow(server.target)
            goal = "Не меняй состояние сервиса. " * 30 + " Проверь pets."
            wf.goal = goal
            run = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start("p", "s", "long", goal, fixtures.DiscoveryModel())
            self.assertEqual(run["status"], "completed", run["stop_reason"])
            self.assertEqual(run["goal"], goal)
            self.assertIn(
                "z_pet_observation", run["selection_manifest"]["selected_ids"]
            )
            self.assertEqual(server.journal, [{"method": "GET", "path": "/pets/1"}])

    def test_tool_search_still_rejects_oversized_query(self):
        cat, cfg, wf = fixtures.DiscoveryTests().workflow("http://127.0.0.1:18080")
        with self.assertRaisesRegex(ValueError, "search_query_limit"):
            wf.discovery("r", {"query": "q" * 201})

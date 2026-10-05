"""Маршрут цели до loop без TUI; сеть настоящая, решения контролируемые."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]
from demo_api.server import DemoServer
from agent_support import ObserverModel


class AgentCliTests(unittest.TestCase):
    def test_goal_runs_and_persists_trace(self):
        from api_agent.agent.__main__ import main

        with DemoServer() as demo, tempfile.TemporaryDirectory() as directory:
            code = main(
                [
                    "--target",
                    demo.target,
                    "--spec",
                    str(ROOT / "tests/fixtures/demo_openapi.json"),
                    "--evidence-dir",
                    directory,
                    "Check pet",
                ],
                model=ObserverModel(),
            )
            self.assertEqual(code, 0)
            traces = list(Path(directory).glob("*.json"))
            self.assertEqual(len(traces), 1)
            run = json.loads(traces[0].read_text())
            self.assertEqual(run["status"], "completed")
            self.assertEqual(len(demo.journal), 1)

    def test_invalid_target_does_not_reach_network(self):
        from api_agent.agent.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(
                main(
                    [
                        "--target",
                        "http://example.com:80",
                        "--spec",
                        str(ROOT / "tests/fixtures/demo_openapi.json"),
                        "--evidence-dir",
                        directory,
                        "Check pet",
                    ],
                    model=ObserverModel(),
                ),
                1,
            )

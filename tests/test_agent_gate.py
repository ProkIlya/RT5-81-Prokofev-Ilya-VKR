"""Gate принимает допустимый inspect, но обнаруживает обход HTTP-бюджета."""

import json
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "src"), str(ROOT / "tests")]
from agent_support import ObserverModel, reply
from scripts import check_agent_loop


class InspectFirstModel(ObserverModel):
    """Реальная модель вправе сначала прочитать контракт, затем выполнить S02."""

    def __call__(self, payload):
        if not any(m["role"] == "tool" for m in payload["messages"]):
            return reply("inspect_operation", {"operation_id": "getPet"})
        return super().__call__(payload)


class AgentGateTests(unittest.TestCase):
    def test_good_accepts_valid_inspection_before_live_execution(self):
        # Unit gate не требует 1.5 GB весов; модель подменена, сеть настоящая.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in (
                "tests/fixtures/demo_openapi.json",
                "tests/gates/s04_good.json",
            ):
                destination = root / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes((ROOT / relative).read_bytes())
            weights = root / "models/MiniCPM5-2B-Q4_K_M.gguf"
            weights.parent.mkdir(parents=True)
            weights.write_bytes(b"unit model fixture")
            lock = root / "experiments/local_model/runtime-lock.json"
            lock.parent.mkdir(parents=True)
            lock.write_text(
                json.dumps(
                    {
                        "model": {
                            "sha256": hashlib.sha256(weights.read_bytes()).hexdigest()
                        }
                    }
                )
            )
            with patch.dict(
                os.environ, {"S04_EVIDENCE_DIR": str(root / "evidence")}
            ), patch.object(check_agent_loop, "ROOT", root), patch.object(
                check_agent_loop, "LocalModel", return_value=InspectFirstModel()
            ):
                self.assertEqual(check_agent_loop.main("good"), 0)

    def test_bad_gate_detects_missing_budget_guard(self):
        from api_agent.agent.runtime import StartAgentRun

        original = StartAgentRun.start

        def mutation(app, *args, **kwargs):
            run = original(app, *args, **kwargs)
            if run["stop_reason"] == "request_limit":
                run["stop_reason"] = "finished"
            return run

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"S04_EVIDENCE_DIR": directory}
        ), patch.object(StartAgentRun, "start", mutation):
            self.assertEqual(check_agent_loop.main("bad"), 1)
            result = json.loads((Path(directory) / "bad.json").read_text())
            self.assertEqual(result["result"], "failed")

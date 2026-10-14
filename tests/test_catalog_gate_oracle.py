"""Oracle gate должен отличать завершённый дефект API от успешной good-проверки."""

import sys, json, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from scripts import check_catalog as gate
from test_catalog_discovery import DiscoveryModel
from agent_support import count_test_tokens
from demo_api.server import DemoServer


class MeasuredDiscovery(DiscoveryModel):
    count_tokens = staticmethod(count_test_tokens)

    def __call__(self, payload):
        result = super().__call__(payload)
        count = count_test_tokens(payload)
        result["usage"] = {
            "prompt_tokens": count,
            "completion_tokens": 10,
            "total_tokens": count + 10,
        }
        return result


class CatalogGateOracleTests(unittest.TestCase):
    def run_gate(self, mutate):
        class MutatedDemo(DemoServer):
            def __init__(self):
                super().__init__()
                mutate(self.pet)

        with tempfile.TemporaryDirectory() as d, patch.dict(
            os.environ, {"S07_EVIDENCE_DIR": d}
        ), patch.object(
            gate, "LocalModel", return_value=MeasuredDiscovery()
        ), patch.object(
            gate, "DemoServer", MutatedDemo
        ):
            code = gate.main("d", "good")
            trace = json.loads(next(Path(d).rglob("catalog100-run.json")).read_text())
        return code, trace

    def test_good_gate_accepts_verified_success(self):
        code, r = self.run_gate(lambda pet: None)
        self.assertEqual(code, 0)
        self.assertEqual(r["report"]["intent_verdict"], "pass")

    def test_good_gate_rejects_completed_wrong_pet(self):
        code, r = self.run_gate(lambda pet: pet.update(name="WrongPet"))
        self.assertEqual(r["status"], "completed")
        self.assertEqual(r["report"]["intent_verdict"], "fail")
        self.assertEqual(code, 1)

    def test_good_gate_rejects_completed_contract_mismatch(self):
        code, r = self.run_gate(lambda pet: pet.pop("id"))
        self.assertEqual(r["report"]["contract_verdict"], "mismatch")
        self.assertEqual(code, 1)

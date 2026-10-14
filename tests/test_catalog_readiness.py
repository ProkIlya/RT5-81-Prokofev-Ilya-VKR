"""Readiness и продолжения основаны на текущих refs, а не расширении прав."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from test_catalog_discovery import hundred_catalog
from test_catalog_execution import status_condition
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.policy import ProjectConfig


class ReadinessTests(unittest.TestCase):
    def test_inspect_no_inputs_reports_ready_without_http(self):
        cat = hundred_catalog()
        cfg = ProjectConfig.create(
            "p",
            "http://127.0.0.1:18080",
            cat,
            mode="READ_ONLY",
            resettable=True,
            operations={"z_pet_observation": "A"},
        )
        wf = CatalogWorkflow(
            cat,
            cfg,
            goal="x",
            conditions={"z_pet_observation": [status_condition(200)]},
        )
        data = wf.inspect(cat, "z_pet_observation")
        self.assertEqual(data.get("input_readiness"), "ready")
        self.assertIn("execute_operation", data.get("next_hint", ""))


if __name__ == "__main__":
    unittest.main()

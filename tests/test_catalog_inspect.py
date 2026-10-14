"""Bounded inspect сообщает секции и не выдаёт неполную schema за полную."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from test_catalog_execution import catalog_variant, status_condition
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.policy import ProjectConfig


class InspectTests(unittest.TestCase):
    def test_default_inputs_and_explicit_response_section_are_exact_and_labelled(self):
        cat = catalog_variant("q")
        cfg = ProjectConfig.create(
            "p",
            "http://127.0.0.1:18080",
            cat,
            mode="TEST",
            resettable=True,
            operations={"qcreateOrder": "C"},
        )
        wf = CatalogWorkflow(
            cat, cfg, goal="q", conditions={"qcreateOrder": [status_condition(201)]}
        )
        inputs = wf.inspect(cat, "qcreateOrder")
        self.assertEqual(inputs.get("sections"), ["inputs"])
        self.assertEqual(
            inputs["catalog_operation"]["requestBody"],
            cat.operations["qcreateOrder"]["requestBody"],
        )
        self.assertEqual(inputs["omitted_sections"], ["responses"])
        responses = wf.inspect(cat, "qcreateOrder", section="responses")
        self.assertEqual(
            responses["catalog_operation"]["responses"],
            cat.operations["qcreateOrder"]["responses"],
        )
        self.assertEqual(responses["sections"], ["responses"])


if __name__ == "__main__":
    unittest.main()

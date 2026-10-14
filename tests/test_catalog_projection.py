"""Проекция для модели сохраняет данные и evidence, убирая пустые служебные поля."""

import unittest
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from test_catalog_execution import catalog_variant, status_condition
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.policy import ProjectConfig


class ProjectionTests(unittest.TestCase):
    def test_projection_preserves_data_and_diagnostics_without_mutating_record(self):
        cat = catalog_variant("x")
        cfg = ProjectConfig.create(
            "p",
            "http://127.0.0.1:18080",
            cat,
            mode="TEST",
            resettable=True,
            operations={"xcreateOrder": "C"},
        )
        wf = CatalogWorkflow(
            cat, cfg, goal="x", conditions={"xcreateOrder": [status_condition(201)]}
        )
        record = {
            "status": "error",
            "data": None,
            "error": {"error_code": "permission_denied", "hint": "Confirm action"},
            "evidence_ids": ["own"],
            "provenance": {"source": "HTTP", "run_id": "own"},
            "bytes_total": 500,
            "bytes_returned": 0,
            "truncated": False,
            "omitted_fields": ["response.json"],
            "summary_only": True,
        }
        self.assertTrue(hasattr(wf, "model_result"), "Нет bounded проекции ToolResult")
        projected = wf.model_result(record)
        self.assertEqual(projected["error"], record["error"])
        self.assertEqual(projected["evidence_ids"], ["own"])
        self.assertEqual(projected["omitted_fields"], ["response.json"])
        self.assertNotIn("bytes_total", projected)
        self.assertEqual(record["bytes_total"], 500)


if __name__ == "__main__":
    unittest.main()

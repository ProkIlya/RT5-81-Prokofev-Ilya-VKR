"""Диагностика должна помогать исправлению формы без утечки model values."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from test_catalog_execution import catalog_variant, status_condition
from api_agent.agent.catalog import CatalogWorkflow, CatalogSession
from api_agent.agent.chains import ChainLimits
from api_agent.policy import ProjectConfig
from demo_api.server import DemoServer


class CatalogHintsTests(unittest.TestCase):
    def test_missing_body_has_exact_repair_hint(self):
        with DemoServer() as server:
            cat = catalog_variant("new")
            cfg = ProjectConfig.create(
                "p",
                server.target,
                cat,
                mode="TEST",
                resettable=True,
                operations={"newcreateOrder": "C"},
            )
            wf = CatalogWorkflow(
                cat,
                cfg,
                goal="Создай A",
                conditions={"newcreateOrder": [status_condition(201)]},
            )
            session = CatalogSession(
                server.target,
                cat,
                wf,
                {"run_id": "r", "project_id": "p"},
                ChainLimits(),
            )
            try:
                session.admit({"operation_id": "newcreateOrder"})
            except ValueError as error:
                hint = getattr(error, "repair_hint", "")
                self.assertIn("body", hint)
                self.assertIn("name", hint)
            else:
                self.fail("missing body accepted")
            self.assertEqual(server.connections, 0)

    def test_trusted_binding_cannot_be_bypassed_by_literal_id(self):
        with DemoServer() as server:
            cat = catalog_variant("new")
            cfg = ProjectConfig.create(
                "p",
                server.target,
                cat,
                mode="TEST",
                resettable=True,
                operations={"newcreateOrder": "C", "newreadOrder": "A"},
            )
            wf = CatalogWorkflow(
                cat,
                cfg,
                goal="Read created",
                conditions={"newreadOrder": [status_condition(200)]},
                extraction={
                    "newcreateOrder": {"/id": {"type": "integer", "secret": False}}
                },
                binding_rules={
                    "newreadOrder": {
                        "order_id": {
                            "source_operation": "newcreateOrder",
                            "source_path": "/id",
                            "response_path": "/id",
                        }
                    }
                },
            )
            session = CatalogSession(
                server.target,
                cat,
                wf,
                {"run_id": "r", "project_id": "p"},
                ChainLimits(),
            )
            with self.assertRaises(ValueError):
                session.admit(
                    {"operation_id": "newreadOrder", "parameters": {"order_id": 1}}
                )
            self.assertEqual(server.connections, 0)


if __name__ == "__main__":
    unittest.main()

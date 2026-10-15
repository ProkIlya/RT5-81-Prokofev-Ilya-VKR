"""Проекция для модели сохраняет данные и evidence, убирая пустые служебные поля."""

import unittest
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from test_catalog_execution import catalog_variant, status_condition
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.policy import ProjectConfig


class ProjectionTests(unittest.TestCase):
    def test_http_200_and_failed_expectation_are_explicitly_separate(self):
        cat = catalog_variant('x')
        cfg = ProjectConfig.create('p','http://127.0.0.1:18080',cat,mode='TEST',resettable=True,operations={'xcreateOrder':'C'})
        wf = CatalogWorkflow(cat,cfg,goal='x',conditions={'xcreateOrder':[status_condition(401)]})
        record = {'status':'ok','data':{
            'exchange_id':'actual-exchange', 'outcome':'received','status':200,
            'response_json':{'id':1,'login':'user'},
            'validation':{'contract_verdict':'pass','intent_verdict':'fail','task_postcondition':True,
                          'expected_status_codes':[401]},
            'finish_evidence_ids':['actual-exchange'],'checks_complete':True,
            'next_hint':'Call finish_run with all finish_evidence_ids.'},
            'error':None,'evidence_ids':['actual-exchange'],'omitted_fields':[],'truncated':False}
        original = deepcopy(record)
        projected = wf.model_result(record)
        self.assertIn('tool_execution', projected, 'Успех инструмента смешан с HTTP status')
        self.assertEqual(projected['tool_execution'], 'ok')
        self.assertEqual(projected['data']['http_response']['status_code'], 200)
        self.assertEqual(projected['data']['http_response']['body'], {'id':1,'login':'user'})
        self.assertEqual(projected['data']['machine_checks']['expectation_verdict'], 'fail')
        self.assertEqual(projected['data']['machine_checks']['expected_status_codes'], [401])
        self.assertNotIn('status', projected['data'])
        self.assertNotIn('validation', projected['data'])
        self.assertEqual(record, original)

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

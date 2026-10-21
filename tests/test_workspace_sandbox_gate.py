"""Сбой среды или отключённый guard не превращаются в успешный bad2."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]


class WorkspaceSandboxGateTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.check_workspace_sandbox'), 'Нет gate S09d2')
        from scripts import check_workspace_sandbox
        return check_workspace_sandbox

    def test_missing_image_is_preparation_error_not_bad2(self):
        gate = self.module()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'S09_EVIDENCE_DIR': directory, 'WORKSPACE_IMAGE': ''}):
            self.assertEqual(gate.main('bad'), 1)

    def test_disabled_authority_guard_and_missing_test_are_gate_error(self):
        gate = self.module()
        from api_agent.workspace_broker import WorkspaceBroker
        name = 'test_workspace_broker.WorkspaceBrokerTests.test_forged_id_stale_artifact_and_protocol_authority_fields_never_connect'
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'S09_EVIDENCE_DIR': directory}), patch.object(gate, 'preflight', return_value={'controlled': True}):
            with patch.dict(gate.CASES, {'bad': [name]}), patch.object(WorkspaceBroker, '_operation', return_value={'status': 200, 'body': {'id': 1}}):
                self.assertEqual(gate.main('bad'), 1)
            with patch.dict(gate.CASES, {'bad': ['missing_workspace_test.NoTest']}):
                self.assertEqual(gate.main('bad'), 1)


if __name__ == '__main__': unittest.main()

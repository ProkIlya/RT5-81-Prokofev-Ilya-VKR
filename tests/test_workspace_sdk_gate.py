"""Gate S09d1 должен обнаруживать сломанный контроль целостности SDK."""
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]


class WorkspaceSdkGateTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('scripts.check_workspace_sdk'), 'Нет gate S09d1')
        from scripts import check_workspace_sdk
        return check_workspace_sdk

    def test_disabled_integrity_validation_breaks_bad_gate(self):
        gate = self.api()
        from test_workspace_sdk import WorkspaceSdkTests
        artifact, _, _ = WorkspaceSdkTests().build()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'S09_EVIDENCE_DIR': folder}), \
                patch('api_agent.workspace.sdk.SdkArtifact.load', return_value=artifact):
            self.assertEqual(gate.main('bad'), 1)

    def test_missing_test_is_not_successful_negative_control(self):
        gate = self.api()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {'S09_EVIDENCE_DIR': folder}), \
                patch.dict(gate.CASES, {'bad': ['missing_workspace_test.Case.test_missing']}):
            self.assertEqual(gate.main('bad'), 1)


if __name__ == '__main__': unittest.main()

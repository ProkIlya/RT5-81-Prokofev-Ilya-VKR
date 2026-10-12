"""Приёмочный gate должен обнаруживать пропущенный validator/journal guard."""

import json, os, sys, tempfile, unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]
from scripts import check_validation, check_agent_resilience
from api_agent.validation import validate_exchange
from api_agent.agent.runtime import StartAgentRun


class ValidationGateTests(unittest.TestCase):
    def test_negative_gate_detects_validator_accepting_bad_response(self):
        def mutation(*args):
            result = validate_exchange(*args)
            result.update(
                contract_verdict="pass", issues=[], confirmed_contract_violations=0
            )
            return result

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"S05_EVIDENCE_DIR": directory}
        ):
            self.assertEqual(check_validation.main("bad"), 2)
            with patch(
                "api_agent.agent.runtime.validate_exchange", side_effect=mutation
            ):
                self.assertEqual(check_validation.main("bad"), 1)

    def test_negative_gate_detects_missing_journal_guard(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"S05_EVIDENCE_DIR": directory}
        ):
            self.assertEqual(check_agent_resilience.main("bad"), 2)
            with patch.object(StartAgentRun, "_emit", return_value=True):
                self.assertEqual(check_agent_resilience.main("bad"), 1)

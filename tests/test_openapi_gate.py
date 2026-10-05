"""CLI gates обязаны проверять данные и сеть, а не только exit самого parser."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class GateTests(unittest.TestCase):
    def test_good_and_bad_cli_produce_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            for mode, code in (("good", 0), ("bad", 2)):
                result = subprocess.run(
                    [sys.executable, "scripts/check_stage.py", "S03", mode],
                    env={**os.environ, "S03_EVIDENCE_DIR": directory},
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                evidence = json.loads(
                    (Path(directory) / (mode + ".json")).read_text(encoding="utf-8")
                )
                self.assertEqual(evidence["exit_code"], code)
                self.assertEqual(evidence["forbidden_connections"], 0)
                self.assertEqual(evidence["observer_positive_control"], "passed")
                if mode == "good":
                    self.assertEqual(
                        sorted(evidence["catalog"]["operations"]),
                        ["GET /health", "createPet", "getPet"],
                    )

    def test_gate_detects_silent_operation_loss_and_external_ref_acceptance(self):
        # Мутация результата parser: gate обязан стать красным, а не принять subset.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        from scripts.check_openapi import verify_good, require_rejected
        from api_agent.openapi import Catalog

        fixture = json.loads(
            Path("tests/gates/s03_good.json").read_text(encoding="utf-8")
        )
        damaged = json.loads(json.dumps(fixture["expected_snapshot"]))
        del damaged["operations"]["createPet"]
        with self.assertRaises(RuntimeError):
            verify_good(Catalog(json.dumps(damaged).encode()), fixture)
        with self.assertRaises(RuntimeError):
            require_rejected(lambda: Catalog(b"{}"), "external_ref")

    def test_user_cli_displays_catalog_and_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "PYTHONPATH": str(Path("src").resolve())}
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "api_agent.openapi",
                    "tests/fixtures/profile_openapi.json",
                ],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                sorted(json.loads(result.stdout)["snapshot"]["operations"]),
                ["GET /health", "createPet", "getPet"],
            )
            path = Path(directory) / "bad.json"
            path.write_text('{"openapi":"3.0.3"}', encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "api_agent.openapi", str(path)],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(json.loads(result.stderr)["code"], "invalid_document")

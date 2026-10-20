"""Gate возвращает 2 только после действительных отрицательных контролей."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class RepairGateTests(unittest.TestCase):
    def test_bad_gate_runs_controls_and_writes_versioned_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            env = {**os.environ, 'S08R_EVIDENCE_DIR': folder}
            process = subprocess.run([sys.executable, '-X', 'utf8', 'scripts/check_stage.py', 'S08Ra', 'bad'],
                cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 2, process.stdout + process.stderr)
            files = list(Path(folder).rglob('verification.json'))
            self.assertEqual(len(files), 1)
            evidence = json.loads(files[0].read_text(encoding='utf-8'))
            self.assertGreater(evidence['tests_run'], 0)
            self.assertEqual(evidence['exit_code'], 2)
            self.assertTrue(evidence['source_hashes'])


if __name__ == '__main__':
    unittest.main()

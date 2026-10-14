"""Родитель S07 не может объявиться принятым без всех четырёх gates."""

import sys, os, json, subprocess, tempfile, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class ParentGateTests(unittest.TestCase):
    def test_parent_bad_routes_all_parts_and_preserves_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {
                **os.environ,
                "S07_EVIDENCE_DIR": directory,
                "PYTHONPATH": str(ROOT / "src")
                + ";"
                + str(ROOT / "tests")
                + ";"
                + str(ROOT),
            }
            result = subprocess.run(
                [sys.executable, "-X", "utf8", "scripts/check_stage.py", "S07", "bad"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
            )
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            parents = list(Path(directory).glob("parent-bad-*.json"))
            self.assertEqual(len(parents), 1)
            record = json.loads(parents[0].read_text(encoding="utf-8"))
            self.assertEqual(record["parts"], {"a": 2, "b": 2, "c": 2, "d": 2})
            self.assertEqual(record["exit_code"], 2)


if __name__ == "__main__":
    unittest.main()

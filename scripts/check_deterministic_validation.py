"""Общий gate S05; родитель принят только при обоих последовательных частях."""

import hashlib
import json
import os
from pathlib import Path
import sys
from scripts import check_validation, check_agent_resilience

ROOT = Path(__file__).resolve().parents[1]


def main(mode):
    directory = Path(os.environ.get("S05_EVIDENCE_DIR", ROOT / "evidence/S05"))
    directory.mkdir(parents=True, exist_ok=True)
    expected = 0 if mode == "good" else 2
    a = check_validation.main(mode)
    b = check_agent_resilience.main(mode) if a == expected else None
    code = expected if a == b == expected else 1
    paths = (
        sorted((ROOT / "src/api_agent").rglob("*.py"))
        + sorted((ROOT / "scripts").glob("check_*.py"))
        + sorted((ROOT / "tests/gates").glob("s05_*.json"))
    )
    manifest = {
        "stage": "S05",
        "mode": mode,
        "exit_code": code,
        "parts": {"a": a, "b": b},
        "source_hashes": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths
        },
    }
    (directory / (mode + ".json")).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("S05", mode, "exit=" + str(code))
    return code

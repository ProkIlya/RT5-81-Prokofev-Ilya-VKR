"""Проверки срезов S01–S05; запускать из корня с готовым локальным llama-server."""

import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from experiments.local_model.spike import (
    InvalidAction,
    collect_stream,
    run_native_round_trip,
    validate_action,
)


def runtime_url(base):
    parts = urlsplit(base)
    if (
        parts.scheme != "http"
        or parts.hostname not in ("127.0.0.1", "localhost")
        or parts.port is None
        or parts.username
        or parts.password
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError(
            "S01 runtime must be a loopback HTTP endpoint with explicit port"
        )
    return f"http://{parts.hostname}:{parts.port}/v1/chat/completions"


def peak_resources(samples):
    return {
        key: max(sample[key] for sample in samples if sample.get(key) is not None)
        for key in ("system_ram_used_bytes", "server_rss_bytes", "vram_used_mib")
        if any(sample.get(key) is not None for sample in samples)
    }


def sample_resources():
    sample = {
        "elapsed_wall_time": time.time(),
        "system_ram_used_bytes": None,
        "server_rss_bytes": None,
        "vram_used_mib": None,
    }
    try:
        import psutil

        sample["system_ram_used_bytes"] = psutil.virtual_memory().used
        for process in psutil.process_iter(["name", "cmdline", "memory_info"]):
            try:
                if process.info["name"] == "llama-server.exe" and "18081" in (
                    process.info["cmdline"] or []
                ):
                    sample["server_rss_bytes"] = process.info["memory_info"].rss
                    break
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
    except (ImportError, OSError):
        pass
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            sample["vram_used_mib"] = int(result.stdout.splitlines()[0].strip())
    except (OSError, subprocess.TimeoutExpired, ValueError, IndexError):
        pass
    return sample


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def model_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as model_file:
        for chunk in iter(lambda: model_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def post_stream(payload):
    url = runtime_url(os.environ.get("S01_BASE_URL", "http://127.0.0.1:18081"))
    body = json.dumps({**payload, "stream": True}).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    started = time.perf_counter()
    first_token = None

    def mark_first_token():
        nonlocal first_token
        first_token = time.perf_counter() - started

    with urllib.request.urlopen(request, timeout=180) as stream:
        response = collect_stream(stream, mark_first_token)
    response["_timing"] = {
        "ttft_seconds": first_token,
        "wall_seconds": time.perf_counter() - started,
    }
    return response


def main():
    if len(sys.argv) == 3 and sys.argv[1] in {'S09b', 'S09b1', 'S09b2', 'S09b3'} and sys.argv[2] in ('good', 'bad'):
        from scripts.check_goals import main as goals_main
        return goals_main(sys.argv[1], sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == 'S09a' and sys.argv[2] in ('good', 'bad'):
        from scripts.check_project_facts import main as facts_main
        return facts_main(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] in ('S08R', 'S08Ra', 'S08Rb', 'S08Rc', 'S08Rd', 'S08Re') and sys.argv[2] in ('good', 'bad'):
        from scripts.check_integration_repair import main as repair_main
        return repair_main(sys.argv[1], sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] in ("S08", "S08a", "S08b") and sys.argv[2] in ("good", "bad"):
        from scripts.check_history import main as history_main
        mode = sys.argv[2]
        expected = 0 if mode == "good" else 2
        parts = "ab" if sys.argv[1] == "S08" else sys.argv[1][-1]
        codes = {part: history_main(part, mode) for part in parts}
        return expected if all(code == expected for code in codes.values()) else 1
    if len(sys.argv) == 3 and sys.argv[1] == "S07" and sys.argv[2] in ("good", "bad"):
        from scripts.check_policy import main as policy_main
        from scripts.check_catalog import main as catalog_main

        expected = 0 if sys.argv[2] == "good" else 2
        parts = {}
        for part, runner in (
            ("a", policy_main),
            ("b", policy_main),
            ("c", catalog_main),
            ("d", catalog_main),
        ):
            parts[part] = runner(part, sys.argv[2])
            if parts[part] != expected:
                break
        code = expected if parts == {part: expected for part in "abcd"} else 1
        directory = Path(os.environ.get("S07_EVIDENCE_DIR", ROOT / "evidence/S07"))
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / (
            "parent-" + sys.argv[2] + "-" + str(time.time_ns()) + ".json"
        )
        path.write_text(
            json.dumps(
                {
                    "stage": "S07",
                    "mode": sys.argv[2],
                    "parts": parts,
                    "exit_code": code,
                    "argv": sys.argv,
                    "python": sys.executable,
                    "cwd": str(Path.cwd()),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(json.dumps({"stage": "S07", "exit_code": code, "evidence": str(path)}))
        return code
    if (
        len(sys.argv) == 3
        and sys.argv[1] in ("S07c", "S07d")
        and sys.argv[2] in ("good", "bad")
    ):
        from scripts.check_catalog import main as catalog_main

        return catalog_main(sys.argv[1][-1], sys.argv[2])
    if (
        len(sys.argv) == 3
        and sys.argv[1] in ("S07a", "S07b")
        and sys.argv[2] in ("good", "bad")
    ):
        from scripts.check_policy import main as policy_main

        return policy_main(sys.argv[1][-1], sys.argv[2])
    if (
        len(sys.argv) == 3
        and sys.argv[1] in ("S06", "S06a", "S06b")
        and sys.argv[2] in ("good", "bad")
    ):
        from scripts.check_runtime_chain import main as chain_main

        mode = sys.argv[2]
        if sys.argv[1] != "S06":
            return chain_main(sys.argv[1][-1], mode)
        expected = 0 if mode == "good" else 2
        a = chain_main("a", mode)
        b = chain_main("b", mode) if a == expected else None
        code = expected if a == b == expected else 1
        root = Path(os.environ.get("S06_EVIDENCE_DIR", ROOT / "evidence/S06"))
        manifest = root / ("parent-" + mode + "-" + str(time.time_ns()) + ".json")
        sources = (
            sorted((ROOT / "src/api_agent").rglob("*.py"))
            + sorted((ROOT / "demo_api").rglob("*.py"))
            + sorted((ROOT / "scripts").glob("check_*.py"))
            + sorted((ROOT / "tests").rglob("*.py"))
            + sorted((ROOT / "tests/fixtures").glob("*.json"))
        )
        manifest.write_text(
            json.dumps(
                {
                    "stage": "S06",
                    "mode": mode,
                    "exit_code": code,
                    "parts": {"a": a, "b": b},
                    "source_hashes": {
                        str(p.relative_to(ROOT)): hashlib.sha256(
                            p.read_bytes()
                        ).hexdigest()
                        for p in sources
                    },
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print("S06", mode, "exit=" + str(code), "manifest=" + str(manifest))
        return code

    if len(sys.argv) == 3 and sys.argv[1] == "S05" and sys.argv[2] in ("good", "bad"):
        from scripts.check_deterministic_validation import main as validation_main

        return validation_main(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == "S04" and sys.argv[2] in ("good", "bad"):
        from scripts.check_agent_loop import main as agent_main

        return agent_main(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == "S03" and sys.argv[2] in ("good", "bad"):
        from scripts.check_openapi import main as openapi_main

        return openapi_main(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == "S02" and sys.argv[2] in ("good", "bad"):
        # Сохраняем общий CLI плана, а реализацию сетевого gate держим отдельно.
        from scripts.check_http_e2e import main as s02_main

        return s02_main(sys.argv[2])
    if len(sys.argv) != 3 or sys.argv[1] != "S01" or sys.argv[2] not in ("good", "bad"):
        print(
            "usage: python scripts/check_stage.py S01|S02|S03|S04|S05|S06[a|b]|S07[a|b|c|d]|S08[a|b] good|bad",
            file=sys.stderr,
        )
        return 1
    mode = sys.argv[2]
    evidence = Path(os.environ.get("S01_EVIDENCE_DIR", ROOT / "evidence" / "S01"))
    evidence.mkdir(parents=True, exist_ok=True)
    if mode == "bad":
        fixture = json.loads(
            (ROOT / "tests" / "gates" / "s01_bad.json").read_text(encoding="utf-8")
        )
        rejected = []
        for case in fixture["cases"]:
            try:
                validate_action(case)
            except InvalidAction as error:
                rejected.append({"case": case, "reason": str(error)})
            else:
                save_json(
                    evidence / "bad.json",
                    {
                        "fixture": fixture,
                        "rejected_cases": rejected,
                        "result": "accepted_in_error",
                        "exit_code": 1,
                    },
                )
                print("S01_BAD_ACCEPTED", file=sys.stderr)
                return 1
        save_json(
            evidence / "bad.json",
            {
                "fixture": fixture,
                "rejected_cases": rejected,
                "result": "rejected",
                "exit_code": 2,
            },
        )
        print("S01_BAD_REJECTED:", len(rejected), "cases")
        return 2

    raw = []
    model = os.environ.get("S01_MODEL", "MiniCPM5-2B-Q4_K_M")
    model_file = ROOT / "models" / "MiniCPM5-2B-Q4_K_M.gguf"
    expected_hash = "ec2d5801640099e97d8d7e8003ad4d81f336e757811f03a26173dddf386602fd"
    samples = []
    stop_sampling = threading.Event()

    def sample_until_stopped():
        while not stop_sampling.wait(0.5):
            samples.append(sample_resources())

    try:
        actual_hash = model_hash(model_file)
        if actual_hash != expected_hash:
            raise RuntimeError("model SHA-256 differs from locked official file")
        samples.append(sample_resources())
        sampler = threading.Thread(target=sample_until_stopped, daemon=True)
        sampler.start()
        try:
            trace = run_native_round_trip(post_stream, model, raw)
        finally:
            stop_sampling.set()
            sampler.join(timeout=6)
            samples.append(sample_resources())
            save_json(evidence / "resource-samples.json", samples)
        fixture = json.loads(
            (ROOT / "tests" / "gates" / "s01_good.json").read_text(encoding="utf-8")
        )
        names = [action["name"] for action in trace["actions"]]
        if (
            names != fixture["expected_actions"]
            or trace["observation"]["operation_id"] != fixture["operation_id"]
        ):
            raise RuntimeError("good fixture expectations not met")
        if fixture["expected_response"] not in trace["observation"]["responses"]:
            raise RuntimeError("expected response absent from fake observation")
        result = {
            "result": "accepted",
            "exit_code": 0,
            "model_sha256": actual_hash,
            "trace": trace,
            "raw_exchanges": raw,
            "resource_peaks": peak_resources(samples),
            "resource_sample_count": len(samples),
        }
        save_json(evidence / "redacted-trace.json", trace)
        save_json(evidence / "good.json", result)
        print(
            "S01_GOOD_ACCEPTED",
            "actions=" + ",".join(names),
            "ttft_seconds="
            + ",".join(
                str(item["response"]["_timing"]["ttft_seconds"]) for item in raw
            ),
        )
        return 0
    except Exception as error:
        save_json(
            evidence / "good.json",
            {
                "result": "failed",
                "exit_code": 1,
                "error": f"{type(error).__name__}: {error}",
                "raw_exchanges": raw,
            },
        )
        print("S01_GOOD_FAILED:", error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

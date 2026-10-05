"""Проверки срезов S01–S03; запускать из корня с готовым локальным llama-server."""

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

from experiments.local_model.spike import InvalidAction, collect_stream, run_native_round_trip, validate_action


def runtime_url(base):
    parts = urlsplit(base)
    if (parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost")
            or parts.port is None or parts.username or parts.password or parts.path not in ("", "/")
            or parts.query or parts.fragment):
        raise ValueError("S01 runtime must be a loopback HTTP endpoint with explicit port")
    return f"http://{parts.hostname}:{parts.port}/v1/chat/completions"


def peak_resources(samples):
    return {key: max(sample[key] for sample in samples if sample.get(key) is not None)
            for key in ("system_ram_used_bytes", "server_rss_bytes", "vram_used_mib")
            if any(sample.get(key) is not None for sample in samples)}


def sample_resources():
    sample = {"elapsed_wall_time": time.time(), "system_ram_used_bytes": None,
              "server_rss_bytes": None, "vram_used_mib": None}
    try:
        import psutil
        sample["system_ram_used_bytes"] = psutil.virtual_memory().used
        for process in psutil.process_iter(["name", "cmdline", "memory_info"]):
            try:
                if process.info["name"] == "llama-server.exe" and "18081" in (process.info["cmdline"] or []):
                    sample["server_rss_bytes"] = process.info["memory_info"].rss
                    break
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
    except (ImportError, OSError):
        pass
    try:
        result = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                                capture_output=True, text=True, timeout=5)
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
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    started = time.perf_counter()
    first_token = None

    def mark_first_token():
        nonlocal first_token
        first_token = time.perf_counter() - started

    with urllib.request.urlopen(request, timeout=180) as stream:
        response = collect_stream(stream, mark_first_token)
    response["_timing"] = {"ttft_seconds": first_token, "wall_seconds": time.perf_counter() - started}
    return response


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "S03" and sys.argv[2] in ("good", "bad"):
        from scripts.check_openapi import main as openapi_main
        return openapi_main(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == "S02" and sys.argv[2] in ("good", "bad"):
        # Сохраняем общий CLI плана, а реализацию сетевого gate держим отдельно.
        from scripts.check_http_e2e import main as s02_main
        return s02_main(sys.argv[2])
    if len(sys.argv) != 3 or sys.argv[1] != "S01" or sys.argv[2] not in ("good", "bad"):
        print("usage: python scripts/check_stage.py S01|S02|S03 good|bad", file=sys.stderr)
        return 1
    mode = sys.argv[2]
    evidence = Path(os.environ.get("S01_EVIDENCE_DIR", ROOT / "evidence" / "S01"))
    evidence.mkdir(parents=True, exist_ok=True)
    if mode == "bad":
        fixture = json.loads((ROOT / "tests" / "gates" / "s01_bad.json").read_text(encoding="utf-8"))
        rejected = []
        for case in fixture["cases"]:
            try:
                validate_action(case)
            except InvalidAction as error:
                rejected.append({"case": case, "reason": str(error)})
            else:
                save_json(evidence / "bad.json", {"fixture": fixture, "rejected_cases": rejected,
                                                   "result": "accepted_in_error", "exit_code": 1})
                print("S01_BAD_ACCEPTED", file=sys.stderr)
                return 1
        save_json(evidence / "bad.json", {"fixture": fixture, "rejected_cases": rejected,
                                           "result": "rejected", "exit_code": 2})
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
        fixture = json.loads((ROOT / "tests" / "gates" / "s01_good.json").read_text(encoding="utf-8"))
        names = [action["name"] for action in trace["actions"]]
        if names != fixture["expected_actions"] or trace["observation"]["operation_id"] != fixture["operation_id"]:
            raise RuntimeError("good fixture expectations not met")
        if fixture["expected_response"] not in trace["observation"]["responses"]:
            raise RuntimeError("expected response absent from fake observation")
        result = {"result": "accepted", "exit_code": 0, "model_sha256": actual_hash,
                  "trace": trace, "raw_exchanges": raw, "resource_peaks": peak_resources(samples),
                  "resource_sample_count": len(samples)}
        save_json(evidence / "redacted-trace.json", trace)
        save_json(evidence / "good.json", result)
        print("S01_GOOD_ACCEPTED", "actions=" + ",".join(names),
              "ttft_seconds=" + ",".join(str(item["response"]["_timing"]["ttft_seconds"]) for item in raw))
        return 0
    except Exception as error:
        save_json(evidence / "good.json", {"result": "failed", "exit_code": 1,
                                           "error": f"{type(error).__name__}: {error}", "raw_exchanges": raw})
        print("S01_GOOD_FAILED:", error, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

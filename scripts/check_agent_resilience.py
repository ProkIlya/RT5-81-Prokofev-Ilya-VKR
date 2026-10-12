"""S05b good/bad: реальные HTTP и JSONL, controlled guards и живая модель.

0 — хорошие сценарии приняты; 2 — каждая плохая фикстура отклонена именно
своим guard; 1 — ошибка подготовки/исполнения или пропущенный инвариант.
"""

import hashlib
import http.client
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]
from api_agent.agent import StartAgentRun, Limits
from api_agent.agent.model import LocalModel
from api_agent.agent.resilience import JsonlJournal
from api_agent.openapi import parse_document
from agent_support import ObserverModel, reply
from demo_api.server import DemoServer


def require(condition, label):
    """Assertions gate обязательны также при python -O."""
    if not condition:
        raise RuntimeError(label)


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main(mode):
    directory = Path(os.environ.get("S05_EVIDENCE_DIR", ROOT / "evidence/S05")) / "b"
    directory.mkdir(parents=True, exist_ok=True)
    runs = []
    try:
        fixtures = json.loads((ROOT / "tests/gates/s05_resilience.json").read_text())
        catalog = parse_document(
            json.loads((ROOT / "tests/fixtures/demo_openapi.json").read_text())
        )
        with DemoServer() as demo, DemoServer() as forbidden:
            observer = http.client.HTTPConnection(
                "127.0.0.1", forbidden.server.server_port, timeout=3
            )
            try:
                observer.request("GET", "/pets/1")
                observer.getresponse().read()
            finally:
                observer.close()
            require(
                forbidden.connections == 1 and len(forbidden.journal) == 1,
                "observer_positive_control",
            )
            forbidden.reset()
            for case in fixtures[mode]:
                demo.reset()
                now = [0.0]
                payloads = []
                normal = ObserverModel()
                secret = "CANARY-S05-SECRET" if case.get("secret") else None
                if secret:
                    demo.pet["name"] = secret
                case_dir = directory / case["name"]
                journal = JsonlJournal(case_dir)

                def model(payload):
                    payloads.append(payload)
                    kind = case["model"]
                    if kind == "empty":
                        response = {"choices": []}
                    elif kind in {"inspect", "time"}:
                        response = reply(
                            "inspect_operation", {"operation_id": "getPet"}
                        )
                        if kind == "time":
                            now[0] = 85 if len(payloads) == 1 else 101
                    elif kind == "execute":
                        response = reply(
                            "execute_operation", {"operation_id": "getPet"}
                        )
                    elif kind == "unknown_tool":
                        response = reply("fetch_url", {"url": forbidden.target})
                    elif kind == "bad_finish":
                        response = reply(
                            "finish_run",
                            {"summary": "unsupported", "evidence_ids": ["foreign"]},
                        )
                    elif kind == "excess_usage":
                        response = reply(
                            "execute_operation", {"operation_id": "getPet"}
                        )
                    elif kind == "correction" and len(payloads) == 1:
                        response = reply("unknown_tool", {})
                    else:
                        response = normal(payload)
                    if kind != "unknown" and case.get("usage", True):
                        output = 400 if kind == "excess_usage" else 10
                        response["usage"] = {
                            "prompt_tokens": 100,
                            "completion_tokens": output,
                            "total_tokens": 100 + output,
                        }
                    return response

                def event_sink(event):
                    if case.get("sink_failure") and event["kind"] == "http_prepared":
                        raise OSError("controlled_sink_failure")
                    journal(event)

                counter = (
                    (lambda payload: case.get("counter", 100))
                    if case.get("counter", 100) is not None
                    else None
                )
                app = StartAgentRun(
                    demo.target,
                    catalog,
                    limits=Limits(**case.get("limits", {})),
                    token_counter=counter,
                    clock=lambda: now[0],
                    event_sink=event_sink,
                    secrets=(secret,) if secret else (),
                    trace_sink=lambda r: save(case_dir / (r["run_id"] + ".json"), r),
                )
                run = app.start(
                    "gate-project",
                    "gate-session",
                    case["name"],
                    case.get("goal", "Check pet"),
                    model,
                )
                runs.append(
                    {
                        "case": case["name"],
                        "run": run,
                        "journal": list(demo.journal),
                        "connections": demo.connections,
                        "tokenizer": "controlled_test_double",
                    }
                )
                require(
                    run["status"] == case["status"]
                    and run["stop_reason"] == case["reason"],
                    case["name"],
                )
                require(
                    run["requests"] == len(demo.journal) == case["requests"],
                    case["name"] + "_network",
                )
                if secret:
                    require(secret not in json.dumps([run, payloads]), "secret_leak")
                    require(
                        secret
                        not in "".join(
                            p.read_text(encoding="utf-8")
                            for p in case_dir.glob("*.jsonl")
                        ),
                        "journal_secret_leak",
                    )
                if "warning" in case:
                    warning = case["warning"]
                    require(warning in run["warnings"], case["name"] + "_warning")
                    require(
                        any(
                            "budget_warning:" + warning in m["content"]
                            for m in run["history"]
                        ),
                        case["name"] + "_context",
                    )
                    require(
                        any(
                            "budget_warning:" + warning in m["content"]
                            for payload in payloads
                            for m in payload["messages"]
                        ),
                        case["name"] + "_delivered_to_model",
                    )
                require(
                    app.start(
                        "gate-project", "gate-session", case["name"], "changed", model
                    )
                    == run,
                    case["name"] + "_duplicate",
                )
                require(
                    len(demo.journal) == case["requests"],
                    case["name"] + "_duplicate_network",
                )
            if mode == "good":
                demo.reset()
                lock = json.loads(
                    (ROOT / "experiments/local_model/runtime-lock.json").read_text()
                )
                with (ROOT / "models/MiniCPM5-2B-Q4_K_M.gguf").open("rb") as weights:
                    digest = hashlib.file_digest(weights, "sha256").hexdigest()
                require(digest == lock["model"]["sha256"], "model_hash")
                live_dir = directory / "live"
                sink = JsonlJournal(live_dir)
                adapter = LocalModel(
                    os.environ.get("S05_BASE_URL", "http://127.0.0.1:18081")
                )
                app = StartAgentRun(
                    demo.target,
                    catalog,
                    event_sink=sink,
                    trace_sink=lambda r: save(live_dir / (r["run_id"] + ".json"), r),
                )
                run = app.start(
                    "gate-project",
                    "gate-session",
                    "live",
                    "Check the test pet and report the actual observation.",
                    adapter,
                )
                runs.append(
                    {
                        "case": "live",
                        "run": run,
                        "journal": list(demo.journal),
                        "model_lock": lock,
                        "tokenizer": "loaded_GGUF_runtime",
                    }
                )
                require(
                    run["status"] == "completed" and run["stop_category"] == "done",
                    "live_completion",
                )
                require(
                    run["validation"]["contract_verdict"] == "pass"
                    and run["validation"]["intent_verdict"] == "pass",
                    "live_validation",
                )
                require(run["resources"]["usage_status"] == "measured", "live_usage")
                require(
                    all(
                        s["input_tokens_preflight"] == s["usage"]["prompt_tokens"]
                        for s in run["steps"]
                    ),
                    "live_preflight",
                )
                require(len(demo.journal) == run["requests"] == 1, "live_network")
                events = [
                    json.loads(line)
                    for line in (live_dir / (run["run_id"] + ".jsonl"))
                    .read_text()
                    .splitlines()
                ]
                require(events == run["events"], "live_journal")
            require(
                forbidden.connections == 0 and forbidden.journal == [],
                "forbidden_network",
            )
        code = 0 if mode == "good" else 2
        result = {
            "part": "b",
            "result": "accepted" if code == 0 else "rejected",
            "exit_code": code,
            "runs": runs,
            "forbidden_connections": 0,
            "observer_positive_control": True,
        }
    except Exception as error:
        code = 1
        result = {
            "part": "b",
            "result": "failed",
            "exit_code": 1,
            "error_type": type(error).__name__,
            "runs": runs,
        }
    save(directory / (mode + ".json"), result)
    print("S05b", mode, "exit=" + str(code), "cases=" + str(len(runs)))
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))

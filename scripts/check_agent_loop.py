"""Good/bad приёмка loop: настоящая сеть, независимый журнал и живой LLM.

Негативный gate возвращает 2 только при доказанном отказе всех фикстур.
Ошибка подготовки, наблюдателя или утверждения даёт 1, а не ложный pass.
"""

import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]
from api_agent.agent import Limits, StartAgentRun
from api_agent.agent.model import LocalModel
from api_agent.openapi import parse_document
from demo_api.server import DemoServer
from agent_support import ObserverModel, reply


def save(path, value):
    """Сохранять один безопасный снимок; I/O failure обязан остановить gate."""
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def require(condition, label):
    if not condition:
        raise RuntimeError(label)


def actions(run):
    return [step["action"]["name"] for step in run["steps"] if step["action"]]


def main(mode):
    directory = Path(os.environ.get("S04_EVIDENCE_DIR", ROOT / "evidence/S04"))
    traces = directory / "traces"
    traces.mkdir(parents=True, exist_ok=True)
    runs = []
    try:
        catalog = parse_document(
            json.loads((ROOT / "tests/fixtures/demo_openapi.json").read_text())
        )
        with DemoServer() as demo, DemoServer() as forbidden:
            # Нулевой журнал доказывает запрет только после положительного контроля.
            control = http.client.HTTPConnection(
                "127.0.0.1", forbidden.server.server_port, timeout=3
            )
            try:
                control.request("GET", "/pets/1")
                control.getresponse().read()
            finally:
                control.close()
            require(
                forbidden.connections == 1 and len(forbidden.journal) == 1,
                "observer failed",
            )
            forbidden.reset()

            def execute(
                label, model, *, limits=None, cancel=None, clock=None, secrets=()
            ):
                kwargs = {
                    "limits": limits,
                    "secrets": secrets,
                    "trace_sink": lambda r: save(traces / (label + ".json"), r),
                }
                if clock is not None:
                    kwargs["clock"] = clock
                app = StartAgentRun(demo.target, catalog, **kwargs)
                run = app.start(
                    "project",
                    "session",
                    label,
                    "Check the test pet and report the actual observation.",
                    model,
                    cancel=cancel,
                )
                runs.append(
                    {
                        "case": label,
                        "run": run,
                        "journal": list(demo.journal),
                        "connections": demo.connections,
                    }
                )
                return app, run

            if mode == "good":
                fixture = json.loads((ROOT / "tests/gates/s04_good.json").read_text())
                for scenario, expected in fixture["scenarios"].items():
                    demo.reset()
                    demo.mode = scenario
                    _, run = execute("fake-" + scenario, ObserverModel())
                    require(actions(run) == expected, "adaptation mismatch")
                    require(
                        len(demo.journal) == run["requests"] == 1,
                        "fake HTTP evidence mismatch",
                    )
                    require(run["stop_reason"] == "finished", "fake did not finish")
                demo.reset()
                model_file = ROOT / "models/MiniCPM5-2B-Q4_K_M.gguf"
                lock = json.loads(
                    (ROOT / "experiments/local_model/runtime-lock.json").read_text()
                )
                with model_file.open("rb") as weights:
                    digest = hashlib.file_digest(weights, "sha256").hexdigest()
                require(digest == lock["model"]["sha256"], "model lock mismatch")
                adapter = LocalModel(
                    os.environ.get("S04_BASE_URL", "http://127.0.0.1:18081")
                )
                app, run = execute("live", adapter)
                execution = [
                    name for name in actions(run) if name != "inspect_operation"
                ]
                require(
                    run["status"] == "completed"
                    and execution == fixture["live_actions"],
                    "live loop failed",
                )
                require(
                    run["exchanges"][0]["response"]["json"]
                    == {"id": 1, "name": "Murka"},
                    "live response mismatch",
                )
                require(
                    run["final"]["evidence_ids"] == [run["exchanges"][0]["id"]],
                    "live evidence mismatch",
                )
                require(
                    app.start("project", "session", "live", "repeat", adapter) == run,
                    "duplicate Run",
                )
                require(len(demo.journal) == 1, "duplicate HTTP")
                result, exit_code = "accepted", 0
            else:
                fixture = json.loads((ROOT / "tests/gates/s04_bad.json").read_text())
                for case in fixture["cases"]:
                    demo.reset()
                    _, run = execute(
                        case["name"], lambda p, c=case: reply(c["tool"], c["arguments"])
                    )
                    require(
                        run["stop_reason"] == case["reason"],
                        "bad stop mismatch: " + case["name"],
                    )
                    require(
                        run["requests"] == len(demo.journal) == case["requests"],
                        "bad HTTP mismatch",
                    )
                    require(run["status"] != "completed", "bad completed")
                for reason in ("cancelled", "time_limit", "step_limit", "prompt_limit"):
                    demo.reset()
                    event = threading.Event()
                    now = [0.0]

                    def model(payload):
                        if reason == "cancelled":
                            event.set()
                        elif reason == "time_limit":
                            now[0] = 1000.0
                        return reply(
                            (
                                "inspect_operation"
                                if reason == "step_limit"
                                else "execute_operation"
                            ),
                            {"operation_id": "getPet"},
                        )

                    limits = (
                        Limits(max_steps=1)
                        if reason == "step_limit"
                        else (
                            Limits(max_prompt_bytes=1)
                            if reason == "prompt_limit"
                            else Limits()
                        )
                    )
                    _, run = execute(
                        reason, model, limits=limits, cancel=event, clock=lambda: now[0]
                    )
                    require(
                        run["stop_reason"] == reason and demo.connections == 0,
                        "budget/cancel failed",
                    )
                demo.reset()
                demo.mode = "redirect"
                demo.redirect_target = forbidden.target + "/pets/1"
                _, run = execute("redirect", ObserverModel())
                require(
                    run["exchanges"][0]["outcome"] == "redirect_denied",
                    "redirect accepted",
                )
                require(run["status"] == "partial", "redirect invented success")
                demo.reset()
                token = "CANARY-S04-SECRET"
                demo.pet["name"] = token
                _, run = execute("secret", ObserverModel(), secrets=(token,))
                require(token not in json.dumps(run), "secret leak")
                result, exit_code = "rejected", 2
            require(
                forbidden.connections == 0 and not forbidden.journal,
                "forbidden network traffic",
            )
            manifest = {
                "model_lock": lock if mode == "good" else None,
                "result": result,
                "exit_code": exit_code,
                "runs": runs,
                "forbidden_tcp": forbidden.connections,
                "observer_positive_control": True,
                "source_hashes": {
                    str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sorted((ROOT / "src/api_agent/agent").glob("*.py"))
                },
            }
            save(directory / (mode + ".json"), manifest)
            print("S04_" + mode.upper(), result, "runs=" + str(len(runs)))
            return exit_code
    except Exception as error:
        # Тексты произвольных exceptions не публикуются: они могут содержать body/secrets.
        save(
            directory / (mode + ".json"),
            {
                "result": "failed",
                "exit_code": 1,
                "error_type": type(error).__name__,
                "runs": runs,
            },
        )
        print("S04_GATE_FAILED", type(error).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))

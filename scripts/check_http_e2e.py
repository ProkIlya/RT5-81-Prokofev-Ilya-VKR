"""Приёмка первого HTTP E2E с независимыми разрешённым и запрещённым серверами."""
import hashlib
import http.client
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from api_agent.first_http import PolicyDenied, Session, load_catalog, loopback_origin, run_goal
from demo_api.server import DemoServer
from experiments.local_model.spike import collect_stream


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class LocalModel:
    """Адаптер локального llama-server с ограничениями времени и размера SSE."""
    def __init__(self, base):
        self.base, self.port = loopback_origin(base)

    def __call__(self, payload):
        started = time.monotonic()
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=90)
        first_token = None
        def mark_token():
            nonlocal first_token
            first_token = time.monotonic() - started
        try:
            connection.request("POST", "/v1/chat/completions",
                json.dumps({**payload, "stream": True}).encode(), {"Content-Type": "application/json"})
            response = connection.getresponse()
            if response.status != 200:
                raise RuntimeError(f"local runtime HTTP {response.status}")
            size = 0
            def lines():
                # Ограничиваем и строку SSE, и весь ответ. Время проверяется
                # перед чтением, включая ожидание ещё не завершённой строки.
                nonlocal size
                while True:
                    remaining = 90 - (time.monotonic() - started)
                    if remaining <= 0:
                        raise RuntimeError("LLM time budget exhausted")
                    response.fp.raw._sock.settimeout(remaining)
                    line = response.readline(65537)
                    if not line:
                        break
                    size += len(line)
                    if len(line) > 65536 or size > 1048576:
                        raise RuntimeError("LLM response budget exhausted")
                    yield line
            result = collect_stream(lines(), mark_token)
            result["_timing"] = {"ttft_seconds": first_token,
                                  "wall_seconds": time.monotonic() - started}
            return result
        finally:
            connection.close()


def bad_gate(demo, forbidden, catalog, directory):
    rejected = []
    def reject(label, function):
        try:
            function()
        except PolicyDenied as error:
            rejected.append({"case": label, "reason": str(error)})
        else:
            raise RuntimeError(f"bad fixture accepted: {label}")
    # Сначала доказываем работоспособность наблюдателя контрольным обращением,
    # затем сбрасываем его. Нулевой журнал отключённого сервера не был бы evidence.
    control = http.client.HTTPConnection("127.0.0.1", forbidden.server.server_port, timeout=3)
    try:
        control.request("GET", "/pets/1")
        control.getresponse().read()
    finally:
        control.close()
    if forbidden.connections != 1 or len(forbidden.journal) != 1:
        raise RuntimeError("forbidden-server observer not working")
    forbidden.reset()
    demo.reset()
    session = Session(demo.target, catalog)
    fixture = json.loads((ROOT / "tests/gates/first_http_bad.json").read_text())
    for action in fixture["actions"]:
        reject(action, lambda action=action: session.execute(action))
    reject("foreign port", lambda: session.check_url(forbidden.target + "/pets/1"))
    reject("foreign host", lambda: session.check_url("http://127.0.0.2:12345/pets/1"))
    reject("DNS target", lambda: Session("http://localhost:18082", catalog))
    reject("finish without exchange", lambda: session.finish({"summary": "done", "evidence_ids": ["invented"]}))
    if demo.connections != 0:
        raise RuntimeError("denied action reached allowed server")
    for extra in ({"servers": [{"url": forbidden.target}]}, {"$ref": forbidden.target + "/spec"}):
        spec = json.loads((ROOT / "tests/fixtures/demo_openapi.json").read_text())
        path = directory / "unsupported-openapi.json"
        save(path, {**spec, **extra})
        reject(extra, lambda: load_catalog(path))
    # URL источника OpenAPI должен завершиться локальным отказом без HTTP fetch.
    try:
        load_catalog(forbidden.target + "/spec")
    except (OSError, PolicyDenied):
        rejected.append({"case": "OpenAPI URL", "reason": "local fixture only"})
    else:
        raise RuntimeError("URL source accepted")
    demo.mode = "redirect"
    demo.redirect_target = forbidden.target + "/pets/1"
    exchange = session.execute({"operation_id": "getPet"})
    if exchange["outcome"] != "redirect_denied" or exchange["response"]["status"] != 302:
        raise RuntimeError("redirect not denied")
    rejected.append({"case": "foreign redirect", "reason": "redirect_denied"})
    reject("invented evidence", lambda: session.finish({"summary": "done", "evidence_ids": ["invented"]}))
    reject("second request", lambda: session.execute({"operation_id": "getPet"}))
    if forbidden.connections or forbidden.journal or len(demo.journal) != 1:
        raise RuntimeError("network boundary violated")
    return {"result": "rejected", "exit_code": 2, "rejected_cases": rejected,
            "redirect_exchange": exchange, "forbidden_connections": forbidden.connections,
            "forbidden_journal": forbidden.journal, "server_journal": demo.journal,
            "reset_log": demo.reset_log, "observer_positive_control": "passed"}


def main(mode):
    """Запустить good/bad; 0 — принят good, 2 — отклонён bad, 1 — ошибка gate."""
    directory = Path(os.environ.get("S02_EVIDENCE_DIR", ROOT / "evidence/S02"))
    directory.mkdir(parents=True, exist_ok=True)
    raw = []
    result = {"result": "failed", "exit_code": 1}
    demo, forbidden = DemoServer(), DemoServer()
    try:
        with demo, forbidden:
            catalog = load_catalog(ROOT / "tests/fixtures/demo_openapi.json")
            if mode == "bad":
                result = bad_gate(demo, forbidden, catalog, directory)
            else:
                demo.reset()
                fixture = json.loads((ROOT / "tests/gates/first_http_good.json").read_text(encoding="utf-8"))
                lock = json.loads((ROOT / "experiments/local_model/runtime-lock.json").read_text())
                weight = ROOT / "models" / lock["model"]["file"]
                with weight.open("rb") as stream:
                    sha = hashlib.file_digest(stream, "sha256").hexdigest()
                if sha != lock["model"]["sha256"]:
                    raise RuntimeError("model SHA mismatch")
                session = Session(demo.target, catalog, run_seconds=180)
                trace = run_goal(fixture["goal"], session,
                    LocalModel(os.environ.get("S02_LLM_BASE_URL", "http://127.0.0.1:18081")), raw=raw)
                exchange = trace["exchanges"][0]
                if (demo.journal != fixture["expected_journal"] or demo.connections != 1
                        or trace["status"] != "completed"
                        or exchange["response"]["status"] != fixture["expected_status"]
                        or exchange["response"]["json"] != fixture["expected_json"]
                        or trace["final"]["evidence_ids"] != [exchange["id"]]):
                    raise RuntimeError("good postconditions not met")
                # Oracle — ожидаемые данные fixture и независимый журнал сервера.
                # Текст итогового сообщения модели не заменяет эти проверки.
                result = {"result": "accepted", "exit_code": 0, "trace": trace,
                    "model_sha256": sha, "runtime_lock": lock, "raw_model_exchanges": raw,
                    "server_journal": demo.journal, "server_connections": demo.connections,
                    "reset_log": demo.reset_log}
                save(directory / "trace.json", trace)
    except Exception as error:
        result = {"result": "failed", "exit_code": 1, "error": f"{type(error).__name__}: {error}",
                  "raw_model_exchanges": raw, "server_journal": demo.journal,
                  "forbidden_journal": forbidden.journal, "forbidden_connections": forbidden.connections}
    result["code_ref"] = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
        capture_output=True, text=True, check=True).stdout.strip()
    result["python"] = sys.version
    # HEAD ещё может быть базовым commit при проверке незакоммиченного кода.
    # Хеши исходников точно идентифицируют проверенную рабочую версию.
    sources = ["src/api_agent/first_http.py", "demo_api/server.py", "scripts/check_http_e2e.py",
               "scripts/check_stage.py", "tests/fixtures/demo_openapi.json",
               "tests/gates/first_http_good.json", "tests/gates/first_http_bad.json"]
    result["source_sha256"] = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in sources}
    save(directory / f"{mode}.json", result)
    save(directory / f"{mode}-server-journal.json", {"allowed": demo.journal,
        "forbidden": forbidden.journal, "forbidden_connections": forbidden.connections})
    print(f"S02_{mode.upper()}_{result['result'].upper()}", result.get("error", ""))
    return result["exit_code"]

"""Живые SQLite gates: реальные локальные LLM/HTTP, затем read-only рестарт."""

import hashlib
import json
import os
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.agent.model import LocalModel
from api_agent.openapi import load_source
from api_agent.storage import SQLiteHistory
from demo_api.server import DemoServer


def main(part, mode):
    directory = Path(os.environ.get("S08_EVIDENCE_DIR", ROOT / "evidence/S08")) / ("S08" + part + "-" + mode + "-" + uuid.uuid4().hex)
    directory.mkdir(parents=True)
    fixture = json.loads((ROOT / "tests/gates" / ("s08" + part + "_good.json")).read_text(encoding="utf-8"))
    result = {"stage": "S08" + part, "mode": mode, "fixture": fixture, "exit_code": 1}
    try:
        catalog = load_source(str(ROOT / "tests/fixtures/demo_openapi.json"))
        database = directory / "history.sqlite3"
        with DemoServer() as server:
            runs = []
            scopes = [("project", s) for s in fixture["sessions"]] if part == "a" else [(p, fixture["session"]) for p in fixture["projects"]]
            with SQLiteHistory(database) as history:
                app = StartAgentRun(server.target, catalog, history_store=history,
                                    trace_sink=lambda r: (directory / (r['run_id'] + '.json')).write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding='utf-8'))
                for index, (project, session) in enumerate(scopes):
                    goal = fixture.get("goals", [fixture["goal"]] * len(scopes))[index]
                    run = app.start(project, session, "original-" + str(index), goal, LocalModel("http://127.0.0.1:18081"))
                    if run["status"] != fixture["expected_status"]:
                        raise ValueError("live_run_not_completed")
                    runs.append(run)
            before = len(server.journal)
            if before != fixture["expected_requests"]:
                raise ValueError("unexpected_request_count")
            with SQLiteHistory(database, read_only=True) as history:
                if mode == "good":
                    for scope, run in zip(scopes, runs):
                        restored = history.run_for_message(*scope, run["response_message_id"])
                        if restored["run_id"] != run["run_id"] or restored["goal"] != run["goal"] or restored["exchanges"] != run["exchanges"] or restored["contract"] != catalog.snapshot:
                            raise ValueError("restored_run_mismatch")
                        if history.episode(*scope, run["run_id"]) is None:
                            raise ValueError("missing_episode")
                else:
                    result["bad_fixture"] = json.loads((ROOT / "tests/gates" / ("s08" + part + "_bad.json")).read_text(encoding="utf-8"))
                    if part == "a":
                        restored = history.run_for_message(*scopes[0], runs[0]["response_message_id"])
                        if restored["run_id"] == runs[1]["run_id"]:
                            raise ValueError("bad_fixture_accepted")
                        result["rejection"] = "answer_run_binding_mismatch"
                    else:
                        try:
                            history.read_run(*scopes[1], runs[0]["run_id"])
                        except ValueError as error:
                            result["rejection"] = str(error)
                        else:
                            raise ValueError("bad_fixture_accepted")
                result["sql"] = {name: history.db.execute("SELECT count(*) FROM " + name).fetchone()[0] for name in ("projects", "sessions", "messages", "runs", "events", "episodes", "snapshots")}
                if history.db.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("dangling_foreign_key")
            after = len(server.journal)
            if before != after:
                raise ValueError("history_repeated_http")
            result.update(exit_code=0 if mode == "good" else 2,
                          scopes=scopes, run_ids=[r["run_id"] for r in runs],
                          response_message_ids=[r["response_message_id"] for r in runs],
                          request_count_before=before, request_count_after=after,
                          server_journal=server.journal)
    except Exception as error:
        result["error"] = {"type": type(error).__name__, "code": str(error) if isinstance(error, ValueError) else "gate_execution_failed"}
    sources = set((ROOT / "src").rglob("*.py")) | set((ROOT / "src").rglob("*.sql")) | set((ROOT / "tests/gates").glob("s08*.json")) | {Path(__file__), ROOT / "scripts/check_stage.py"}
    result["source_hashes"] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(sources)}
    (directory / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"stage": result["stage"], "mode": mode, "exit_code": result["exit_code"], "evidence": str(directory)}, ensure_ascii=False))
    return result["exit_code"]

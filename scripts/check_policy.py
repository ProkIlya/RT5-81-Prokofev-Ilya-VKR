"""Машинный gate policy: настоящий HTTP, отдельный server journal, live LLM."""

import json
import os
import sys
import uuid
import hashlib
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.agent.chains import CreateRead
from api_agent.agent.model import LocalModel
from api_agent.agent.resilience import JsonlJournal
from api_agent.policy import ProjectConfig, PolicyWorkflow, PolicySession
from api_agent.openapi import load_source
from demo_api.server import DemoServer
from test_runtime_chain import CreateReadModel
from agent_support import count_test_tokens


def main(part, mode):
    directory = (
        Path(os.environ.get("S07_EVIDENCE_DIR", ROOT / "evidence/S07"))
        / part
        / mode
        / uuid.uuid4().hex
    )
    directory.mkdir(parents=True, exist_ok=True)
    records = []

    def execute(label, policy_mode, risk="C", approved=None, live=False, strategy=None):
        with DemoServer() as server:
            catalog = load_source(str(ROOT / "tests/fixtures/chain_openapi.json"))
            config = ProjectConfig.create(
                "gate-project",
                server.target,
                catalog,
                mode=policy_mode,
                resettable=True,
                operations={"createOrder": risk, "readOrder": "A"},
            )
            current = [config]
            cancel = threading.Event()

            def approval(proposal):
                assert server.connections == 0
                if strategy == "cancel":
                    cancel.set()
                if strategy == "revoke":
                    current[0] = ProjectConfig.create(
                        "gate-project",
                        server.target,
                        catalog,
                        mode=policy_mode,
                        resettable=True,
                        version=2,
                        operations={"readOrder": "A"},
                    )
                return {
                    "action_hash": (
                        "stale" if strategy == "stale" else proposal["action_hash"]
                    ),
                    "config_version": proposal["config_version"],
                    "approved": approved,
                }

            def sink(run):
                (directory / (label + "-run.json")).write_text(
                    json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
                )

            workflow = PolicyWorkflow(
                CreateRead(),
                config,
                approval=approval if approved is not None else None,
                current_config=lambda: current[0],
            )
            app = StartAgentRun(
                server.target,
                catalog,
                workflow=workflow,
                trace_sink=sink,
                event_sink=JsonlJournal(directory),
                token_counter=None if live else count_test_tokens,
            )
            run = app.start(
                "gate-project",
                "session",
                uuid.uuid4().hex,
                "Create and read order",
                LocalModel("http://127.0.0.1:18081") if live else CreateReadModel(),
                cancel=cancel,
            )
            record = {
                "case": label,
                "live_model": live,
                "status": run["status"],
                "reason": run["stop_reason"],
                "requests": run["requests"],
                "connections": server.connections,
                "server_journal": server.journal,
                "decisions": run.get("policy_decisions"),
                "config": config.snapshot,
                "resources": run["resources"],
            }
            records.append(record)
            return run, server

    try:
        if part == "b":
            if mode == "good":
                run, server = execute("live-safe", "SAFE", approved=True, live=True)
                assert run["status"] == "completed" and len(server.journal) == 2
            for strategy in ("stale", "cancel", "revoke"):
                run, server = execute(
                    strategy, "SAFE", approved=True, strategy=strategy
                )
                assert (
                    run["status"] != "completed"
                    and server.connections == 0
                    and not server.journal
                )
            with DemoServer() as server:
                from api_agent.agent.chains import ChainLimits

                server.mode = "timeout_after_commit"
                catalog = load_source(str(ROOT / "tests/fixtures/chain_openapi.json"))
                config = ProjectConfig.create(
                    "gate-project",
                    server.target,
                    catalog,
                    mode="TEST",
                    resettable=True,
                    operations={"createOrder": "C", "readOrder": "A"},
                )
                session_run = {"run_id": uuid.uuid4().hex, "project_id": "gate-project"}
                session = PolicySession(
                    server.target,
                    catalog,
                    PolicyWorkflow(CreateRead(), config),
                    session_run,
                    ChainLimits(),
                )
                session.timeout = 0.1
                exchange = session.execute(
                    {"operation_id": "createOrder", "body": {"name": "A"}}
                )
                assert (
                    exchange["effect_status"] == "uncertain" and len(server.orders) == 1
                )
                try:
                    session.execute(
                        {"operation_id": "createOrder", "body": {"name": "B"}}
                    )
                except ValueError:
                    pass
                else:
                    raise AssertionError("blind_retry")
                assert len(server.journal) == 1
                records.append(
                    {
                        "case": "uncertain-write",
                        "exchange": exchange,
                        "server_journal": server.journal,
                    }
                )
            code = 0 if mode == "good" else 2
        elif part != "a":
            raise ValueError("unsupported_gate_part")
        elif mode == "good":
            for policy_mode, approved in (
                ("READ_ONLY", None),
                ("SAFE", False),
                ("SAFE", True),
                ("TEST", None),
            ):
                run, server = execute(
                    policy_mode + str(approved), policy_mode, approved=approved
                )
                allowed = policy_mode == "TEST" or approved is True
                assert (run["status"] == "completed") == allowed
                assert len(server.journal) == (2 if allowed else 0)
                assert allowed or server.connections == 0
            run, server = execute("live-test", "TEST", live=True)
            assert (
                run["status"] == "completed"
                and run["requests"] == 2
                and len(server.journal) == 2
            )
            assert run["resources"]["usage_status"] == "measured"
            code = 0
        elif mode == "bad":
            for policy_mode, risk, approved in (
                ("READ_ONLY", "C", None),
                ("SAFE", "C", False),
                ("TEST", "D", None),
                ("TEST", "E", False),
            ):
                run, server = execute(
                    policy_mode + risk + str(approved), policy_mode, risk, approved
                )
                assert (
                    run["status"] != "completed"
                    and server.connections == 0
                    and not server.journal
                )
            code = 2
        else:
            raise ValueError("invalid_mode")
    except Exception as error:
        records.append({"setup_or_assertion_error": type(error).__name__})
        code = 1
    sources = (
        sorted((ROOT / "src").rglob("*.py"))
        + sorted((ROOT / "scripts").glob("*.py"))
        + sorted((ROOT / "tests").rglob("*.py"))
        + sorted((ROOT / "tests/fixtures").glob("*.json"))
    )
    manifest = {
        "stage": "S07" + part,
        "mode": mode,
        "exit_code": code,
        "argv": sys.argv,
        "python": sys.executable,
        "cwd": str(Path.cwd()),
        "records": records,
        "hashes": {
            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sources
        },
    }
    (directory / "verification.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "stage": "S07" + part,
                "mode": mode,
                "exit_code": code,
                "evidence": str(directory),
            },
            ensure_ascii=False,
        )
    )
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))

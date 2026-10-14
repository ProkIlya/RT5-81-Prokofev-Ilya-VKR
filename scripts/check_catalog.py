"""Gate общего каталога: разные operation IDs и реальные модель/HTTP/validator."""

import hashlib
import json
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.agent.model import LocalModel
from api_agent.agent.resilience import JsonlJournal
from api_agent.policy import ProjectConfig
from api_agent.openapi import parse_document
from demo_api.server import DemoServer
from test_catalog_execution import catalog_variant, status_condition
from agent_support import reply, count_test_tokens


def main(part, mode):
    directory = (
        Path(os.environ.get("S07_EVIDENCE_DIR", ROOT / "evidence/S07"))
        / part
        / mode
        / uuid.uuid4().hex
    )
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    try:
        if part == "d":
            from test_catalog_discovery import hundred_catalog

            with DemoServer() as server:
                cat = hundred_catalog()
                cfg = ProjectConfig.create(
                    "gate-project",
                    server.target,
                    cat,
                    mode="READ_ONLY",
                    resettable=True,
                    operations={n: "A" for n in cat.operations},
                )
                goal = "Найди и проверь питомца Мурку. Ничего не изменяй."
                wf = CatalogWorkflow(
                    cat,
                    cfg,
                    goal=goal,
                    conditions={
                        "z_pet_observation": [
                            status_condition(200),
                            {
                                "predicate": "json_value_equals",
                                "parameters": {"path": "/name", "value": "Murka"},
                                "intent": "positive",
                                "preconditions": "verified",
                            },
                        ]
                    },
                )

                def sink(run):
                    (directory / "catalog100-run.json").write_text(
                        json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
                    )

                if mode == "good":
                    run = StartAgentRun(
                        server.target,
                        cat,
                        workflow=wf,
                        trace_sink=sink,
                        event_sink=JsonlJournal(directory),
                    ).start(
                        "gate-project",
                        "s",
                        uuid.uuid4().hex,
                        goal,
                        LocalModel("http://127.0.0.1:18081"),
                    )
                    records.append(
                        {
                            "case": "live-catalog100",
                            "status": run["status"],
                            "reason": run["stop_reason"],
                            "contract_verdict": run["report"]["contract_verdict"],
                            "intent_verdict": run["report"]["intent_verdict"],
                            "chain_verdict": run["chain_verdict"],
                            "server_journal": server.journal,
                            "selection": run["selection_manifest"],
                            "pages": run.get("discovery_pages"),
                            "resources": run["resources"],
                            "actions": [step.get("action") for step in run["steps"]],
                        }
                    )
                    assert run["status"] == "completed" and len(cat.operations) == 100
                    # Completed означает окончание проверки, включая найденный
                    # дефект API. Good обязан доказать успех всех своих oracle.
                    assert run["report"]["contract_verdict"] == "pass"
                    assert run["report"]["intent_verdict"] == "pass"
                    assert run["chain_verdict"] == "pass"
                    assert (
                        "z_pet_observation"
                        not in run["selection_manifest"]["selected_ids"]
                    )
                    names = [
                        step["action"]["name"]
                        for step in run["steps"]
                        if step.get("action")
                    ]
                    assert all(
                        n in names
                        for n in (
                            "search_operations",
                            "inspect_operation",
                            "execute_operation",
                            "finish_run",
                        )
                    )
                    assert server.journal == [{"method": "GET", "path": "/pets/1"}]
                    assert run["resources"]["usage_status"] == "measured"
                    code = 0
                elif mode == "bad":
                    first = wf.discovery("own", {"query": ""})
                    for run_id, args in (
                        ("other", {"query": "", "cursor": first["cursor"]}),
                        ("own", {"query": "pets", "cursor": first["cursor"]}),
                        ("own", {"query": "", "cursor": "forged"}),
                    ):
                        try:
                            wf.discovery(run_id, args)
                        except ValueError:
                            records.append({"case": "cursor-denied", "run_id": run_id})
                        else:
                            raise AssertionError("cursor_scope_bypass")
                    run = StartAgentRun(
                        server.target,
                        cat,
                        workflow=wf,
                        token_counter=lambda p: wf.default_limits().max_context_tokens
                        + 100,
                        trace_sink=sink,
                    ).start(
                        "gate-project",
                        "s",
                        "budget",
                        goal,
                        lambda p: (_ for _ in ()).throw(AssertionError("model_called")),
                    )
                    assert (
                        run["stop_reason"] == "context_limit"
                        and server.connections == 0
                        and run["model_calls"] == 0
                    )
                    records.append(
                        {
                            "case": "context-preflight",
                            "status": run["status"],
                            "reason": run["stop_reason"],
                            "connections": server.connections,
                        }
                    )
                    limited_cfg = ProjectConfig.create(
                        "gate-project",
                        server.target,
                        cat,
                        mode="READ_ONLY",
                        resettable=True,
                        operations={"z_pet_observation": "A"},
                    )
                    limited = CatalogWorkflow(
                        cat,
                        limited_cfg,
                        goal=goal,
                        conditions={"z_pet_observation": [status_condition(200)]},
                    )
                    assert (
                        limited.discovery("limited", {"query": "item000"})["total"] == 0
                    )
                    run = StartAgentRun(
                        server.target,
                        cat,
                        workflow=limited,
                        token_counter=count_test_tokens,
                    ).start(
                        "gate-project",
                        "s",
                        "forbidden",
                        goal,
                        lambda p: reply(
                            "execute_operation", {"operation_id": "item000"}
                        ),
                    )
                    assert run["status"] != "completed" and server.connections == 0
                    records.append(
                        {
                            "case": "denied-operation-search-execute",
                            "server_journal": server.journal,
                            "connections": server.connections,
                        }
                    )
                    code = 2
                else:
                    raise ValueError("invalid_gate_mode")
        elif part != "c":
            raise ValueError("unsupported_gate_part")
        elif mode == "good":
            for prefix in ("alpha", "beta"):
                with DemoServer() as server:
                    cat = catalog_variant(prefix)
                    create, read = prefix + "createOrder", prefix + "readOrder"
                    cfg = ProjectConfig.create(
                        "gate-project",
                        server.target,
                        cat,
                        mode="TEST",
                        resettable=True,
                        operations={create: "C", read: "A"},
                    )
                    cfg.save(directory / (prefix + "-project.json"))
                    wf = CatalogWorkflow(
                        cat,
                        cfg,
                        goal="Создай заявку A и проверь её чтение",
                        conditions={
                            create: [status_condition(201)],
                            read: [status_condition(200)],
                        },
                        extraction={
                            create: {"/id": {"type": "integer", "secret": False}}
                        },
                        binding_rules={
                            read: {
                                "order_id": {
                                    "source_operation": create,
                                    "source_path": "/id",
                                    "response_path": "/id",
                                }
                            }
                        },
                    )

                    def sink(run):
                        (directory / (prefix + "-run.json")).write_text(
                            json.dumps(run, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )

                    run = StartAgentRun(
                        server.target,
                        cat,
                        workflow=wf,
                        trace_sink=sink,
                        event_sink=JsonlJournal(directory),
                    ).start(
                        "gate-project",
                        "s",
                        uuid.uuid4().hex,
                        "Создай заявку A и проверь её чтение",
                        LocalModel("http://127.0.0.1:18081"),
                    )
                    records.append(
                        {
                            "case": prefix,
                            "status": run["status"],
                            "reason": run["stop_reason"],
                            "server_journal": server.journal,
                            "run_catalog": run["run_catalog"],
                            "resources": run["resources"],
                        }
                    )
                    assert (
                        run["status"] == "completed"
                        and len(server.journal) == 2
                        and run["resources"]["usage_status"] == "measured"
                    )
                    assert server.journal[-1]["path"] == "/orders/" + str(
                        server.journal[0]["created_id"]
                    )
            code = 0
        elif mode == "bad":
            for attack in (
                {"headers": {"Authorization": "Bearer unsafe"}},
                {"parameters": {"foreign": 1}},
                {"bindings": {"order_id": "foreign"}},
                {"operation_id": "unknown"},
            ):
                with DemoServer() as server:
                    cat = catalog_variant("bad")
                    create, read = "badcreateOrder", "badreadOrder"
                    cfg = ProjectConfig.create(
                        "gate-project",
                        server.target,
                        cat,
                        mode="TEST",
                        resettable=True,
                        operations={create: "C", read: "A"},
                    )
                    wf = CatalogWorkflow(
                        cat,
                        cfg,
                        goal="Проверь чтение",
                        conditions={read: [status_condition(200)]},
                    )
                    args = {"operation_id": read, **attack}
                    run = StartAgentRun(
                        server.target, cat, workflow=wf, token_counter=count_test_tokens
                    ).start(
                        "gate-project",
                        "s",
                        uuid.uuid4().hex,
                        "Проверь чтение",
                        lambda p: reply("execute_operation", args),
                    )
                    records.append(
                        {
                            "attack": attack,
                            "status": run["status"],
                            "connections": server.connections,
                            "server_journal": server.journal,
                        }
                    )
                    assert (
                        run["status"] != "completed"
                        and not server.journal
                        and server.connections == 0
                    )
            code = 2
        else:
            raise ValueError("invalid_gate_mode")
    except Exception as error:
        records.append({"setup_or_assertion_error": type(error).__name__})
        code = 1
    sources = (
        sorted((ROOT / "src").rglob("*.py"))
        + sorted((ROOT / "scripts").glob("*.py"))
        + sorted((ROOT / "tests").rglob("*.py"))
    )
    (directory / "verification.json").write_text(
        json.dumps(
            {
                "stage": "S07" + part,
                "mode": mode,
                "exit_code": code,
                "python": sys.executable,
                "argv": sys.argv,
                "cwd": str(Path.cwd()),
                "records": records,
                "hashes": {
                    str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in sources
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
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

"""Gate зависимой цепочки: verdict и сетевой журнал проверяет код, не модель."""

import json, os, sys, uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]
from api_agent.agent import StartAgentRun
from api_agent.agent.chains import CreateRead, AuthRead
from api_agent.agent.model import LocalModel
from api_agent.agent.resilience import JsonlJournal
from api_agent.openapi import load_source
from demo_api.server import DemoServer
from test_runtime_chain import CreateReadModel
from test_runtime_auth import AuthModel
from agent_support import count_test_tokens


def main(part, mode):
    directory = (
        Path(os.environ.get("S06_EVIDENCE_DIR", ROOT / "evidence/S06"))
        / part
        / mode
        / uuid.uuid4().hex
    )
    directory.mkdir(parents=True, exist_ok=True)
    records = []

    def execute(name, model, **options):
        with DemoServer() as server:
            server.reset()
            server.mode = options.pop("mode", "good")

            def sink(run):
                rendered = json.dumps(run, ensure_ascii=False, indent=2)
                if part == "b" and (
                    server.auth_token in rendered or "fixture-password" in rendered
                ):
                    raise ValueError("secret_in_trace")
                (directory / (name + "-" + run["run_id"] + ".json")).write_text(
                    rendered, encoding="utf-8"
                )

            fixture = "chain_openapi.json" if part == "a" else "auth_openapi.json"
            app = StartAgentRun(
                server.target,
                load_source(str(ROOT / "tests/fixtures" / fixture)),
                workflow=CreateRead() if part == "a" else AuthRead(),
                trace_sink=sink,
                event_sink=JsonlJournal(directory),
                **options
            )
            run = app.start(
                "gate-project",
                "gate-session",
                uuid.uuid4().hex,
                "Create and read order" if part == "a" else "Login and read profile",
                model,
            )
            record = {
                "name": name,
                "status": run["status"],
                "stop_reason": run["stop_reason"],
                "chain_verdict": run["chain_verdict"],
                "requests": run["requests"],
                "journal": server.journal,
                "run_id": run["run_id"],
            }
            if part == "b":
                token = server.auth_token
                assert token not in json.dumps(run, ensure_ascii=False)
                assert "fixture-password" not in json.dumps(run, ensure_ascii=False)
                record["credential_absent_from_run"] = True
            records.append(record)
            return run, server

    code = 1
    try:
        if part not in ("a", "b"):
            raise ValueError("part_not_implemented")
        if part == "b":
            if mode == "good":
                run, server = execute(
                    "auth-observer", AuthModel(), token_counter=count_test_tokens
                )
                assert (
                    run["status"] == "completed"
                    and run["chain_verdict"] == "pass"
                    and server.journal[-1]["auth_matched"]
                )
                run, server = execute("auth-live", LocalModel("http://127.0.0.1:18081"))
                assert (
                    run["status"] == "completed"
                    and run["chain_verdict"] == "pass"
                    and server.journal[-1]["auth_matched"]
                )
                code = 0
            else:
                run, server = execute(
                    "foreign-credential",
                    AuthModel(stolen="foreign-ref"),
                    token_counter=count_test_tokens,
                )
                assert run["status"] != "completed" and len(server.journal) == 1
                run, server = execute(
                    "wrong-profile-id",
                    AuthModel(),
                    mode="state_id_mismatch",
                    token_counter=count_test_tokens,
                )
                assert (
                    run["chain_verdict"] == "fail"
                    and server.journal[-1]["auth_matched"]
                )
                code = 2
        elif mode == "good":
            run, server = execute(
                "observer", CreateReadModel(two=True), token_counter=count_test_tokens
            )
            assert run["status"] == "completed" and run["chain_verdict"] == "pass"
            assert server.journal[-1]["path"] == "/orders/" + str(
                server.journal[0]["created_id"]
            )
            run, server = execute("live", LocalModel("http://127.0.0.1:18081"))
            assert (
                run["status"] == "completed"
                and run["chain_verdict"] == "pass"
                and run["requests"] == 2
            )
            assert server.journal[-1]["path"] == "/orders/" + str(
                server.journal[0]["created_id"]
            )
            code = 0
        else:
            run, server = execute(
                "invented-ref",
                CreateReadModel(invented=True),
                token_counter=count_test_tokens,
            )
            assert run["status"] != "completed" and len(server.journal) == 1
            run, server = execute(
                "wrong-response-id",
                CreateReadModel(),
                mode="state_id_mismatch",
                token_counter=count_test_tokens,
            )
            assert (
                run["chain_verdict"] == "fail"
                and run["report"]["contract_verdict"] == "pass"
            )
            code = 2
    except Exception as error:
        records.append({"error_type": type(error).__name__, "code": "gate_failed"})
    (directory / "verification.json").write_text(
        json.dumps(
            {"part": part, "mode": mode, "exit_code": code, "records": records},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("S06" + part, mode, "exit=" + str(code), "evidence=" + str(directory))
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))

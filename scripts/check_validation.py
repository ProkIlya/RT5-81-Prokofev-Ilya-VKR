"""Приёмка S05a: хороший контроль и отклонение намеренно плохих ответов.

Негативный запуск возвращает 2 только при обнаружении каждого нарушения;
принятие плохой фикстуры, ошибка подготовки или сохранения возвращают 1.
"""

import json
import os
from pathlib import Path
import sys
from contextlib import nullcontext
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]
from api_agent.agent import StartAgentRun
from api_agent.openapi import parse_document
from agent_support import count_test_tokens, ObserverModel, reply
from demo_api.server import DemoServer


def require(condition, label):
    if not condition:
        raise RuntimeError(label)


def main(mode):
    directory = Path(os.environ.get("S05_EVIDENCE_DIR", ROOT / "evidence/S05")) / "a"
    directory.mkdir(parents=True, exist_ok=True)
    runs = []
    try:
        fixture = json.loads((ROOT / "tests/gates/s05_validation.json").read_text())
        catalog = parse_document(
            json.loads((ROOT / "tests/fixtures/demo_openapi.json").read_text())
        )
        with DemoServer() as demo:
            for case in fixture[mode]:
                demo.reset()
                demo.mode = case["mode"]
                app = StartAgentRun(
                    demo.target,
                    catalog,
                    token_counter=count_test_tokens,
                    trace_sink=lambda r: (
                        directory / (case["name"] + ".json")
                    ).write_text(
                        json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8"
                    ),
                )
                conditions = [case["condition"]] if "condition" in case else None
                if conditions and conditions[0]["source"] == "CONTRACT":
                    basis = conditions[0]["basis"]
                    if basis.get("operation_hash") == "$CURRENT":
                        basis["operation_hash"] = catalog.operation_hashes["getPet"]
                    if basis.get("spec_hash") == "$CURRENT":
                        basis["spec_hash"] = catalog.spec_hash
                observer = ObserverModel()

                def model(payload):
                    if case.get("mutate_condition"):
                        conditions[0]["parameters"]["value"] = "Murka"
                    if case.get("fault") == "foreign_evidence" and any(
                        m["role"] == "tool" for m in payload["messages"]
                    ):
                        return reply(
                            "finish_run",
                            {"summary": "done", "evidence_ids": ["foreign"]},
                        )
                    return observer(payload)

                boundary = (
                    patch(
                        "api_agent.agent.runtime.validate_exchange", return_value=None
                    )
                    if case.get("fault") == "missing_validator"
                    else nullcontext()
                )
                with boundary:
                    run = app.start(
                        "gate-project",
                        "gate-session",
                        case["name"],
                        "Check pet",
                        model,
                        conditions=conditions,
                    )
                runs.append(
                    {
                        "case": case["name"],
                        "run": run,
                        "journal": list(demo.journal),
                        "connections": demo.connections,
                    }
                )
                require(run["status"] == case["status"], case["name"])
                if "contract" in case:
                    require(
                        run["validation"]["contract_verdict"] == case["contract"],
                        case["name"],
                    )
                    require(
                        run["validation"]["intent_verdict"] == case["intent"],
                        case["name"],
                    )
                if "confirmed" in case:
                    require(
                        run["validation"]["confirmed_contract_violations"]
                        == case["confirmed"],
                        case["name"],
                    )
                if case.get("mutate_condition"):
                    require(
                        run["expectations"][0]["parameters"]["value"] == "Milk",
                        case["name"],
                    )
                if run.get("final"):
                    require(
                        run["final"]["summary"] == run["report"]["summary"],
                        case["name"],
                    )
                if "path" in case:
                    require(
                        any(
                            i["path"] == case["path"]
                            for i in run["validation"]["issues"]
                        ),
                        case["name"],
                    )
                require(
                    len(demo.journal) == run["requests"] == case.get("requests", 1),
                    case["name"],
                )
        code = 0 if mode == "good" else 2
        result = {
            "part": "a",
            "result": "accepted" if code == 0 else "rejected",
            "exit_code": code,
            "runs": runs,
        }
    except Exception as error:
        code = 1
        result = {
            "part": "a",
            "result": "failed",
            "exit_code": 1,
            "error_type": type(error).__name__,
            "runs": runs,
        }
    (directory / (mode + ".json")).write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("S05a", mode, "exit=" + str(code), "cases=" + str(len(runs)))
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))

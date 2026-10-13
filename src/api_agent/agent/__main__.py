"""CLI раннего прототипа: доверенные target/spec и цель пользователя.

История пока файловая; новый процесс создаёт новый in-memory application.
Постоянная дедупликация сообщений появится вместе с SQLite в S08.
"""

import argparse
import json
from pathlib import Path
import sys
import uuid

from . import StartAgentRun
from .model import LocalModel
from .resilience import JsonlJournal
from ..openapi import load_source


def main(argv=None, *, model=None, token_counter=None):
    """Передать команду StartAgentRun и показать безопасный итог без нового HTTP."""
    parser = argparse.ArgumentParser(description="Run restricted local API agent")
    parser.add_argument("goal")
    parser.add_argument("--workflow", choices=("create-read", "auth-read"))
    parser.add_argument(
        "--credentials-file",
        help="Локальный JSON login/password; значения не передаются в argv",
    )
    parser.add_argument("--target", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--llm-target", default="http://127.0.0.1:18081")
    parser.add_argument("--evidence-dir", default="evidence/agent")
    parser.add_argument(
        "--conditions",
        help="JSON файл явных условий пользователя: predicate, parameters, intent, preconditions",
    )
    args = parser.parse_args(argv)
    try:
        catalog = load_source(args.spec, target=args.target)
        directory = Path(args.evidence_dir)
        directory.mkdir(parents=True, exist_ok=True)

        def sink(run):
            path = directory / (run["run_id"] + ".json")
            path.write_text(
                json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        from .chains import CreateRead, AuthRead

        workflow = None
        if args.workflow == "create-read":
            workflow = CreateRead()
        elif args.workflow == "auth-read":
            credentials = {"login": "fixture", "password": "fixture-password"}
            if args.credentials_file:
                credential_path = Path(args.credentials_file)
                if credential_path.stat().st_size > 16384:
                    raise ValueError("credentials_file_limit")
                credentials = json.loads(
                    credential_path.read_text(encoding="utf-8-sig")
                )
                if (
                    not isinstance(credentials, dict)
                    or set(credentials) != {"login", "password"}
                    or any(
                        not isinstance(v, str) or not v for v in credentials.values()
                    )
                ):
                    raise ValueError("invalid_credentials")
            workflow = AuthRead(
                account=credentials["login"], password=credentials["password"]
            )
        if args.credentials_file and args.workflow != "auth-read":
            raise ValueError("credentials_workflow_required")
        message_id = str(uuid.uuid4())
        conditions = None
        if args.conditions:
            descriptors = json.loads(Path(args.conditions).read_text(encoding="utf-8"))
            if not isinstance(descriptors, list) or not 1 <= len(descriptors) <= 7:
                raise ValueError("invalid_conditions")
            conditions = []
            for descriptor in descriptors:
                if not isinstance(descriptor, dict) or set(descriptor) != {
                    "predicate",
                    "parameters",
                    "intent",
                    "preconditions",
                }:
                    raise ValueError("invalid_condition")
                conditions.append(
                    {
                        **descriptor,
                        "source": "USER_DEFINED",
                        "basis": {"message_id": message_id},
                    }
                )
        app = StartAgentRun(
            args.target,
            catalog,
            workflow=workflow,
            trace_sink=sink,
            token_counter=token_counter,
            event_sink=JsonlJournal(directory),
        )
        run = app.start(
            "cli-project-" + uuid.uuid4().hex if workflow else "cli-project",
            "cli-session-" + uuid.uuid4().hex if workflow else "cli-session",
            message_id,
            args.goal,
            model if model is not None else LocalModel(args.llm_target),
            conditions=conditions,
        )
        print(
            json.dumps(
                {
                    "run_id": run["run_id"],
                    "status": run["status"],
                    "stop_reason": run["stop_reason"],
                    "requests": run["requests"],
                    "trace": str(directory / (run["run_id"] + ".json")),
                    "journal": str(directory / (run["run_id"] + ".jsonl")),
                    "summary": run["report"]["summary"],
                    "contract_verdict": run["report"]["contract_verdict"],
                    "intent_verdict": run["report"]["intent_verdict"],
                    **({"chain_verdict": run["chain_verdict"]} if workflow else {}),
                    "issues": run["report"]["issues"],
                    "issues_total": run["report"]["issues_total"],
                    "issue_details_truncated": run["report"]["issue_details_truncated"],
                    "intent_results": run["report"]["intent_results"],
                    "completion_scope": "supported_checks_only",
                },
                ensure_ascii=False,
            )
        )
        return 0 if run["status"] == "completed" else 2
    except Exception as error:
        print(
            json.dumps({"code": "agent_setup_failed", "type": type(error).__name__}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI раннего прототипа: доверенные target/spec и цель пользователя.

SQLite сохраняет связи после рестарта; JSON/JSONL остаются экспортом evidence.
"""

import argparse
import json
from pathlib import Path
import sys
import uuid
import signal
import threading

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
    parser.add_argument("--model-name", default="MiniCPM5-2B-Q4_K_M")
    parser.add_argument('--review-summary',action='store_true',help='One isolated advisory consistency check after the agent finishes; no additional API actions')
    parser.add_argument("--evidence-dir", default="evidence/agent")
    parser.add_argument("--history-db", help="SQLite history; default: evidence-dir/history.sqlite3")
    parser.add_argument("--project-id")
    parser.add_argument("--session-id", default="cli-session")
    parser.add_argument("--message-id", help="Повторная доставка использует прежний ID; retest — новый")
    parser.add_argument("--parent-run-id", help="Явное продолжение новым Run, без replay")
    parser.add_argument("--environment", default="default")
    parser.add_argument(
        "--conditions",
        help="JSON файл явных условий пользователя: predicate, parameters, intent, preconditions",
    )
    parser.add_argument(
        "--project-config", help="Approved concrete Project rights with pinned hashes"
    )
    parser.add_argument(
        "--goal-checks", help="Trusted per-operation conditions for this exact goal"
    )
    parser.add_argument(
        "--runtime-mappings",
        help="Trusted extraction/binding/auth/selection_scopes JSON; credentials remain private",
    )
    parser.add_argument(
        "--interactive-approval",
        action="store_true",
        help="Ask owner for each concrete CONFIRM action",
    )
    args = parser.parse_args(argv)
    history = None
    cancel = threading.Event()
    previous_sigint = None
    if threading.current_thread() is threading.main_thread():
        previous_sigint = signal.signal(
            signal.SIGINT, lambda signum, frame: cancel.set()
        )
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
        project_id = None
        if args.project_config:
            if (
                args.workflow
                or args.conditions
                or args.credentials_file
                or not args.goal_checks
            ):
                raise ValueError("conflicting_or_missing_catalog_options")
            from ..policy import ProjectConfig
            from .catalog import CatalogWorkflow

            config = ProjectConfig.load(args.project_config, catalog)
            if config.snapshot["target"] != args.target:
                raise ValueError("foreign_project_target")
            project_id = config.snapshot["project_id"]

            def load_bounded(path):
                file = Path(path)
                if file.stat().st_size > 65536:
                    raise ValueError("mapping_limit")
                return json.loads(file.read_text(encoding="utf-8-sig"))

            checks = load_bounded(args.goal_checks)
            mappings = (
                load_bounded(args.runtime_mappings) if args.runtime_mappings else {}
            )
            if not isinstance(mappings, dict) or set(mappings) - {
                "extraction",
                "binding_rules",
                "auth",
                "anonymous_operations",
                "selection_scopes",
            }:
                raise ValueError("invalid_runtime_mappings")

            def approval(proposal):
                print(
                    json.dumps({"needs_confirmation": proposal}, ensure_ascii=False),
                    file=sys.stderr,
                )
                from ..policy.approval import read_approval

                print("Approve this exact action? Type yes:", file=sys.stderr)
                return read_approval(
                    proposal,
                    reader=sys.stdin.readline,
                    cancel=cancel,
                    timeout=proposal["approval_timeout_seconds"],
                )

            workflow = CatalogWorkflow(
                catalog,
                config,
                goal=args.goal,
                conditions=checks,
                approval=approval if args.interactive_approval else None,
                current_config=lambda: ProjectConfig.load(args.project_config, catalog),
                **mappings
            )
        elif args.goal_checks or args.runtime_mappings or args.interactive_approval:
            raise ValueError("project_config_required")
        if args.project_id and project_id and args.project_id != project_id:
            raise ValueError("foreign_project_configuration")
        project_id = project_id or args.project_id or "cli-project"
        message_id = args.message_id or str(uuid.uuid4())
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
        from ..storage import SQLiteHistory
        from .summary_review import FinalSummaryReviewer
        history = SQLiteHistory(args.history_db or directory / "history.sqlite3")
        app = StartAgentRun(
            args.target,
            catalog,
            workflow=workflow,
            trace_sink=sink,
            token_counter=token_counter,
            event_sink=JsonlJournal(directory),
            history_store=history,
            environment=args.environment,
            model_name=args.model_name,
            summary_reviewer=FinalSummaryReviewer(model if model is not None else LocalModel(args.llm_target)) if args.review_summary else None,
        )
        run = app.start(
            project_id,
            args.session_id,
            message_id,
            args.goal,
            model if model is not None else LocalModel(args.llm_target),
            conditions=conditions,
            cancel=cancel,
            parent_run_id=args.parent_run_id,
        )
        print(
            json.dumps(
                {
                    "run_id": run["run_id"],
                    "status": run["status"],
                    "stop_reason": run["stop_reason"],
                    "requests": run["requests"],
                    "response_message_id": run.get("response_message_id"),
                    "history_db": str(history.path),
                    "trace": str(directory / (run["run_id"] + ".json")),
                    "journal": str(directory / (run["run_id"] + ".jsonl")),
                    "summary": run["report"]["summary"],
                    "contract_verdict": run["report"]["contract_verdict"],
                    "intent_verdict": run["report"]["intent_verdict"],
                    'checks_verdict':run['report']['checks_verdict'],
                    'goal_verdict':run['report']['goal_verdict'],
                    **({'summary_review':run['report']['summary_review']} if 'summary_review' in run['report'] else {}),
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
    finally:
        if history is not None:
            history.close()
        if previous_sigint is not None:
            signal.signal(signal.SIGINT, previous_sigint)


if __name__ == "__main__":
    raise SystemExit(main())

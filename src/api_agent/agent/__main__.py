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
from ..openapi import load_source


def main(argv=None, *, model=None):
    """Передать команду StartAgentRun и показать безопасный итог без нового HTTP."""
    parser = argparse.ArgumentParser(description="Run restricted local API agent")
    parser.add_argument("goal")
    parser.add_argument("--target", required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--llm-target", default="http://127.0.0.1:18081")
    parser.add_argument("--evidence-dir", default="evidence/agent")
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

        app = StartAgentRun(args.target, catalog, trace_sink=sink)
        run = app.start(
            "cli-project",
            "cli-session",
            str(uuid.uuid4()),
            args.goal,
            model if model is not None else LocalModel(args.llm_target),
        )
        print(
            json.dumps(
                {
                    "run_id": run["run_id"],
                    "status": run["status"],
                    "stop_reason": run["stop_reason"],
                    "requests": run["requests"],
                    "trace": str(directory / (run["run_id"] + ".json")),
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

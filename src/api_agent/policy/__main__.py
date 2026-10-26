"""Явный preview/сохранение прав владельцем до запуска модели."""

import argparse
import json
from pathlib import Path
from api_agent.policy.config import ProjectConfig
from api_agent.openapi import load_source


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Preview supported API and explicitly configure Project rights"
    )
    parser.add_argument("command", choices=("preview", "configure"))
    parser.add_argument("--spec", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--project-id", default="cli-project")
    parser.add_argument(
        "--mode", choices=("READ_ONLY", "SAFE", "TEST"), default="READ_ONLY"
    )
    parser.add_argument("--resettable", action="store_true")
    parser.add_argument(
        "--selection", help="Owner JSON mapping operation_id to risk A-E"
    )
    parser.add_argument("--output")
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Explicit approval of the shown concrete selection",
    )
    args = parser.parse_args(argv)
    try:
        catalog = load_source(args.spec, target=args.target)
        preview = [
            {
                "operation_id": name,
                "operation_hash": catalog.operation_hashes[name],
                "method": op["method"],
                "path": op["path"],
                "security": op["security"],
            }
            for name, op in sorted(catalog.operations.items())
        ]
        print(
            json.dumps(
                {"spec_hash": catalog.spec_hash, "operations": preview},
                ensure_ascii=False,
            )
        )
        if args.command == "configure":
            if not args.selection or not args.output or not args.approve:
                raise ValueError("explicit_owner_selection_and_approval_required")
            path = Path(args.selection)
            if path.stat().st_size > 65536:
                raise ValueError("selection_limit")
            selection = json.loads(path.read_text(encoding="utf-8-sig"))
            config = ProjectConfig.create(
                args.project_id,
                args.target,
                catalog,
                mode=args.mode,
                resettable=args.resettable,
                operations=selection,
            )
            config.save(args.output)
        return 0
    except Exception as error:
        print(json.dumps({"code": "policy_setup_failed", "type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

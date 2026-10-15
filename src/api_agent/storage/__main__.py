"""Read-only CLI истории: target и LLM не загружаются даже при недоступном API."""

import argparse
import json
from . import SQLiteHistory


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read saved API agent history")
    parser.add_argument("--db", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--session", required=True)
    selected = parser.add_mutually_exclusive_group()
    selected.add_argument("--run")
    selected.add_argument("--message")
    args = parser.parse_args(argv)
    try:
        with SQLiteHistory(args.db, read_only=True) as history:
            if args.run:
                value = history.read_run(args.project, args.session, args.run)
            elif args.message:
                value = history.run_for_message(args.project, args.session, args.message)
            else:
                value = history.messages(args.project, args.session)
            print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        print(json.dumps({"code": "history_read_rejected"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

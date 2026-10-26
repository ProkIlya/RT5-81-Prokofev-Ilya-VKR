"""Read-only CLI истории: target и LLM не загружаются даже при недоступном API."""

import argparse
import json
from api_agent.storage import SQLiteHistory


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read saved API agent history")
    parser.add_argument("--db", required=True)
    parser.add_argument("--project")
    parser.add_argument("--session")
    selected = parser.add_mutually_exclusive_group()
    selected.add_argument("--run")
    selected.add_argument("--message")
    parser.add_argument('--view',choices=['projects','sessions','messages','report','exchanges','section','selection','pins'])
    parser.add_argument('--limit',type=int,default=16)
    parser.add_argument('--offset',type=int,default=0)
    parser.add_argument('--exchange')
    parser.add_argument('--section',choices=['request','response'],default='response')
    parser.add_argument('--report-section',choices=['conditions','contract_diagnostics'],default='conditions')
    parser.add_argument('--pointer',default='')
    parser.add_argument('--object-path',default='')
    parser.add_argument('--field',action='append',default=[])
    bookmarks=parser.add_mutually_exclusive_group()
    bookmarks.add_argument('--pin',action='store_true')
    bookmarks.add_argument('--unpin',action='store_true')
    parser.add_argument('--label',default='')
    args = parser.parse_args(argv)
    try:
        with SQLiteHistory(args.db, read_only=not (args.pin or args.unpin)) as history:
            bounds={'limit':args.limit,'offset':args.offset}
            if args.pin or args.unpin:
                if not all([args.project,args.session,args.run]) or args.view:raise ValueError('pin_invalid_scope')
                if args.pin:value=history.pin_report(args.project,args.session,args.run,label=args.label)
                else:history.unpin_report(args.project,args.session,args.run);value={'unpinned':True}
            elif args.view=='pins':
                if not args.project:raise ValueError('project_required')
                value=history.pinned_reports(args.project,**bounds)
            elif args.view=='projects' or not args.project:
                if args.view not in {None,'projects'}:raise ValueError('project_required')
                value=history.list_projects(**bounds)
            elif args.view=='sessions':
                value=history.list_sessions(args.project,**bounds)
            elif not args.session:raise ValueError('session_required')
            elif args.view in {'report','exchanges','section','selection'}:
                if not args.run:raise ValueError('run_required')
                if args.view=='report':value=history.report_page(args.project,args.session,args.run,section=args.report_section,**bounds)
                elif args.view=='exchanges':value=history.exchanges_page(args.project,args.session,args.run,**bounds)
                elif args.view=='selection':value=history.select_attachment(args.project,args.session,args.run,args.exchange,
                    object_path=args.object_path,fields=args.field)
                else:value=history.exchange_section(args.project,args.session,args.run,args.exchange,
                    section=args.section,pointer=args.pointer,**bounds)
            elif args.run:
                value = history.read_run(args.project, args.session, args.run)
            elif args.message:
                value = history.run_for_message(args.project, args.session, args.message)
            else:
                value = history.list_messages(args.project, args.session,**bounds)
            print(json.dumps(value, ensure_ascii=False, indent=2))
        return 0
    except Exception:
        print(json.dumps({"code": "history_read_rejected"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

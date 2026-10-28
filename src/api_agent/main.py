"""CLI выбранного code агента; отдельного structured исполнителя больше нет."""
import argparse
import json
from pathlib import Path
import signal
import sys
import threading
from api_agent.agent.model import configured_goal_model
from api_agent.openapi import load_source


def main(argv=None, *, model=None, token_counter=None, human_reader=None):
    parser=argparse.ArgumentParser(description="Run restricted code API agent")
    parser.add_argument('goal',nargs='?')
    parser.add_argument('--interface',choices=('code',),default='code')
    parser.add_argument('--manual-config')
    parser.add_argument('--continue-run')
    parser.add_argument('--origin-session')
    parser.add_argument('--preview-only',action='store_true')
    parser.add_argument('--accept-preview')
    for name in ('code-sdk','code-sdk-hash','code-image','project-config','runtime-mappings','history-db',
                 'project-id','message-id','parent-run-id'):
        parser.add_argument('--'+name)
    parser.add_argument('--target',required=True)
    parser.add_argument('--spec',required=True)
    parser.add_argument('--llm-target',default='http://127.0.0.1:18081')
    parser.add_argument('--model-name',default=configured_goal_model())
    parser.add_argument('--session-id',default='cli-session')
    parser.add_argument('--evidence-dir',default='evidence/agent')
    parser.add_argument('--human-timeout',type=float,default=120)
    for name in ('max-requests','max-steps','max-tokens-total'):
        parser.add_argument('--'+name,type=int)
    parser.add_argument('--max-seconds',type=float)
    args=parser.parse_args(argv)
    cancel=threading.Event();previous=None
    if threading.current_thread() is threading.main_thread():
        previous=signal.signal(signal.SIGINT,lambda *_:cancel.set())
    try:
        catalog=load_source(args.spec,target=args.target)
        directory=Path(args.evidence_dir);directory.mkdir(parents=True,exist_ok=True)
        def sink(run):
            (directory/(run['run_id']+'.json')).write_text(json.dumps(run,ensure_ascii=False,indent=2),encoding='utf-8')
        if args.manual_config or args.continue_run:
            from api_agent.goals.entry.manual_cli import run_manual_cli as run
        else: raise ValueError('selected_manual_config_required')
        return run(args,catalog,directory,sink,model=model,token_counter=token_counter,
                            cancel=cancel,reader=human_reader)
    except Exception as error:
        print(json.dumps({'code':'agent_setup_failed','type':type(error).__name__}),file=sys.stderr)
        return 1
    finally:
        if previous is not None:signal.signal(signal.SIGINT,previous)


if __name__=='__main__':raise SystemExit(main())

"""Пользовательский CLI ручной конфигурации; preview не запускает inference."""
from dataclasses import replace
import getpass
import json
from pathlib import Path
import sys
import uuid

from api_agent.execution.contracts import CatalogLimits
from api_agent.agent.model import LocalModel
from api_agent.bounded_json import validate_tree
from api_agent.policy.config import ProjectConfig
from api_agent.storage import SQLiteHistory
from api_agent.agent.runtime import WorkspaceOptions
from api_agent.workspace.sdk import SdkArtifact, build_sdk
from api_agent.goals.entry.interaction import HumanInteraction
from api_agent.goals.entry.manual_application import ManualApplication


def _read(path, limit=65536):
    file=Path(path)
    if file.stat().st_size > limit: raise ValueError('selected_input_limit')
    value=json.loads(file.read_text(encoding='utf-8-sig'))
    validate_tree(value,max_depth=24,max_bytes=limit)
    return value


def run_manual_cli(args, catalog, directory, sink, *, model, token_counter, cancel, reader=None):
    if not args.project_config: raise ValueError('selected_project_config_required')
    continuation = args.continue_run
    if continuation and (args.manual_config or args.goal is not None or args.parent_run_id or
            any(getattr(args, field) is not None for field in ('max_requests','max_steps','max_seconds','max_tokens_total'))):
        raise ValueError('continuation_immutable_goal_and_limits')
    if continuation and not args.origin_session: raise ValueError('continuation_origin_session_required')
    document=_read(args.manual_config) if not continuation else None
    if not continuation and args.goal is not None and args.goal != document.get('goal'): raise ValueError('selected_goal_conflict')
    config=ProjectConfig.load(args.project_config,catalog)
    if args.project_id and args.project_id != config.snapshot['project_id']: raise ValueError('selected_foreign_project')
    if args.target != config.snapshot['target']: raise ValueError('selected_target_conflict')
    if args.code_sdk:
        if not args.code_sdk_hash: raise ValueError('selected_sdk_hash_required')
        artifact=SdkArtifact.load(args.code_sdk,expected_hash=args.code_sdk_hash)
    else: artifact=build_sdk(catalog,config)
    if not args.preview_only and not args.code_image: raise ValueError('selected_code_image_required')
    workspace=WorkspaceOptions(artifact,args.code_image or 'sha256:'+'0'*64,model_name=args.model_name)
    mappings=_read(args.runtime_mappings) if args.runtime_mappings else {}
    if not isinstance(mappings,dict) or set(mappings)-{'auth','extraction','binding_rules','private_bodies','model_test_passwords','health_check','public_response_headers'}:
        raise ValueError('selected_invalid_mappings')
    def console(request):
        print(json.dumps(request,ensure_ascii=False),file=sys.stderr)
        value=getpass.getpass('Приватное значение: ') if request['kind']=='private_input' else input('Ответ (yes для принятия): ')
        if request['kind']=='ask':
            try:value=json.loads(value)
            except ValueError:pass
        return {'request_id':request['id'],'value':value}
    human=HumanInteraction(reader or console,timeout=args.human_timeout,cancel=cancel)
    refs={}
    def resolve(value):
        if isinstance(value,dict):
            if set(value)=={'private'}:
                name=value['private']
                if name not in refs:refs[name]=human.private_input(name)
                return human.resolve_private(refs[name])
            return {k:resolve(v) for k,v in value.items()}
        if isinstance(value,list):return [resolve(v) for v in value]
        return value
    try:
        passwords=mappings.pop('model_test_passwords',None)
        mappings=resolve(mappings)
        limits=replace(CatalogLimits(),**{field:getattr(args,arg) for field,arg in (
            ('max_requests','max_requests'),('max_steps','max_steps'),('max_seconds','max_seconds'),
            ('max_tokens_total','max_tokens_total')) if getattr(args,arg) is not None})
        path=Path(args.history_db) if args.history_db else directory/'history.sqlite3'
        with SQLiteHistory(path) as history:
            project = config.snapshot['project_id']
            if continuation:
                from api_agent.storage.checkpoints import validate_checkpoint
                original = history.read_run(project, args.origin_session, continuation)
                limits = CatalogLimits(**validate_checkpoint(history, original)['limits'])
            app=ManualApplication(catalog,config,history=history,workspace=workspace,human=human,
                mappings=mappings,limits=limits,token_counter=token_counter,trace_sink=sink,
                model_test_passwords=passwords)
            preview=(app.preview_continuation(project,args.origin_session,continuation) if continuation
                     else app.preview(document,parent_run_id=args.parent_run_id))
            if args.preview_only:
                print(json.dumps(preview,ensure_ascii=False));return 0
            print(json.dumps(preview,ensure_ascii=False),file=sys.stderr)
            if continuation:
                result=app.continue_run(project,args.origin_session,continuation,session_id=args.session_id,
                    message_id=args.message_id or str(uuid.uuid4()),model=model or LocalModel(args.llm_target),
                    accepted_hash=args.accept_preview)
            else:
                result=app.start(project,args.session_id,args.message_id or str(uuid.uuid4()),
                    document,model or LocalModel(args.llm_target),accepted_hash=args.accept_preview,parent_run_id=args.parent_run_id)
            print(json.dumps(result,ensure_ascii=False))
            return 0 if result['status']=='completed' else 2
    finally:human.close()

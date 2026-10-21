"""CLI подготовки S09b до Textual: явные вопросы, принятие и private input."""
from dataclasses import replace
import getpass
import json
from pathlib import Path
import sys

from .agent.catalog import CatalogLimits
from .agent.model import LocalModel
from .agent.resilience import JsonlJournal
from .goal_application import GoalApplication
from .interaction import HumanInteraction
from .policy import ProjectConfig
from .storage import SQLiteHistory
from .validation import fingerprint


def run_goal_cli(args, catalog, directory, sink, *, model, token_counter, cancel, reader=None):
    if not args.project_config or args.workflow or args.conditions or args.goal_checks or args.credentials_file or args.review_summary:
        raise ValueError('conflicting_generated_goal_options')
    config = ProjectConfig.load(args.project_config, catalog)
    if args.project_id and args.project_id != config.snapshot['project_id']: raise ValueError('foreign_project')
    project, session, message = config.snapshot['project_id'], args.session_id, args.message_id
    if not message:
        import uuid
        message = str(uuid.uuid4())
    path = Path(args.history_db) if args.history_db else directory / 'history.sqlite3'
    with SQLiteHistory(path) as history:
        history.goal_preparation(project, session, message, fingerprint(args.goal))
        old = history.lookup(project, session, message)
        if old is not None:
            if old['goal_hash'] != fingerprint(args.goal): raise ValueError('goal_message_changed')
            print(json.dumps(old, ensure_ascii=False))
            return 0 if old['status'] == 'completed' else 2
        mappings = {}
        if args.runtime_mappings:
            file = Path(args.runtime_mappings)
            if file.stat().st_size > 65536: raise ValueError('mapping_limit')
            mappings = json.loads(file.read_text(encoding='utf-8-sig'))
        if not isinstance(mappings, dict) or set(mappings) - {'auth', 'extraction', 'binding_rules', 'private_bodies'}:
            raise ValueError('unsupported_goal_mappings')
        def console(request):
            print(json.dumps(request, ensure_ascii=False), file=sys.stderr)
            if request['kind'] == 'private_input': value = getpass.getpass('Приватное значение: ')
            else:
                value = input('Ответ (yes для принятия/подтверждения): ')
                if request['kind'] == 'ask':
                    try: value = json.loads(value)
                    except ValueError: pass
            return {'request_id': request['id'], 'value': value}
        human = HumanInteraction(reader or console, timeout=args.human_timeout, cancel=cancel)
        private_refs = {}
        def resolve(value):
            if isinstance(value, dict):
                if set(value) == {'private'}:
                    label = value['private']
                    if label not in private_refs: private_refs[label] = human.private_input(label)
                    return human.resolve_private(private_refs[label])
                return {k: resolve(v) for k, v in value.items()}
            if isinstance(value, list): return [resolve(v) for v in value]
            return value
        try:
            mappings = resolve(mappings)
            limits = CatalogLimits()
            values = {field: getattr(args, argument) for field, argument in (
                ('max_requests', 'max_requests'), ('max_steps', 'max_steps'), ('max_seconds', 'max_seconds'),
                ('max_tokens_total', 'max_tokens_total')) if getattr(args, argument) is not None}
            limits = replace(limits, **values)
            local = model or LocalModel(args.llm_target)
            app = GoalApplication(args.target, catalog, config, history=history, generator=local, human=human,
                token_counter=token_counter, limits=limits, mappings=mappings, trace_sink=sink, event_sink=JsonlJournal(directory),
                answer_diagnostic=args.answer_diagnostic)
            result = app.start(project, session, message, args.goal, local, parent_run_id=args.parent_run_id,
                               force_confirmation=args.confirm_goal)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result['status'] == 'completed' else 2
        finally:
            human.close()

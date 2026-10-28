"""Один UI-command adapter; просмотр истории не создаёт application/runtime."""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass, replace
import json
from pathlib import Path
import queue
import threading
import time
import uuid

from api_agent.agent.model import LocalModel
from api_agent.agent.runtime import WorkspaceOptions
from api_agent.execution.contracts import CatalogLimits
from api_agent.goals.entry.interaction import HumanInteraction
from api_agent.goals.entry.manual_application import ManualApplication
from api_agent.openapi import load_source
from api_agent.openapi.model import canonical
from api_agent.policy.config import ProjectConfig
from api_agent.storage import SQLiteHistory
from api_agent.workspace.sdk import build_sdk
from api_agent.desktop.models import ModelManager


class InteractionChannel:
    """Ответы только ожидающему request_id; private value живёт лишь до передачи."""
    def __init__(self, cancel, notify=None):
        self.cancel = cancel; self.notify = notify or (lambda *_: None)
        self.pending = None; self.waiting = threading.Event(); self.lock = threading.Lock()
        self.replies = queue.Queue(maxsize=1)
        self.deadline = 0

    def __call__(self, request):
        replies=queue.Queue(maxsize=1)
        deadline=time.monotonic()+request.get('timeout_seconds',120)
        with self.lock:
            if self.pending is not None and time.monotonic()<self.deadline:return None
            self.pending = deepcopy(request); self.waiting.set()
            self.deadline=deadline;self.replies=replies;self.current_id=request['id']
        self.notify('human', deepcopy(request))
        try:
            while not self.cancel.is_set() and time.monotonic()<deadline:
                try: return replies.get(timeout=min(0.05,max(.001,deadline-time.monotonic())))
                except queue.Empty: pass
            return None
        finally:
            with self.lock:
                if self.current_id==request['id']:self.pending=None;self.waiting.clear()
            self.notify('human_closed',{'request_id':request['id']})

    def reply(self, request_id, value):
        with self.lock:
            if self.cancel.is_set() or time.monotonic()>=self.deadline or self.pending is None or self.pending['id'] != request_id: return False
            self.pending = None
            self.replies.put_nowait({'request_id': request_id, 'value': value})
            return True


@dataclass
class PreparedGoal:
    project: str
    session: str
    message: str
    document: bytes | None
    application: ManualApplication
    history: SQLiteHistory
    preview: dict
    model_snapshot: dict
    origin: tuple | None = None

    def close(self):
        self.application.human.close(); self.history.close()


class DesktopController:
    def __init__(self, store, *, manager=None, notify=None, model_factory=None):
        self.store = store; self.manager = manager or ModelManager()
        self.notify = notify or (lambda *_: None); self.model_factory = model_factory or LocalModel
        self.cancel_event = threading.Event(); self.channel = InteractionChannel(self.cancel_event, self._notify)
        self.lock = threading.RLock(); self.thread = None; self.busy = False
        self.result = None; self.active_scope = None; self.current_run = None

    def _notify(self, kind, value):
        if kind == 'human' and self.active_scope: self.store.mark(*self.active_scope, 'input')
        self.notify(kind, value)

    def history_path(self, project):
        config = self.store.project(project)['configuration']
        return Path(config.get('history_path') or self.store.path.parent/'history.sqlite3')

    @contextmanager
    def history(self, project, *, write=False):
        with SQLiteHistory(self.history_path(project), read_only=not write) as history: yield history

    def messages(self, project, session, *, offset=0):
        if not self.history_path(project).exists(): return {'items': [], 'more': False}
        with self.history(project) as history: return history.list_messages(project, session, limit=20, offset=offset)

    def read_run(self, project, session, run_id):
        with self.history(project) as history: return history.read_run(project, session, run_id)

    def exchanges(self, project, session, run_id, *, offset=0):
        with self.history(project) as history: return history.exchanges_page(project, session, run_id, limit=20, offset=offset)

    def section(self, project, session, run_id, exchange_id, **options):
        with self.history(project) as history:
            return history.exchange_section(project, session, run_id, exchange_id, **options)

    def _application(self, project, snapshot, history, *, limits=None):
        config = self.store.project(project)['configuration']
        catalog = load_source(config['spec_path'], target=config['target'])
        policy = ProjectConfig.load(config['policy_path'], catalog)
        if policy.snapshot['project_id'] != project: raise ValueError('desktop_project_identity')
        mappings = {}
        if config.get('mappings_path'):
            from api_agent.goals.entry.manual_cli import _read
            mappings = _read(config['mappings_path'])
        if not isinstance(mappings,dict) or set(mappings)-{'auth','extraction','binding_rules','private_bodies','model_test_passwords','health_check','public_response_headers'}:
            raise ValueError('selected_invalid_mappings')
        passwords = mappings.pop('model_test_passwords', None)
        human = HumanInteraction(self.channel, cancel=self.cancel_event)
        def resolve(value):
            if isinstance(value, dict):
                if set(value) == {'private'}: return human.resolve_private(human.private_input(value['private']))
                return {k: resolve(v) for k, v in value.items()}
            if isinstance(value, list): return [resolve(v) for v in value]
            return value
        mappings = resolve(mappings)
        limits = limits or replace(CatalogLimits(), max_context_tokens=snapshot['context_tokens'],
                                   max_output_tokens=snapshot['output_tokens'])
        options = WorkspaceOptions(build_sdk(catalog, policy), config['image'],
                                   snapshot['model_name'], canonical(snapshot))
        return ManualApplication(catalog, policy, history=history, workspace=options, human=human, source='user_constructor',
            mappings=mappings, limits=limits, token_counter=LocalModel(snapshot['endpoint']).count_tokens,
            current_config=lambda:ProjectConfig.load(config['policy_path'], catalog),
            trace_sink=self._trace, event_sink=lambda event:self._notify('event', event),
            model_test_passwords=passwords)

    def _trace(self, run):
        self.current_run = run['run_id']; self._notify('trace', {'project':run['project_id'],
            'session':run['session_id'], 'run_id':run['run_id'], 'exchanges':len(run.get('exchanges', []))})

    def prepare(self, project, session, draft, *, origin=None):
        with self.lock:
            if self.busy: raise ValueError('desktop_run_busy')
            if not origin and not draft.get('model_id'): raise ValueError('model_selection_required')
            self.cancel_event.clear(); self.channel = InteractionChannel(self.cancel_event, self._notify)
            self.active_scope = (project, session)
            if origin:
                with self.history(project) as history: parent = history.read_run(project, *origin)
                expected = (parent.get('model') or {}).get('snapshot')
                if not expected: raise ValueError('continuation_model_identity_unproven')
                record = expected['profile']
            else:
                expected = None; record = self.store.model(draft['model_id'])
            self._notify('preparation', {'message':'Подготовка выбранной модели'})
            snapshot = self.manager.prepare(record, expected=expected, cancel=self.cancel_event)
            if expected is not None: snapshot = expected
            history = SQLiteHistory(self.history_path(project))
            try:
                if origin:
                    from api_agent.storage.checkpoints import validate_checkpoint
                    limits = CatalogLimits(**validate_checkpoint(history, parent)['limits'])
                else: limits = None
                app = self._application(project, snapshot, history, limits=limits)
                if origin: preview = app.preview_continuation(project, *origin); document = None
                else:
                    document = {'schema_version':'selected-checks-v1', 'goal':draft['goal'],
                        'mode':'testing' if any(c.get('enabled', True) for c in draft['cards']) else 'research',
                        'cards':deepcopy(draft['cards']), 'attachments':deepcopy(draft.get('attachments', []))}
                    if draft.get('input_slots'): document['input_slots'] = deepcopy(draft['input_slots'])
                    preview = app.preview(document)
                    document = canonical(document)
                return PreparedGoal(project, session, uuid.uuid4().hex, document, app, history,
                                    preview, snapshot, origin)
            except Exception:
                history.close(); raise

    def start(self, prepared, accepted_hash, *, can_start=None):
        with self.lock:
            if self.busy: raise ValueError('desktop_run_busy')
            if accepted_hash != prepared.preview['preview_hash']: raise ValueError('selected_preview_changed')
            self._validate_start(prepared,can_start)
            self.manager.prepare(prepared.model_snapshot['profile'], expected=prepared.model_snapshot,
                                 cancel=self.cancel_event)
            self._validate_start(prepared,can_start)
            self.manager.acquire(expected=prepared.model_snapshot); self.busy = True; self.result = None
            self.active_scope = (prepared.project, prepared.session)
            self.store.mark(*self.active_scope, 'active')
            def execute():
                try:
                    model = self.model_factory(prepared.model_snapshot['endpoint'])
                    if prepared.origin:
                        result = prepared.application.continue_run(prepared.project, *prepared.origin,
                            session_id=prepared.session, message_id=prepared.message, model=model, accepted_hash=accepted_hash)
                    else:
                        result = prepared.application.start(prepared.project, prepared.session, prepared.message,
                            json.loads(prepared.document), model, accepted_hash=accepted_hash)
                    self.result = result
                    self.store.mark(*self.active_scope, 'done' if result.get('status') == 'completed' else 'error')
                    self._notify('finished', {'project':prepared.project,'session':prepared.session,
                        'run_id':result.get('run_id'),'stop_reason':result.get('stop_reason')})
                except Exception as error:
                    self.result = {'status':'blocked','stop_reason':str(error) if isinstance(error,ValueError) else type(error).__name__}
                    self.store.mark(*self.active_scope, 'error'); self._notify('error', self.result)
                finally:
                    prepared.close(); self.manager.release(); self.busy = False
            self.thread = threading.Thread(target=execute, daemon=True); self.thread.start()

    def _validate_start(self,prepared,can_start):
        if self.cancel_event.is_set():raise ValueError('selected_preparation_cancelled')
        if can_start is not None and not can_start():raise ValueError('selected_submission_changed')
        if prepared.origin is None:
            current=self.store.model(prepared.model_snapshot['profile']['id'])
            if current!=prepared.model_snapshot['profile']:raise ValueError('selected_model_profile_changed')

    def cancel(self): self.cancel_event.set()
    def join(self, timeout=5):
        if self.thread: self.thread.join(timeout)

    def close(self):
        self.cancel(); self.join()
        if self.busy: raise ValueError('desktop_run_busy')
        self.manager.close()

    def shutdown(self):
        """База UI остаётся открытой до завершения finite Core cleanup."""
        self.cancel()
        while self.thread and self.thread.is_alive():self.thread.join(1)
        self.busy=False;self.manager.release();self.manager.close()

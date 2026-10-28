"""Подготовка привязана к полной редакции composer и завершается на UI thread."""
from copy import deepcopy
from dataclasses import dataclass
from textual import work
from api_agent.desktop.dialogs import PreviewDialog
from api_agent.validation import fingerprint


@dataclass(frozen=True)
class Submission:
    scope: tuple
    revision: int
    draft_hash: str
    project_hash: str


class SubmissionMixin:
    def prepare_send(self,project,session,draft,origin=None):
        self.action_save()
        token=Submission((project,session),self.draft_revision,
            fingerprint(self.store.draft(project,session)),fingerprint(self.store.project(project)['configuration']))
        self.pending_submission=token;self.follow_live=True
        self.prepare_submission(project,session,draft,origin,token)

    def submission_current(self,token):
        if (self.closing or self.controller.cancel_event.is_set() or self.pending_submission is not token
            or token.scope!=(self.project,self.session) or token.revision!=self.draft_revision):return False
        if fingerprint(self.store.draft(*token.scope))!=token.draft_hash:return False
        self.action_save()
        return (token.revision==self.draft_revision and fingerprint(self.draft)==token.draft_hash
            and fingerprint(self.store.project(token.scope[0])['configuration'])==token.project_hash)

    def close_submission(self,prepared,token):
        prepared.close()
        if self.prepared is prepared:self.prepared=None
        if self.pending_submission is token:self.pending_submission=None

    @work(thread=True,exclusive=True,group='prepare')
    def prepare_submission(self,project,session,draft,origin,token):
        prepared=None
        try:
            prepared=self.controller.prepare(project,session,draft,origin=origin)
            self.call_from_thread(self.preparation_ready,prepared,token)
        except Exception as error:
            if prepared is not None:prepared.close()
            self.submission_feedback(str(error))
        finally:
            if self.pending_submission is token or self.pending_submission is None:self.preparing=False

    def submission_feedback(self,value):
        if self.closing:return
        try:self.call_from_thread(self.set_status,value)
        except RuntimeError:pass

    def preparation_ready(self,prepared,token):
        if not self.submission_current(token):
            self.close_submission(prepared,token)
            if not self.closing:self.set_status('Подготовка отменена: черновик/сессия изменены. Нажмите «Отправить» заново.')
            return
        self.prepared=prepared
        if prepared.origin or prepared.preview['configuration']['mode']=='testing':
            self.push_screen(PreviewDialog(prepared.preview),lambda accepted:self.accept_prepared(prepared,accepted,token))
        else:self.accept_prepared(prepared,True,token)

    def accept_prepared(self,prepared,accepted,token):
        if not accepted or not self.submission_current(token):self.close_submission(prepared,token);return
        self.launching=True;self.launch_prepared(prepared,token);self.prepared=None

    @work(thread=True,group='start')
    def launch_prepared(self,prepared,token):
        try:
            self.call_from_thread(self.set_status,'Проверяем модель и запускаем…')
            self.controller.start(prepared,prepared.preview['preview_hash'],
                can_start=lambda:self.call_from_thread(self.submission_current,token))
            self.submission_feedback('Выполняется · отмена не откатывает HTTP-эффекты')
        except Exception as error:
            self.close_submission(prepared,token);self.submission_feedback(str(error))
        finally:self.launching=False

    def action_send(self):
        self.action_save()
        if not self.project:self.project_form();return
        if self.closing or self.controller.busy or self.preparing or self.launching:
            self.set_status('Дождитесь завершения текущего Run');return
        if not self.draft.get('model_id'):self.choose_model();return
        if not self.draft.get('goal','').strip():self.set_status('Введите цель');return
        self.preparing=True;self.prepare_send(self.project,self.session,deepcopy(self.draft))

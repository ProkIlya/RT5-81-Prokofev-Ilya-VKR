"""Видимая конфигурация новой цели; только read-only public attachment resolver."""
from dataclasses import asdict
import json
import sqlite3
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Label, Static
from api_agent.desktop.constructor_widgets import EditorDialog
from api_agent.execution.contracts import CatalogLimits


class ComposerMixin:
    def draft_limits(self):
        limits=asdict(CatalogLimits())
        record=self.store.model(self.draft.get('model_id'),required=False)
        if record:
            for field in ['context_tokens','output_tokens']:
                limits['max_'+field]=record['profile'].get(field,limits['max_'+field])
        return limits

    def refresh_composer(self):
        summary=self.query_one_optional('#draft-summary')
        if summary is None:return
        count=sum(bool(c.get('enabled',True)) for c in self.draft.get('cards',[]))
        limits=self.draft_limits();mode='Testing' if count else 'Research'
        summary.update(f"{mode} · условий{count} · прикреплений{len(self.draft.get('attachments',[]))} · "
            f"HTTP{limits['max_requests']}/шаги{limits['max_steps']}/{limits['max_seconds']}с\n"
            f"Токены{limits['max_tokens_total']} · контекст{limits['max_context_tokens']} · ответ{limits['max_output_tokens']}")

    def show_draft_details(self):
        if not self.project:self.project_form();return
        self.action_save();self.push_screen(DraftDetails(self))


class DraftDetails(EditorDialog):
    def __init__(self,owner):super().__init__(id='draft-details-dialog');self.owner=owner
    def compose(self):
        with Vertical(classes='editor-shell'):
            yield Label('Сводка новой цели · черновик до отправки')
            with VerticalScroll(classes='editor-body'):
                yield Static('Исторические данные — явно прикреплённые публичные значения. Их применимость повторно проверяется перед Run.')
                yield DataTable(id='attachments-table',cursor_type='row')
                yield Static('',id='attachment-detail',markup=False)
                yield Label('Все лимиты новой цели · выбранная модель проверяется перед стартом')
                yield Static(json.dumps(self.owner.draft_limits(),ensure_ascii=False,indent=2),id='draft-limits',markup=False)
                yield Static('',classes='editor-error')
            with Horizontal(classes='editor-actions'):
                yield Button('Убрать прикрепление',id='attachment-remove')
                yield Button('Закрыть',id='draft-details-close')
    def on_mount(self):
        self.query_one(DataTable).add_columns('№','Источник Run','Выбранные поля');self.refresh_rows()
    def refresh_rows(self):
        table=self.query_one(DataTable);table.clear()
        descriptors=self.owner.draft.get('attachments',[])
        for index,item in enumerate(descriptors[:8]):
            table.add_row(str(index+1),str(item.get('run_id','Недоступно')),str(item.get('fields',[])),key=str(index))
        self.query_one('#attachment-remove',Button).disabled=not descriptors
        if descriptors:self.show_attachment(0)
        else:self.query_one('#attachment-detail',Static).update('Прикреплённых данных нет')
        if len(descriptors)>8:self.error('Набор превышает поддержанные8 прикреплений; требуется исправить черновик')
    def show_attachment(self,index):
        descriptor=self.owner.draft['attachments'][index]
        try:
            with self.owner.controller.history(self.owner.project) as history:
                resolved=history.resolve_attachment(self.owner.project,descriptor)
            text=json.dumps(resolved,ensure_ascii=False,indent=2)
        except (ValueError,OSError,sqlite3.Error) as error:
            text='Данные недоступны: '+str(error)+'\nИсточник: '+json.dumps(descriptor,ensure_ascii=False,indent=2)
        self.query_one('#attachment-detail',Static).update(text)
    def on_data_table_row_selected(self,event):self.show_attachment(int(str(event.row_key.value)))
    def on_button_pressed(self,event):
        event.stop()
        if event.button.id=='draft-details-close':self.dismiss(None)
        elif event.button.id=='attachment-remove' and self.owner.draft.get('attachments'):
            index=min(self.query_one(DataTable).cursor_row,len(self.owner.draft['attachments'])-1)
            self.owner.draft['attachments'].pop(index);self.owner.action_save();self.refresh_rows()

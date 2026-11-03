"""Сводка новой цели с включёнными карточками и явными историческими объектами."""
from api_agent.desktop.components.controls import Button, Summary as Static
from dataclasses import asdict
from copy import deepcopy
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Label, Collapsible
from api_agent.desktop.builder.constructor_widgets import EditorDialog
from api_agent.desktop.presentation.presentation import conditions_summary, LIMITS, error_text
from api_agent.execution.contracts import CatalogLimits
from api_agent.desktop.presentation.ui_errors import read_feedback


class ComposerMixin:
    def draft_limits(self):
        limits=asdict(CatalogLimits());record=self.store.model(self.draft.get('model_id'),required=False)
        if record:
            for field in ['context_tokens','output_tokens']:limits['max_'+field]=record['profile'].get(field,limits['max_'+field])
        return limits
    def refresh_composer(self):
        summary=self.query_one_optional('#draft-summary')
        if summary is not None:summary.update('')
    @read_feedback
    def show_draft_details(self):
        if not self.project:self.project_form();return
        self.action_save();candidate=self.resume_candidate()
        if candidate:
            value=self.controller.continuation_summary(*self.inspected_scope,candidate)
            self.push_screen(ContinuationDetails(value))
        else:self.push_screen(DraftDetails(self))


class ContinuationDetails(EditorDialog):
    def __init__(self,value):super().__init__();self.value=value
    def compose(self):
        from api_agent.desktop.presentation.presentation import duration,literal
        value=self.value;model=value['model'] or {}
        with Vertical(classes='editor-shell'):
            yield Label('Сводка продолжения',classes='page-title')
            with VerticalScroll(classes='editor-body'):
                yield Static('Исходный запуск: '+value['parent_run_id'],markup=False)
                yield Static('Цель: '+value['goal'],markup=False)
                yield Static('Модель: '+model.get('profile',{}).get('profile',{}).get('name','Нет подтверждённого снимка'),markup=False)
                yield Static('Выбор модели и условия нового черновика не меняют продолжение исходного запуска.',classes='field-help')
                yield Label('Исходные включённые условия')
                yield Static(conditions_summary(value['configuration']),markup=False)
                for item in value['configuration'].get('attachments',[]):
                    yield Static('Прикреплённый объект: '+', '.join(f['path']+' = '+literal(f['value']) for f in item.get('fields',[])),markup=False)
                yield Label('Общие бюджеты всей цепочки')
                lines=[]
                for key,label in [('steps','Шаги агента'),('http','HTTP-запросы'),('tokens','Токены'),('active_seconds','Активное время')]:
                    show=duration if key=='active_seconds' else str
                    lines.append(label+': Израсходовано '+show(value['spent'][key])+' · Осталось '+show(value['remaining'][key]))
                yield Static('\n'.join(lines),id='continuation-ledger',markup=False)
                yield Static('\n'.join(LIMITS.get(k,k)+': '+str(v) for k,v in value['limits'].items()),id='draft-limits',markup=False)
                yield Static('Консервативный резерв времени: '+duration(value['active_time_accounting'].get('reserved_seconds',0)),markup=False)
                yield Static('Есть неизвестный эффект записи: повторные мутации запрещены до проверки.' if value['uncertain'] else 'Неизвестные эффекты записи не сохранены.',markup=False)
                if any(v<=0 for v in value['remaining'].values()):yield Static('Общий бюджет исчерпан: продолжение будет отклонено.',classes='editor-error')
                yield Static('Перед запуском будут повторно проверены исходная модель, политика и готовность API.',classes='field-help')
            with Horizontal(classes='editor-actions'):yield Button('Закрыть',id='draft-details-close')
    def on_button_pressed(self,event):event.stop();self.dismiss(None)


class DraftDetails(EditorDialog):
    def __init__(self,owner):
        super().__init__(id='draft-details-dialog');self.owner=owner;self.display_draft=deepcopy(owner.draft)
        resolved=[]
        for descriptor in owner.draft.get('attachments',[]):
            try:
                with owner.controller.history(owner.project) as history:resolved.append(history.resolve_attachment(owner.project,descriptor))
            except (ValueError,OSError):pass
        self.display_draft['attachments']=resolved
    def compose(self):
        with Vertical(classes='editor-shell'):
            yield Label('Сводка новой цели',classes='page-title')
            with VerticalScroll(classes='editor-body'):
                yield Static('Цель: '+self.owner.draft.get('goal',''),markup=False)
                model=self.owner.store.model(self.owner.draft.get('model_id'),required=False)
                yield Static('Модель: '+(model['profile']['name'] if model else 'не выбрана'),markup=False)
                with Collapsible(title='Проверки',collapsed=False):
                    yield Static(conditions_summary(self.display_draft),id='draft-cards',markup=False)
                with Collapsible(title='Источники · '+str(len(self.owner.draft.get('attachments',[]))),collapsed=False):
                    yield Static('Исторические наблюдения. Применимость проверяется перед запуском.',classes='field-help')
                    yield DataTable(id='attachments-table',cursor_type='row')
                    yield Static('',id='attachment-detail',markup=False)
                with Collapsible(title='Лимиты',collapsed=True):
                    yield Static('\n'.join(LIMITS.get(k,k)+': '+str(v) for k,v in self.owner.draft_limits().items()),id='draft-limits',markup=False)
                yield Static('Проверки подтверждают выбранные аспекты цели. Изменение цели или условий создаёт новый запуск.',classes='field-help')
                yield Static('',classes='editor-error')
            with Horizontal(classes='editor-actions'):
                yield Button('Убрать прикрепление',id='attachment-remove');yield Button('Закрыть',id='draft-details-close')
    def on_mount(self):self.query_one(DataTable).add_columns('№','Источник','Поля');self.refresh_rows()
    def refresh_rows(self):
        table=self.query_one(DataTable);table.clear();descriptors=self.owner.draft.get('attachments',[])
        for i,item in enumerate(descriptors[:8]):table.add_row(str(i+1),str(item.get('run_id',''))[:8],', '.join(item.get('fields',[])),key=str(i))
        self.query_one('#attachment-remove',Button).disabled=not descriptors
        table.display=bool(descriptors);table.styles.height=min(7,len(descriptors)+1)
        table.styles.width=min(100,max(36,max((len(', '.join(d.get('fields',[])))+20 for d in descriptors),default=36)))
        table.styles.max_width='100%'
        if descriptors:self.show_attachment(0)
        else:self.query_one('#attachment-detail',Static).update('Прикреплённых данных нет')
        if len(descriptors)>8:self.error('Можно прикрепить не более 8 объектов')
    def show_attachment(self,index):
        descriptor=self.owner.draft['attachments'][index]
        try:
            with self.owner.controller.history(self.owner.project) as history:resolved=history.resolve_attachment(self.owner.project,descriptor)
            lines=['Источник: '+resolved['origin']['operation_id']+' · запуск '+resolved['origin']['run_id'], 'HTTP-обмен: '+resolved['origin']['exchange_id']]
            lines.extend(f["path"]+' = '+str(f['value']) for f in resolved['fields']);text='\n'.join(lines)
        except (ValueError,OSError) as error:text='Данные недоступны: '+error_text(error)
        self.query_one('#attachment-detail',Static).update(text)
    def on_data_table_row_selected(self,event):self.show_attachment(int(str(event.row_key.value)))
    def on_button_pressed(self,event):
        event.stop()
        if event.button.id=='draft-details-close':self.dismiss(None)
        elif event.button.id=='attachment-remove' and self.owner.draft.get('attachments'):
            index=min(self.query_one(DataTable).cursor_row,len(self.owner.draft['attachments'])-1)
            self.owner.draft['attachments'].pop(index);self.owner.action_save();self.owner.update_controls();self.refresh_rows()

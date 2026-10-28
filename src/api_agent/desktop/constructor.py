"""Диалог карточек рядом с composer; Apply не является принятием/запуском Run."""
from copy import deepcopy
import json
from pathlib import Path
import uuid
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Label, Select, Static, TextArea

from api_agent.desktop.constructor_state import ConstructorState
from api_agent.desktop.constructor_widgets import EditorDialog
from api_agent.openapi import load_source
from api_agent.policy.config import ProjectConfig


class OperationPicker(EditorDialog):
    def __init__(self,state):super().__init__();self.state=state
    def compose(self):
        with Vertical(classes='editor-shell'):
            yield Label('Операция разрешённого API')
            yield Input(placeholder='Поиск имени / пути',id='operation-search')
            yield Select([(m,m) for m in ['ALL','GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS']],
                value='ALL',allow_blank=False,id='operation-method')
            yield DataTable(id='operation-table',cursor_type='row')
            yield Static('Enter — выбрать. Ожидаемый HTTP-статус задаётся в карточке.')
            yield Button('Отмена',id='operation-cancel')
    def on_mount(self):self.query_one(DataTable).add_columns('Операция','Метод','Путь');self.refresh_rows()
    def refresh_rows(self):
        table=self.query_one(DataTable);table.clear()
        for row in self.state.operations(search=self.query_one(Input).value,method=self.query_one(Select).value):
            table.add_row(row['id'],row['method'],row['path'],key=row['id'])
    def on_input_changed(self,event):
        if self.is_mounted:self.refresh_rows()
    def on_select_changed(self,event):
        if self.is_mounted:self.refresh_rows()
    def on_data_table_row_selected(self,event):self.dismiss(str(event.row_key.value))
    def on_button_pressed(self,event):event.stop();self.dismiss(None)


class TransferDialog(EditorDialog):
    def __init__(self,state,*,export=False):
        super().__init__(id='constructor-transfer');self.state=state;self.export=export
    def compose(self):
        with Vertical(classes='editor-shell'):
            yield Label('Экспорт условий' if self.export else 'Импорт условий · без запуска')
            yield TextArea(self.state.export_text() if self.export else '',id='transfer-text',read_only=self.export)
            yield Input(placeholder='Локальный файл UTF-8 (необязательно)',id='transfer-path')
            yield Static('',id='transfer-error',classes='editor-error')
            with Horizontal(classes='editor-actions'):
                yield Button('Копировать' if self.export else 'Применить импорт',id='transfer-apply',variant='primary')
                yield Button('В файл' if self.export else 'Из файла',id='transfer-file')
                yield Button('Назад',id='transfer-cancel')
    def on_button_pressed(self,event):
        event.stop();key=event.button.id;area=self.query_one(TextArea)
        try:
            if key=='transfer-cancel':self.dismiss(None)
            elif key=='transfer-apply':
                if self.export:self.app.copy_to_clipboard(area.text);self.error('Скопировано; при отсутствии clipboard используйте текст/файл')
                else:self.state.import_text(area.text);self.dismiss(True)
            elif key=='transfer-file':
                path=self.query_one(Input).value
                if not path or path.startswith(('\\\\','//')):raise ValueError('constructor_local_path_required')
                file=Path(path)
                if self.export:
                    with file.open('x',encoding='utf-8') as stream:stream.write(area.text)
                    self.error('Файл сохранён')
                else:
                    with file.open('rb') as stream:raw=stream.read(65537)
                    if len(raw)>65536:raise ValueError('constructor_import_size')
                    area.text=raw.decode('utf-8-sig')
        except (ValueError,OSError) as error:self.error('constructor: '+str(error))


def card_summary(card):
    return '\n'.join([card.get('name') or card['operation_id'],
        'HTTP '+', '.join(map(str,card['statuses']))+' · '+card.get('selector','last')+' · auth '+card.get('auth','any'),
        'Фильтры: '+json.dumps(card.get('where',{}),ensure_ascii=False),
        'Проверки: '+json.dumps(card.get('assertions',[]),ensure_ascii=False),
        'Негативный допуск: '+json.dumps(card.get('negative'),ensure_ascii=False)])


class ConstructorDialog(EditorDialog):
    def __init__(self,state,*,readonly=False):
        super().__init__(id='constructor');self.state=state;self.readonly=readonly
    def compose(self):
        with Vertical(classes='editor-shell'):
            yield Label('Сохранённые условия · только чтение' if self.readonly else 'Условия цели')
            with VerticalScroll(classes='editor-body'):
                yield Static('Проверяются выбранные аспекты. Без привязки ID карточки не доказывают один объект; без карточек операций не доказан весь lifecycle.')
                yield DataTable(id='cards-table',cursor_type='row')
                yield Static('',id='card-detail',markup=False)
                if not self.readonly:
                    with Horizontal(classes='editor-actions'):
                        for name,title in [('add','Добавить'),('edit','Изменить'),('delete','Удалить'),('toggle','Вкл/выкл')]:
                            yield Button(title,id='card-'+name)
                    with Horizontal(classes='editor-actions'):
                        yield Button('Импорт',id='constructor-import');yield Button('Экспорт',id='constructor-export')
                yield Static('',classes='editor-error')
            with Horizontal(classes='editor-actions'):
                if not self.readonly:yield Button('Применить',id='constructor-apply',variant='primary')
                yield Button('Закрыть' if self.readonly else 'Отмена',id='constructor-cancel')
    def on_mount(self):self.query_one(DataTable).add_columns('Вкл','Операция / название','HTTP','Вызов');self.refresh_rows()
    def refresh_rows(self):
        table=self.query_one(DataTable);table.clear()
        for index,card in enumerate(self.state.cards):
            table.add_row('✓' if card.get('enabled',True) else '—',card.get('name') or card['operation_id'],
                ','.join(map(str,card['statuses'])),card.get('selector','last'),key=str(index))
        if self.state.cards:self.query_one('#card-detail',Static).update(card_summary(self.state.cards[0]))
        else:self.query_one('#card-detail',Static).update('Набор пуст: Research · проверки не настроены')
    def selected(self):
        table=self.query_one(DataTable)
        return min(table.cursor_row,len(self.state.cards)-1) if self.state.cards else None
    def on_data_table_row_selected(self,event):
        self.query_one('#card-detail',Static).update(card_summary(self.state.cards[int(str(event.row_key.value))]))
    def edit(self,operation,index=None):
        if operation is None:return
        from api_agent.desktop.card_editor import CardEditor
        card=self.state.cards[index] if index is not None else {'id':'c'+uuid.uuid4().hex[:12],
            'operation_id':operation,'statuses':[],'assertions':[],'enabled':True}
        def saved(value):
            if value is None:return
            if index is None:self.state.cards.append(value)
            else:self.state.cards[index]=value
            self.refresh_rows()
        self.app.push_screen(CardEditor(self.state,card),saved)
    def on_button_pressed(self,event):
        event.stop();key=event.button.id;index=self.selected()
        try:
            if key=='constructor-cancel':self.dismiss(None)
            elif self.readonly:return
            elif key=='card-add':self.app.push_screen(OperationPicker(self.state),self.edit)
            elif key=='card-edit' and index is not None:self.edit(self.state.cards[index]['operation_id'],index)
            elif key=='card-delete' and index is not None:self.state.cards.pop(index);self.refresh_rows()
            elif key=='card-toggle' and index is not None:
                card=self.state.cards[index];card['enabled']=not card.get('enabled',True);self.refresh_rows()
            elif key in {'constructor-import','constructor-export'}:
                self.app.push_screen(TransferDialog(self.state,export=key.endswith('export')),lambda _:self.refresh_rows())
            elif key=='constructor-apply':self.state.compiled('Черновик условий');self.dismiss(deepcopy(self.state.cards))
        except ValueError as error:self.error(error)


def open_constructor(app,*,manifest=None):
    if not app.project:app.project_form();return
    try:
        if manifest is not None:
            app.push_screen(ConstructorDialog(ConstructorState(None,None,manifest.get('cards',[])),readonly=True));return
        settings=app.store.project(app.project)['configuration']
        catalog=load_source(settings['spec_path'],target=settings['target']);config=ProjectConfig.load(settings['policy_path'],catalog)
        attachments=[]
        if not manifest and app.draft.get('attachments'):
            with app.controller.history(app.project) as history:
                attachments=[history.resolve_attachment(app.project,d) for d in app.draft['attachments']]
        state=ConstructorState(catalog,config,(manifest or app.draft).get('cards',[]),attachments=attachments)
        def applied(cards):
            if cards is not None:app.draft['cards']=cards;app.action_save();app.update_controls()
        app.push_screen(ConstructorDialog(state,readonly=manifest is not None),applied if manifest is None else None)
    except (ValueError,OSError,KeyError) as error:app.set_status('Конструктор: '+str(error))

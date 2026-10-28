"""Фильтры запроса и response binding текущего Run без построения HTTP-плана."""
from copy import deepcopy
import json
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Label, Select, Static

from api_agent.desktop.constructor_widgets import EditorDialog, FieldPicker, LiteralDialog
from api_agent.goals.checks.condition_scope import validate_where, _compatible
from api_agent.goals.checks.selected_compile import _negative
from api_agent.openapi.model import pointer_part


class ScopeEditor(EditorDialog):
    def __init__(self,state,operation,where,*,references=True):
        super().__init__();self.state=state;self.operation=operation;self.where=deepcopy(where);self.references=references
        self.fields=state.request_fields(operation)
        self.options=[(location+' · '+name,location+':'+name) for location,fields in self.fields.items() for name in fields]
    def compose(self):
        with Vertical(classes='editor-shell'):
            yield Label('Условия запроса · '+self.operation)
            with VerticalScroll(classes='editor-body'):
                yield Static('Фильтры выбирают подходящие свидетельства. Они не задают порядок HTTP и не освобождают агента от обязательных параметров.')
                yield DataTable(id='scope-table',cursor_type='row')
                yield Select(self.options,prompt='Параметр / поле body',id='scope-field')
                with Horizontal(classes='editor-actions'):
                    yield Button('Литерал / данные',id='scope-literal',disabled=not self.options)
                    yield Button('ID из ответа',id='scope-response',disabled=not self.references or not self.options)
                    yield Button('Убрать фильтр',id='scope-remove')
                yield Static('',classes='editor-error')
            with Horizontal(classes='editor-actions'):
                yield Button('Применить фильтры',id='scope-save',variant='primary');yield Button('Отмена',id='scope-cancel')
    def on_mount(self):self.query_one(DataTable).add_columns('Место','Поле','Значение / источник');self.refresh_rows()
    def refresh_rows(self):
        table=self.query_one(DataTable);table.clear()
        for location,fields in self.where.items():
            for name,value in fields.items():table.add_row(location,name,json.dumps(value,ensure_ascii=False),key=location+':'+name)
    def field(self):
        key=self.query_one(Select).value
        if key is Select.NULL:raise ValueError('Выберите поле запроса')
        location,name=key.split(':',1);return location,name,self.fields[location][name]
    def set_value(self,location,name,result):
        if result is not None:
            self.where.setdefault(location,{})[name]=deepcopy(result['value']);self.refresh_rows()
    def on_data_table_row_selected(self,event):self.query_one(Select).value=str(event.row_key.value)
    def on_button_pressed(self,event):
        event.stop();key=event.button.id
        try:
            if key=='scope-cancel':self.dismiss(None)
            elif key=='scope-save':
                validate_where(self.state.catalog,self.state.config.snapshot['operations'],self.operation,self.where,references=self.references)
                self.dismiss(deepcopy(self.where))
            elif key in {'scope-literal','scope-response','scope-remove'}:
                location,name,schema=self.field()
                if key=='scope-remove':
                    self.where.get(location,{}).pop(name,None)
                    if not self.where.get(location):self.where.pop(location,None)
                    self.refresh_rows()
                elif key=='scope-literal':
                    self.app.push_screen(LiteralDialog(self.state,schema),lambda result:self.set_value(location,name,result))
                else:
                    if location!='parameters':raise ValueError('Response binding поддержан только для parameters')
                    self.app.push_screen(BindingEditor(self.state,schema),lambda result:self.set_value(location,name,result))
        except ValueError as error:self.error(error)


class BindingEditor(EditorDialog):
    def __init__(self,state,schema):
        super().__init__();self.state=state;self.schema=schema;self.operation=None;self.path=None;self.where={}
    def compose(self):
        with Vertical(classes='editor-shell'):
            yield Label('Источник неизвестного ID · фактический ответ этого Run')
            with VerticalScroll(classes='editor-body'):
                yield Button('Выбрать операцию источника',id='binding-operation')
                yield Static('Операция не выбрана',id='binding-operation-label')
                yield Button('Выбрать совместимое поле',id='binding-field')
                yield Static('Поле не выбрано',id='binding-field-label')
                yield Select([('Первый ответ источника','first'),('Последний ответ источника','last')],
                    value='last',allow_blank=False,id='binding-occurrence')
                yield Button('Литеральные фильтры источника (необязательно)',id='binding-scope')
                yield Static('{}',id='binding-scope-label')
                yield Static('Не извлекает ID заранее. Backend проверит тип, scope, actor и предшествующий фактический ответ; при недостатке evidence — UNKNOWN.')
                yield Static('',classes='editor-error')
            with Horizontal(classes='editor-actions'):
                yield Button('Применить источник',id='binding-save',variant='primary');yield Button('Отмена',id='binding-cancel')
    def operation_selected(self,value):
        if value is None:return
        self.operation=value;self.path=None;self.where={}
        self.query_one('#binding-operation-label',Static).update(value)
        self.query_one('#binding-field-label',Static).update('Поле не выбрано')
        self.query_one('#binding-scope-label',Static).update('{}')
    def field_selected(self,path):
        if path is not None:self.path=path;self.query_one('#binding-field-label',Static).update(path or 'Корень ответа')
    def scope_selected(self,where):
        if where is not None:self.where=where;self.query_one('#binding-scope-label',Static).update(json.dumps(where,ensure_ascii=False))
    def on_button_pressed(self,event):
        event.stop();key=event.button.id
        try:
            if key=='binding-cancel':self.dismiss(None)
            elif key=='binding-operation':
                from api_agent.desktop.constructor import OperationPicker
                self.app.push_screen(OperationPicker(self.state),self.operation_selected)
            elif key in {'binding-field','binding-scope','binding-save'}:
                if not self.operation:raise ValueError('Выберите операцию источника')
                if key=='binding-field':
                    fields={p:s for p,s in self.state.response_fields(self.operation,[]).items()
                        if s.get('type') in {'string','integer','number','boolean'} and _compatible(s,self.schema)}
                    self.app.push_screen(FieldPicker(fields),self.field_selected)
                elif key=='binding-scope':self.app.push_screen(ScopeEditor(self.state,self.operation,self.where,references=False),self.scope_selected)
                else:
                    if self.path is None:raise ValueError('Выберите публичное поле источника')
                    reference={'operation_id':self.operation,'path':self.path,'occurrence':self.query_one(Select).value}
                    if self.where:reference['where']=deepcopy(self.where)
                    self.dismiss({'value':{'response':reference}})
        except ValueError as error:self.error(error)


class NegativeEditor(EditorDialog):
    def __init__(self,state,operation,value=None):
        super().__init__();self.state=state;self.operation=operation;self.initial=value or {}
    def compose(self):
        fields=self.state.request_fields(self.operation)['body']
        with Vertical(classes='editor-shell'):
            yield Label('Одно намеренное нарушение схемы body')
            with VerticalScroll(classes='editor-body'):
                yield Select([(k,'/'+pointer_part(k)) for k in fields],value=self.initial.get('path',Select.NULL),
                    prompt='Поле body',id='negative-field')
                yield Select([('Пропустить required','required'),('Неверный тип','schema_type'),('Нарушить enum','enum')],
                    value=self.initial.get('rule','required'),allow_blank=False,id='negative-rule')
                yield Static('Допуск не отключает проверку остальных полей, policy, auth и budgets. Итоговая карточка требует ожидаемых4xx.')
                yield Static('',classes='editor-error')
            with Horizontal(classes='editor-actions'):
                yield Button('Применить допуск',id='negative-save',variant='primary');yield Button('Отмена',id='negative-cancel')
    def on_button_pressed(self,event):
        event.stop()
        if event.button.id=='negative-cancel':self.dismiss(None)
        elif event.button.id=='negative-save':
            try:
                value={'location':'body','path':self.query_one('#negative-field',Select).value,
                    'rule':self.query_one('#negative-rule',Select).value}
                _negative(value,self.state.catalog,self.operation,[400]);self.dismiss(value)
            except ValueError as error:self.error(error)

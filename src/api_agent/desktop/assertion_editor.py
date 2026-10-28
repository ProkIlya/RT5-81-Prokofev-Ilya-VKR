"""Предикаты общего compiler; UI не оценивает фактические HTTP-ответы."""
from copy import deepcopy
import json
from textual.containers import Vertical, VerticalScroll, Horizontal
from textual.widgets import Button, Input, Label, Select, Static

from api_agent.desktop.constructor_widgets import EditorDialog, FieldPicker, LiteralDialog
from api_agent.goals.checks.selected_compile import _assertion


PREDICATES=[('Равно','equals'),('Не равно','not_equals'),('Поле существует','exists'),
    ('Ответ соответствует схеме','schema'),('Число с округлением','number_rounded'),
    ('Число с допуском','number_within'),('Коллекция / таблица','collection')]


class AssertionEditor(EditorDialog):
    def __init__(self,state,fields,*,assertion=None):
        super().__init__();self.state=state;self.fields=fields;self.original=deepcopy(assertion or {})
        self.path=self.original.get('path');self.value=self.original.get('value');self.has_value='value' in self.original
        self.rows=deepcopy(self.original.get('rows',[]));self.collection_mode=self.original.get('mode','includes')
    def compose(self):
        with Vertical(classes='editor-shell'):
            yield Label('Проверка ответа')
            with VerticalScroll(classes='editor-body'):
                yield Select(PREDICATES,value=self.original.get('predicate','equals'),allow_blank=False,id='assertion-kind')
                yield Button('Выбрать поле из схемы',id='assertion-path')
                yield Static(self.path if self.path is not None else 'Поле не выбрано',id='assertion-path-label')
                yield Button('Задать значение / из прикреплённых',id='assertion-value')
                yield Static(json.dumps(self.value,ensure_ascii=False) if self.has_value else 'Значение не задано',id='assertion-value-label')
                yield Label('Ожидаемое число (десятичная строка; для округления сохраняется точность)',id='number-label')
                yield Input(str(self.original.get('expected','')),id='assertion-number')
                yield Label('Допуск >0 (для числового сравнения с допуском)',id='tolerance-label')
                yield Input(str(self.original.get('tolerance','')),id='assertion-tolerance')
                yield Button('Настроить таблицу коллекции',id='assertion-collection')
                yield Static('',id='collection-summary')
                yield Static('',classes='editor-error')
            with Horizontal(classes='editor-actions'):
                yield Button('Применить проверку',id='assertion-save',variant='primary');yield Button('Отмена',id='assertion-cancel')
    def on_mount(self):self.refresh_controls()
    def on_select_changed(self,event):
        if self.is_mounted:self.refresh_controls()
    def refresh_controls(self):
        kind=self.query_one(Select).value
        for key in ['assertion-path','assertion-path-label']:self.query_one('#'+key).display=kind!='schema'
        for key in ['assertion-value','assertion-value-label']:self.query_one('#'+key).display=kind in {'equals','not_equals'}
        for key in ['assertion-number','number-label']:self.query_one('#'+key).display=kind.startswith('number_')
        for key in ['assertion-tolerance','tolerance-label']:self.query_one('#'+key).display=kind=='number_within'
        for key in ['assertion-collection','collection-summary']:self.query_one('#'+key).display=kind=='collection'
    def field_selected(self,path):
        if path is not None:
            if path!=self.path:self.has_value=False;self.rows=[]
            self.path=path;self.query_one('#assertion-path-label',Static).update(path or 'Корень ответа')
    def literal_selected(self,value):
        if value is not None:
            self.value=value['value'];self.has_value=True
            self.query_one('#assertion-value-label',Static).update(json.dumps(self.value,ensure_ascii=False))
    def collection_selected(self,value):
        if value is not None:
            self.rows=value['rows'];self.collection_mode=value['mode']
            self.query_one('#collection-summary',Static).update(f"{self.collection_mode}: строк{len(self.rows)}")
    def on_button_pressed(self,event):
        event.stop();key=event.button.id;kind=self.query_one(Select).value
        try:
            if key=='assertion-cancel':self.dismiss(None)
            elif key=='assertion-path':
                kinds={'array'} if kind=='collection' else {'integer','number'} if kind.startswith('number_') else {'string','integer','number','boolean'}
                self.app.push_screen(FieldPicker(self.fields,kinds=kinds),self.field_selected)
            elif key in {'assertion-value','assertion-collection'}:
                if self.path is None:raise ValueError('Сначала выберите поле')
                schema=self.fields[self.path]
                if key=='assertion-value':self.app.push_screen(LiteralDialog(self.state,schema,value=self.value),self.literal_selected)
                else:
                    if schema.get('type')!='array':raise ValueError('Выберите поле типа array')
                    from api_agent.desktop.collection_editor import CollectionEditor
                    self.app.push_screen(CollectionEditor(self.state,schema,self.rows,self.collection_mode),self.collection_selected)
            elif key=='assertion-save':
                value={'predicate':kind}
                if kind!='schema':
                    if self.path is None:raise ValueError('Выберите поле из схемы ответа')
                    value['path']=self.path
                if kind in {'equals','not_equals'}:
                    if not self.has_value:raise ValueError('Задайте ожидаемое значение')
                    value['value']=deepcopy(self.value)
                elif kind.startswith('number_'):
                    value['expected']=self.query_one('#assertion-number',Input).value
                    if kind=='number_within':value['tolerance']=self.query_one('#assertion-tolerance',Input).value
                elif kind=='collection':value.update(mode=self.collection_mode,rows=deepcopy(self.rows))
                _assertion(value,self.fields.get(''));self.dismiss(value)
        except (ValueError,KeyError) as error:self.error(error)

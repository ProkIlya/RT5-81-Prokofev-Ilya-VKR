"""Одна карточка: пользовательские ожидания и расширенные ограничения evidence."""
from copy import deepcopy
import json
import re
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Checkbox, Collapsible, DataTable, Input, Label, Select, Static

from api_agent.desktop.constructor_widgets import EditorDialog


class CardEditor(EditorDialog):
    def __init__(self,state,card):super().__init__();self.state=state;self.card=deepcopy(card)
    def compose(self):
        with Vertical(classes='editor-shell'):
            op=self.state.catalog.operations[self.card['operation_id']]
            yield Label(op['method']+' '+op['path']+' · '+self.card['operation_id'])
            with VerticalScroll(classes='editor-body'):
                yield Checkbox('Условие включено',value=self.card.get('enabled',True),id='card-enabled')
                yield Input(self.card.get('name',''),placeholder='Название (необязательно)',id='card-name')
                yield Label('Ожидаемый HTTP-статус / набор через запятую · задаётся вами')
                yield Input(', '.join(map(str,self.card['statuses'])),placeholder='Например: 200',id='card-statuses')
                yield DataTable(id='assertions-table',cursor_type='row')
                with Horizontal(classes='editor-actions'):
                    yield Button('Добавить проверку',id='assertion-add');yield Button('Изменить',id='assertion-edit')
                    yield Button('Удалить',id='assertion-delete')
                with Collapsible(title='Вызов, запрос, авторизация и неизвестный ID',collapsed=False):
                    yield Select([('Последний подходящий','last'),('Первый подходящий','first'),('Все подходящие','all')],
                        value=self.card.get('selector','last'),allow_blank=False,id='card-selector')
                    options=[('Любая допустимая','any'),('Без авторизации','anonymous')]
                    if op['security']:options.append(('Текущая учётная запись','current'))
                    yield Select(options,value=self.card.get('auth','any'),allow_blank=False,id='card-auth')
                    yield Button('Фильтры запроса / источник ID',id='card-scope')
                    yield Static(json.dumps(self.card.get('where',{}),ensure_ascii=False),id='scope-summary',markup=False)
                with Collapsible(title='Намеренно schema-invalid body · одно поле / правило'):
                    yield Static('Только для ожидаемого4xx. Остальные проверки запроса сохраняются.')
                    yield Button('Настроить отрицательный допуск',id='negative-edit')
                    yield Button('Убрать допуск',id='negative-clear')
                    yield Static(json.dumps(self.card.get('negative'),ensure_ascii=False),id='negative-summary',markup=False)
                yield Static('',classes='editor-error')
            with Horizontal(classes='editor-actions'):
                yield Button('Сохранить карточку',id='card-save',variant='primary');yield Button('Отмена',id='card-cancel')
    def on_mount(self):self.query_one(DataTable).add_columns('Проверка','Поле','Ожидание');self.refresh_assertions()
    def statuses(self):
        try:values=[int(v) for v in re.split(r'[\s,;]+',self.query_one('#card-statuses',Input).value.strip()) if v]
        except ValueError:raise ValueError('Введите целые HTTP-статусы через запятую') from None
        if not values or any(not 100<=v<=599 for v in values):raise ValueError('Укажите ожидаемый HTTP-статус100–599')
        return values
    def refresh_assertions(self):
        table=self.query_one(DataTable);table.clear()
        for index,assertion in enumerate(self.card.get('assertions',[])):
            table.add_row(assertion['predicate'],assertion.get('path','Весь ответ'),
                json.dumps({k:v for k,v in assertion.items() if k not in {'predicate','path'}},ensure_ascii=False),key=str(index))
    def assertion(self,index=None):
        from api_agent.desktop.assertion_editor import AssertionEditor
        fields=self.state.response_fields(self.card['operation_id'],self.statuses())
        def saved(value):
            if value is None:return
            if index is None:self.card.setdefault('assertions',[]).append(value)
            else:self.card['assertions'][index]=value
            self.refresh_assertions()
        self.app.push_screen(AssertionEditor(self.state,fields,
            assertion=self.card['assertions'][index] if index is not None else None),saved)
    def scope_saved(self,value):
        if value is not None:
            self.card['where']=value;self.query_one('#scope-summary',Static).update(json.dumps(value,ensure_ascii=False))
    def negative_saved(self,value):
        if value is not None:
            self.card['negative']=value;self.query_one('#negative-summary',Static).update(json.dumps(value,ensure_ascii=False))
    def on_button_pressed(self,event):
        event.stop();key=event.button.id;table=self.query_one(DataTable);index=table.cursor_row
        try:
            if key=='card-cancel':self.dismiss(None)
            elif key=='assertion-add':self.assertion()
            elif key=='assertion-edit' and self.card.get('assertions'):self.assertion(index)
            elif key=='assertion-delete' and self.card.get('assertions'):
                self.card['assertions'].pop(index);self.refresh_assertions()
            elif key=='card-scope':
                from api_agent.desktop.scope_editor import ScopeEditor
                self.app.push_screen(ScopeEditor(self.state,self.card['operation_id'],self.card.get('where',{})),self.scope_saved)
            elif key=='negative-edit':
                from api_agent.desktop.scope_editor import NegativeEditor
                self.app.push_screen(NegativeEditor(self.state,self.card['operation_id'],self.card.get('negative')),self.negative_saved)
            elif key=='negative-clear':self.card.pop('negative',None);self.query_one('#negative-summary',Static).update('Нет')
            elif key=='card-save':
                candidate={**deepcopy(self.card),'statuses':self.statuses(),
                    'name':self.query_one('#card-name',Input).value,'enabled':self.query_one('#card-enabled',Checkbox).value,
                    'selector':self.query_one('#card-selector',Select).value,'auth':self.query_one('#card-auth',Select).value}
                self.state.compiled('Черновик условий',cards=[candidate]);self.dismiss(candidate)
        except ValueError as error:self.error(error)

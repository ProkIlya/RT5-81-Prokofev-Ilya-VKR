"""Карточка: ожидания ответа, отрицательный запрос и отбор фактического вызова."""
from api_agent.desktop.components.controls import Button, Input, Select, Summary as Static
from copy import deepcopy
import re
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Checkbox, Collapsible, DataTable, Label
from api_agent.desktop.builder.constructor_widgets import EditorDialog
from api_agent.desktop.presentation.presentation import assertion_summary, scope_summary, error_text


class CardEditor(EditorDialog):
    DEFAULT_CSS='''CardEditor #assertions-table {height: 5; max-height: 7;}
    CardEditor #card-selector, CardEditor #card-auth {max-width: 65;}
    CardEditor .editor-body {padding-right: 1;}'''
    def __init__(self,state,card):super().__init__();self.state=state;self.card=deepcopy(card)
    def compose(self):
        op=self.state.catalog.operations[self.card['operation_id']]
        with Vertical(classes='editor-shell'):
            yield Label(op['method']+' '+op['path'],classes='page-title')
            with VerticalScroll(classes='editor-body'):
                yield Checkbox('Условие включено',value=self.card.get('enabled',True),id='card-enabled')
                yield Label('Название карточки')
                yield Input(self.card.get('name',''),placeholder='Например: Проверка созданной поездки',id='card-name')
                yield Static('',id='card-name-error',classes='editor-error')
                yield Label('Ожидаемый HTTP-статус (несколько — через запятую)')
                yield Input(', '.join(map(str,self.card['statuses'])),placeholder='Например: 200 или 400, 422',id='card-statuses')
                yield Static('',id='card-status-error',classes='editor-error')
                yield DataTable(id='assertions-table',cursor_type='row')
                yield Static('',id='card-checks-error',classes='editor-error')
                with Horizontal(classes='editor-actions'):
                    yield Button('Добавить проверки',id='assertion-add');yield Button('Изменить',id='assertion-edit');yield Button('Удалить',id='assertion-delete')
                yield Static('',id='negative-summary',markup=False)
                with Collapsible(title='Какой вызов проверять',collapsed=True):
                    yield Label('Если операция выполнена несколько раз')
                    yield Select([('Последний подходящий','last'),('Первый подходящий','first'),('Все подходящие','all')],value=self.card.get('selector','last'),allow_blank=False,id='card-selector')
                    yield Static('Сначала отбираются запросы по операции, параметрам и авторизации. Затем выбирается первый, последний или каждый из них. Ожидаемый результат не участвует в отборе.',classes='field-help')
                    options=[('Без ограничения авторизации','any'),('Без авторизации','anonymous')]
                    if op['security']:options.append(('Текущая учётная запись','current'))
                    yield Select(options,value=self.card.get('auth','any'),allow_blank=False,id='card-auth')
                    if not op['security']:yield Static('Публичная операция по OpenAPI: текущая учётная запись для её отбора не требуется.',classes='field-help')
                    yield Static('Проверка успешного входа сама по себе не подтверждает авторизацию другого запроса.',classes='field-help')
                    yield Button('⚙ Параметры и источник значений',id='card-scope',
                        disabled=not any(self.state.request_fields(self.card['operation_id']).values()))
                    yield Static(scope_summary(self.card.get('where',{}),self.state.attachments),id='scope-summary',markup=False)
                yield Static('',classes='editor-error')
            with Horizontal(classes='editor-actions'):
                yield Button('Сохранить карточку',id='card-save',variant='primary');yield Button('Отмена',id='card-cancel')
    def on_mount(self):self.query_one(DataTable).add_columns('Проверки ответа');self.refresh_assertions()
    def statuses(self):
        try:values=[int(v) for v in re.split(r'[\s,;]+',self.query_one('#card-statuses',Input).value.strip()) if v]
        except ValueError:raise ValueError('Введите целые HTTP-статусы через запятую') from None
        if not values or any(not 100<=v<=599 for v in values):raise ValueError('Укажите ожидаемый HTTP-статус от 100 до 599')
        return values
    def on_input_changed(self,event):
        if event.input.id=='card-statuses' and self.is_mounted:
            try:self.statuses();self.query_one('#card-status-error',Static).update('')
            except ValueError as error:self.query_one('#card-status-error',Static).update(str(error))
    def refresh_assertions(self):
        table=self.query_one(DataTable);table.clear()
        from api_agent.desktop.components.controls import highlighted
        for index,item in enumerate(self.card.get('assertions',[])):table.add_row(highlighted(assertion_summary(item)),key=str(index))
        table.styles.height=min(6,max(2,len(self.card.get('assertions',[]))+1))
        negative=self.card.get('negative')
        self.query_one('#negative-summary',Static).update('Намеренно некорректный запрос: '+str(negative['path']) if negative else '')
    def assertion(self,index=None):
        from api_agent.desktop.builder.assertion_editor import AssertionEditor
        fields=self.state.response_fields(self.card['operation_id'],self.statuses())
        def saved(value):
            if value is None:return
            self.card['assertions']=value['assertions']
            if value['negative']:self.card['negative']=value['negative']
            else:self.card.pop('negative',None)
            self.refresh_assertions()
        self.app.push_screen(AssertionEditor(self.state,fields,assertions=self.card.get('assertions',[]),operation=self.card['operation_id'],statuses=self.statuses(),negative=self.card.get('negative')),saved)
    def scope_saved(self,value):
        if value is not None:self.card['where']=value;self.query_one('#scope-summary',Static).update(scope_summary(value,self.state.attachments))
    def on_key(self,event):
        if event.key=='backspace' and isinstance(self.focused,DataTable):
            self.on_button_pressed(Button.Pressed(self.query_one('#assertion-delete',Button)));event.stop();event.prevent_default()
        else:super().on_key(event)
    def on_button_pressed(self,event):
        event.stop();key=event.button.id;table=self.query_one(DataTable);index=table.cursor_row
        try:
            if key=='card-cancel':self.dismiss(None)
            elif key in {'assertion-add','assertion-edit'}:self.assertion(index if key=='assertion-edit' else None)
            elif key=='assertion-delete' and self.card.get('assertions'):self.card['assertions'].pop(index);self.refresh_assertions()
            elif key=='card-scope':
                from api_agent.desktop.builder.scope_editor import ScopeEditor
                self.app.push_screen(ScopeEditor(self.state,self.card['operation_id'],self.card.get('where',{})),self.scope_saved)
            elif key=='card-save':
                name=self.query_one('#card-name',Input).value.strip()
                if len(name)>200:
                    self.query_one('#card-name-error',Static).update('Название должно содержать не более 200 символов');return
                candidate={**deepcopy(self.card),'statuses':self.statuses(),'name':self.query_one('#card-name',Input).value.strip(),
                    'enabled':self.query_one('#card-enabled',Checkbox).value,'selector':self.query_one('#card-selector',Select).value,'auth':self.query_one('#card-auth',Select).value}
                self.state.compiled('Черновик условий',cards=[candidate]);self.dismiss(candidate)
        except ValueError as error:
            if key=='card-save':
                try:self.statuses()
                except ValueError:self.query_one('#card-status-error',Static).update(error_text(error))
                else:self.query_one('#card-checks-error',Static).update(error_text(error))
            else:self.error(error)

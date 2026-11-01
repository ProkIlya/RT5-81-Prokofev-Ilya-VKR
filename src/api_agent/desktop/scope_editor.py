"""Отбор HTTP-вызова с вводом значения и источника на одном экране."""
from copy import deepcopy
import json
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Label, Select, Static
from api_agent.desktop.widgets import PageDialog
from api_agent.desktop.constructor_state import typed_literal
from api_agent.goals.checks.condition_scope import validate_where, _compatible
from api_agent.desktop.presentation import scope_summary, error_text


class ScopeEditor(PageDialog):
    DEFAULT_CSS='''ScopeEditor .page-actions {height: 4;}
    ScopeEditor #scope-table {height: 7;}
    ScopeEditor #scope-readable {margin-top: 1;}'''
    def __init__(self,state,operation,where,*,references=True):
        super().__init__();self.state=state;self.operation=operation;self.where=deepcopy(where);self.references=references
        self.fields=state.request_fields(operation)
        self.options=[(('Параметр ' if location=='parameters' else 'Тело: ')+name,location+':'+name) for location,fields in self.fields.items() for name in fields]
        self.history_choices=[]
    def compose(self):
        with Vertical(classes='page-shell'):
            yield Label('Какой вызов проверять · '+self.operation,classes='page-title')
            with VerticalScroll(classes='page-body'):
                yield Static('Отбор выбирает запрос для проверки. Он не задаёт порядок выполнения. Ожидания ответа настраиваются отдельно.')
                yield DataTable(id='scope-table',cursor_type='row')
                yield Select(self.options,prompt='Параметр или поле запроса',id='scope-field')
                yield Select([('Указать значение','literal'),('Прикреплённый объект прошлого запуска','history')]+([('Из ответа этого запуска','response')] if self.references else []),value='literal',allow_blank=False,id='scope-source')
                yield Input(placeholder='Значение по типу поля; строки без кавычек',id='scope-value')
                yield Select([],prompt='Публичное значение прикреплённого объекта',id='scope-history')
                yield Select([(r['method']+' '+r['path'],r['id']) for r in self.state.operations()],prompt='Операция источника',id='binding-operation')
                yield Select([],prompt='Совместимое поле ответа',id='binding-field')
                yield Select([('Первый ответ источника','first'),('Последний ответ источника','last')],value='first',allow_blank=False,id='binding-occurrence')
                yield Label('Дополнительный отбор ответа источника (необязательно)',id='binding-where-label')
                yield Input('{}',placeholder='Например: {"body":{"start_charge":57}}',id='binding-where')
                yield Static('',id='scope-error',classes='editor-error')
                yield Static('',id='scope-readable',markup=False)
            with Horizontal(classes='page-actions'):
                yield Button('Добавить / заменить условие',id='scope-add',variant='primary')
                yield Button('Убрать условие',id='scope-remove')
            with Horizontal(classes='page-actions'):
                yield Button('Применить',id='scope-save',variant='primary');yield Button('Отмена',id='scope-cancel')
    def on_mount(self):
        self.query_one(DataTable).add_columns('Место','Поле','Значение / источник');self.refresh_rows();self.refresh_controls()
    def field(self):
        key=self.query_one('#scope-field',Select).value
        if key is Select.NULL:raise ValueError('Выберите поле запроса')
        location,name=key.split(':',1);return location,name,self.fields[location][name]
    def refresh_rows(self):
        table=self.query_one(DataTable);table.clear()
        for location,fields in self.where.items():
            for name,value in fields.items():table.add_row('Параметр' if location=='parameters' else 'Тело',name,scope_summary({location:{name:value}}),key=location+':'+name)
        self.query_one('#scope-readable',Static).update(scope_summary(self.where))
    def refresh_controls(self):
        source=self.query_one('#scope-source',Select).value
        self.query_one('#scope-value').display=source=='literal'
        self.query_one('#scope-history').display=source=='history'
        for key in ['binding-operation','binding-field','binding-occurrence','binding-where','binding-where-label']:self.query_one('#'+key).display=source=='response'
        try:
            _,_,schema=self.field()
            self.history_choices=self.state.attachment_choices(schema)
            self.query_one('#scope-history',Select).set_options([(f["path"]+' = '+str(f['value'])+' · '+f['source']['run_id'][:8],str(i)) for i,f in enumerate(self.history_choices)])
            operation=self.query_one('#binding-operation',Select).value
            fields={} if operation is Select.NULL else self.state.response_fields(operation,[])
            compatible=[(path or 'Корень',path) for path,s in fields.items() if s.get('type') in {'string','integer','number','boolean'} and _compatible(s,schema)]
            select=self.query_one('#binding-field',Select);previous=select.value;select.set_options(compatible)
            if previous in {value for _,value in compatible}:select.value=previous
        except ValueError:pass
    def on_select_changed(self,event):
        if self.is_mounted and event.select.id in {'scope-field','scope-source','binding-operation'}:self.refresh_controls()
    def on_data_table_row_selected(self,event):
        key=str(event.row_key.value);self.query_one('#scope-field',Select).value=key
        location,name=key.split(':',1);value=self.where[location][name]
        if isinstance(value,dict) and 'response' in value:
            ref=value['response'];self.query_one('#scope-source',Select).value='response'
            self.query_one('#binding-operation',Select).value=ref['operation_id']
            self.refresh_controls()
            self.query_one('#binding-field',Select).value=ref['path']
            self.query_one('#binding-occurrence',Select).value=ref.get('occurrence','first')
            self.query_one('#binding-where',Input).value=json.dumps(ref.get('where',{}),ensure_ascii=False)
        elif not isinstance(value,dict):
            self.query_one('#scope-source',Select).value='literal'
            self.query_one('#scope-value',Input).value=value if isinstance(value,str) else json.dumps(value)
    def on_button_pressed(self,event):
        event.stop();key=event.button.id
        try:
            if key=='scope-cancel':self.dismiss(None)
            elif key=='scope-save':
                validate_where(self.state.catalog,self.state.config.snapshot['operations'],self.operation,self.where,references=self.references)
                self.dismiss(deepcopy(self.where))
            elif key in {'scope-add','scope-remove'}:
                location,name,schema=self.field()
                if key=='scope-remove':
                    self.where.get(location,{}).pop(name,None)
                    if not self.where.get(location):self.where.pop(location,None)
                else:
                    source=self.query_one('#scope-source',Select).value
                    if source=='literal':value=typed_literal(self.query_one('#scope-value',Input).value,schema)
                    elif source=='history':
                        choice=self.query_one('#scope-history',Select).value
                        if choice is Select.NULL:raise ValueError('Выберите поле прикреплённого объекта')
                        value=deepcopy(self.history_choices[int(choice)]['value'])
                    else:
                        if location!='parameters':raise ValueError('Связь с ответом доступна для параметров запроса')
                        operation=self.query_one('#binding-operation',Select).value;path=self.query_one('#binding-field',Select).value
                        if operation is Select.NULL or path is Select.NULL:raise ValueError('Выберите операцию и поле источника')
                        ref={'operation_id':operation,'path':path,'occurrence':self.query_one('#binding-occurrence',Select).value}
                        where=json.loads(self.query_one('#binding-where',Input).value or '{}')
                        if where:
                            validate_where(self.state.catalog,self.state.config.snapshot['operations'],operation,where,references=False);ref['where']=where
                        value={'response':ref}
                    proposed=deepcopy(self.where);proposed.setdefault(location,{})[name]=value
                    validate_where(self.state.catalog,self.state.config.snapshot['operations'],self.operation,proposed,references=self.references)
                    self.where=proposed
                self.refresh_rows();self.query_one('#scope-error',Static).update('')
        except (ValueError,KeyError,IndexError) as error:self.query_one('#scope-error',Static).update(error_text(error))

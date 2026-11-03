"""Отбор HTTP-вызова с вводом значения и источника на одном экране."""
from api_agent.desktop.components.controls import Button, Input, Select, Summary as Static
from copy import deepcopy
import json
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Label
from api_agent.desktop.components.widgets import PageDialog
from api_agent.desktop.builder.constructor_state import typed_literal
from api_agent.goals.checks.condition_scope import validate_where, _compatible
from api_agent.desktop.builder.schema_examples import example
from api_agent.desktop.presentation.presentation import scope_summary, error_text


class ScopeEditor(PageDialog):
    DEFAULT_CSS='''ScopeEditor #scope-table {height: 4;}
    ScopeEditor #scope-columns {height: auto;}
    ScopeEditor #scope-fields {height: auto; width: 60%; padding-right: 2;}
    ScopeEditor #scope-readable {width: 1fr; padding: 0 1;}
    ScopeEditor.compact #scope-columns {layout: vertical;}
    ScopeEditor.compact #scope-fields, ScopeEditor.compact #scope-readable {width: 100%;}
    ScopeEditor #source-other {margin-bottom: 1;}'''
    def __init__(self,state,operation,where,*,references=True):
        super().__init__();self.state=state;self.operation=operation;self.where=deepcopy(where);self.references=references
        self.fields=state.request_fields(operation)
        self.options=[(('Параметр ' if location=='parameters' else 'Тело: ')+name,location+':'+name) for location,fields in self.fields.items() for name in fields]
        self.history_choices=[];self.show_other=not any(c.get('enabled',True) for c in state.cards)
    def compose(self):
        with Vertical(classes='page-shell'):
            yield Label('Какой вызов проверять · '+self.operation,classes='page-title')
            with VerticalScroll(classes='page-body'):
                yield Static('Отбор выбирает запрос для проверки. Он не задаёт порядок выполнения. Ожидания ответа настраиваются отдельно.')
                yield DataTable(id='scope-table',cursor_type='row')
                with Horizontal(id='scope-columns'):
                    with Vertical(id='scope-fields'):
                        yield Select(self.options,prompt='Параметр или поле запроса',id='scope-field')
                        yield Select([('Указать значение','literal'),('Прикреплённый объект прошлого запуска','history')]+([('Из ответа этого запуска','response')] if self.references else []),value='literal',allow_blank=False,id='scope-source')
                        yield Static('',id='scope-source-help',classes='field-help')
                        yield Input(placeholder='Значение по типу поля; строки без кавычек',id='scope-value')
                        yield Select([],prompt='Публичное значение прикреплённого объекта',id='scope-history')
                        yield Select([],prompt='Операция источника',id='binding-operation')
                        yield Button('Другие допустимые операции…',id='source-other')
                        yield Select([],prompt='Совместимое поле ответа',id='binding-field')
                        yield Select([('Первый ответ источника','first'),('Последний ответ источника','last')],value='first',allow_blank=False,id='binding-occurrence')
                        yield Label('Дополнительный отбор источника (необязательно)',id='binding-where-label')
                        yield Input('',placeholder='Например: {"body":{"start_charge":57}}',id='binding-where')
                        yield Static('Отбор по запросу источника: {"body":{"start_charge":57}}',id='binding-example',classes='field-help')
                        yield Static('',id='scope-error',classes='editor-error')
                    yield Static('',id='scope-readable',markup=False)
            with Horizontal(classes='page-actions'):
                yield Button('Добавить / заменить условие',id='scope-add',variant='primary')
                yield Button('Убрать условие',id='scope-remove')
            with Horizontal(classes='page-actions'):
                yield Button('Применить',id='scope-save',variant='primary');yield Button('Отмена',id='scope-cancel')
    def on_mount(self):
        self.query_one(DataTable).add_columns('Место','Поле','Значение / источник');self.refresh_rows();self.refresh_controls()
        self.set_class(self.size.width<95,'compact')
    def on_resize(self,event):self.set_class(event.size.width<95,'compact')
    def field(self):
        key=self.query_one('#scope-field',Select).value
        if key is Select.NULL:raise ValueError('Выберите поле запроса')
        location,name=key.split(':',1);return location,name,self.fields[location][name]
    def refresh_rows(self):
        table=self.query_one(DataTable);table.clear()
        for location,fields in self.where.items():
            for name,value in fields.items():table.add_row('Параметр' if location=='parameters' else 'Тело',name,scope_summary({location:{name:value}},self.state.attachments),key=location+':'+name)
        self.query_one('#scope-readable',Static).update(scope_summary(self.where,self.state.attachments))
    def refresh_controls(self):
        select=self.query_one('#scope-source',Select);source=select.value
        try:location,_,_=self.field()
        except ValueError:location=None
        options=[('Указать значение','literal'),('Прикреплённый объект прошлого запуска','history')]
        if self.references and location in {None,'parameters'}:options.append(('Из ответа этого запуска','response'))
        if options!=getattr(self,'source_options',None):
            self.source_options=options
            with select.prevent(Select.Changed):
                select.set_options(options);select.value=source if source in {v for _,v in options} else 'literal'
            source=select.value
        self.query_one('#scope-value').display=source=='literal'
        self.query_one('#scope-history').display=source=='history'
        for key in ['binding-operation','binding-field','binding-occurrence','binding-where','binding-where-label','binding-example','source-other']:self.query_one('#'+key).display=source=='response'
        try:
            location,_,schema=self.field()
            self.history_choices=self.state.attachment_choices(schema)
            historical=self.query_one('#scope-history',Select)
            historical.set_options([(f["path"]+' = '+str(f['value'])+' · '+f['source']['run_id'][:8],str(i)) for i,f in enumerate(self.history_choices)])
            historical.disabled=not self.history_choices
            source_select=self.query_one('#binding-operation',Select);old=source_select.value
            primary={c['operation_id'] for c in self.state.cards if c.get('enabled',True)}
            choices=[]
            for row in self.state.operations():
                if primary and not self.show_other and row['id'] not in primary:continue
                try:fields=self.state.response_fields(row['id'],[])
                except ValueError:continue
                if any(s.get('type') in {'string','integer','number','boolean'} and _compatible(s,schema) for s in fields.values()):
                    choices.append((row['method']+' '+row['path'],row['id']))
            choices.sort(key=lambda row:row[1] not in primary)
            if choices!=getattr(self,'binding_choices',None):
                self.binding_choices=choices;source_select.set_options(choices)
                if old in {value for _,value in choices}:source_select.value=old
            self.query_one('#source-other').display=source=='response' and bool(primary) and not self.show_other
            operation=source_select.value
            fields={} if operation is Select.NULL else self.state.response_fields(operation,[])
            compatible=[(path or 'Корень',path) for path,s in fields.items() if s.get('type') in {'string','integer','number','boolean'} and _compatible(s,schema)]
            select=self.query_one('#binding-field',Select);previous=select.value;select.set_options(compatible)
            if previous in {value for _,value in compatible}:select.value=previous
            if operation is not Select.NULL:
                inputs=self.state.request_fields(operation)
                area='body' if inputs['body'] else 'parameters';sample={}
                if inputs[area]:
                    key,field=next(iter(inputs[area].items()));sample={area:{key:example(field)}}
                self.query_one('#binding-example',Static).update('Пример отбора запроса источника: '+json.dumps(sample,ensure_ascii=False))
            self.query_one('#binding-operation',Select).disabled=not choices
            self.query_one('#binding-field',Select).disabled=not compatible
            help_text=('Нет прикреплённых совместимых данных. Откройте завершённый запуск и отметьте поле ответа в HTTP-инспекторе.'
                if source=='history' and not self.history_choices else
                'Нет операций с совместимым полем ответа. Выберите другой параметр или укажите значение.'
                if source=='response' and not choices else
                'Ссылка на будущий ответ этого запуска. Данные появятся только после реального HTTP-вызова источника.'
                if source=='response' else '')
            self.query_one('#scope-source-help',Static).update(help_text)
            self.query_one('#scope-add').disabled=(source=='history' and not self.history_choices) or (source=='response' and not choices)
        except ValueError:
            self.query_one('#scope-source-help',Static).update('Сначала выберите параметр или поле запроса.')
    def on_select_changed(self,event):
        if self.is_mounted and event.select.id in {'scope-field','scope-source','binding-operation'}:self.refresh_controls()
    def on_data_table_row_selected(self,event):
        key=str(event.row_key.value);self.query_one('#scope-field',Select).value=key
        self.refresh_controls()
        location,name=key.split(':',1);value=self.where[location][name]
        if isinstance(value,dict) and 'response' in value:
            self.show_other=True
            ref=value['response'];self.query_one('#scope-source',Select).value='response'
            self.refresh_controls()
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
            if key=='source-other':self.show_other=True;self.refresh_controls()
            elif key=='scope-cancel':self.dismiss(None)
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
    def on_key(self,event):
        if event.key=='backspace' and isinstance(self.focused,DataTable):
            table=self.query_one(DataTable)
            if table.row_count:
                key=str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
                self.query_one('#scope-field',Select).value=key
                self.on_button_pressed(Button.Pressed(self.query_one('#scope-remove',Button)))
            event.stop();event.prevent_default()
        else:super().on_key(event)

"""Несколько проверок в одном редакторе, JSON коллекции и отрицательный запрос."""
from copy import deepcopy
import json
from textual.containers import Vertical, VerticalScroll, Horizontal
from textual.widgets import Button, Input, Label, Select, Static, TextArea, DataTable
from api_agent.desktop.widgets import PageDialog
from api_agent.desktop.constructor_state import typed_literal
from api_agent.desktop.presentation import assertion_summary, error_text
from api_agent.goals.checks.selected_compile import _assertion, _negative

PREDICATES=[('Равно','equals'),('Не равно','not_equals'),('Поле существует','exists'),
 ('Ответ соответствует схеме','schema'),('Число с округлением','number_rounded'),('Число с допуском','number_within'),('Коллекция JSON','collection')]


def collection_rows(value):
    if not isinstance(value,list):raise ValueError('Ожидается JSON-массив, например [{"id": 1}]')
    def flatten(item,path=''):
        if isinstance(item,dict):
            result={}
            for k,v in item.items():result.update(flatten(v,path+'/'+str(k).replace('~','~0').replace('/','~1')))
            return result
        if isinstance(item,list):raise ValueError('В строке коллекции выберите скалярные поля, без вложенных массивов')
        return {path:item}
    return [flatten(row) for row in value]


def rows_json(rows):
    result=[]
    for row in rows:
        if '' in row:result.append(row['']);continue
        value={}
        for path,item in row.items():
            parts=[s.replace('~1','/').replace('~0','~') for s in path.lstrip('/').split('/')];node=value
            for part in parts[:-1]:node=node.setdefault(part,{})
            node[parts[-1]]=item
        result.append(value)
    return json.dumps(result,ensure_ascii=False,indent=2)


class AssertionEditor(PageDialog):
    DEFAULT_CSS='''
    AssertionEditor #response-editor, AssertionEditor #request-editor {height: auto;}
    AssertionEditor #collection-json {height: 10;}
    AssertionEditor #batch-assertions {height: 8;}
    '''
    def __init__(self,state,fields,*,assertion=None,assertions=None,operation=None,statuses=None,negative=None):
        super().__init__();self.state=state;self.fields=fields;self.operation=operation;self.statuses=statuses or []
        self.items=deepcopy(assertions if assertions is not None else ([assertion] if assertion else []))
        self.negative=deepcopy(negative);self.editing=None
    def compose(self):
        with Vertical(classes='page-shell'):
            yield Label('Проверки карточки',classes='page-title')
            with VerticalScroll(classes='page-body'):
                yield Select([('Проверка ответа','response'),('Проверка запроса: намеренное нарушение схемы','request')],value='response',allow_blank=False,id='assertion-target')
                with Vertical(id='response-editor'):
                    yield Select([x for x in PREDICATES if x[1]!='collection' or any(s.get('type')=='array' for s in self.fields.values())],value='equals',allow_blank=False,id='assertion-kind')
                    yield Select([],prompt='Поле JSON-ответа',id='assertion-path')
                    yield Static('Корень — всё JSON-тело ответа. /0/name — поле первого элемента массива.',classes='field-help')
                    yield Static('',id='assertion-path-error',classes='editor-error')
                    yield Input(placeholder='Ожидаемое значение; строка без кавычек',id='assertion-value')
                    yield Static('',id='assertion-value-error',classes='editor-error')
                    yield Input(placeholder='Ожидаемое число, например 0.10',id='assertion-number')
                    yield Input(placeholder='Допуск больше нуля, например 0.01',id='assertion-tolerance')
                    yield Select([('Содержит ожидаемые элементы','includes'),('Точное совпадение выбранных полей','exact')],value='includes',allow_blank=False,id='collection-mode')
                    yield TextArea('[]',language='json',id='collection-json')
                    yield Static('JSON-массив. Пример: [{"id": 1, "name": "Пример"}]. Порядок не важен; повторы требуют разных фактических элементов.',id='collection-help',classes='field-help')
                    with Horizontal(classes='page-actions'):
                        yield Button('Добавить проверку',id='assertion-add',variant='primary')
                        yield Button('Новая строка',id='assertion-new')
                with Vertical(id='request-editor'):
                    yield Static('Разрешить одно намеренное нарушение схемы перед отправкой HTTP. В карточке должен ожидаться ответ 4xx. Остальные проверки запроса сохраняются.')
                    yield Select([],prompt='Поле тела запроса',id='negative-field')
                    yield Select([('Пропустить обязательное поле','required'),('Неверный тип','schema_type'),('Значение вне enum','enum')],value='required',allow_blank=False,id='negative-rule')
                    yield Static('',id='negative-error',classes='editor-error')
                    with Horizontal(classes='page-actions'):
                        yield Button('Добавить нарушение',id='negative-add')
                        yield Button('Убрать нарушение',id='negative-clear')
                yield DataTable(id='batch-assertions',cursor_type='row')
                yield Static('',id='batch-negative',markup=False)
                with Horizontal(classes='page-actions'):
                    yield Button('Изменить выбранную',id='batch-edit');yield Button('Удалить выбранную',id='batch-delete')
                yield Static('',classes='editor-error',id='batch-error')
            with Horizontal(classes='page-actions'):
                yield Button('Применить проверки',id='assertion-save',variant='primary');yield Button('Отмена',id='assertion-cancel')
    def on_mount(self):
        self.query_one('#batch-assertions',DataTable).add_columns('Добавленные проверки')
        if self.operation:
            fields=self.state.request_fields(self.operation)['body']
            self.query_one('#negative-field',Select).set_options([(name,'/'+name.replace('~','~0').replace('/','~1')) for name in fields])
        self.refresh_controls();self.refresh_rows()
    def refresh_rows(self):
        table=self.query_one('#batch-assertions',DataTable);table.clear()
        for i,item in enumerate(self.items):table.add_row(assertion_summary(item),key=str(i))
        self.query_one('#batch-negative',Static).update('Отрицательный сценарий запроса: '+str(self.negative['path']) if self.negative else 'Намеренные нарушения запроса не заданы')
    def edit_row(self,index):
        self.editing=index
        self.query_one('#assertion-add',Button).label='Добавить проверку' if index is None else 'Сохранить изменения'
    def refresh_controls(self):
        target=self.query_one('#assertion-target',Select).value
        self.query_one('#response-editor').display=target=='response';self.query_one('#request-editor').display=target=='request'
        kind=self.query_one('#assertion-kind',Select).value
        kinds=({'array'} if kind=='collection' else {'integer','number'} if kind.startswith('number_')
            else {'string','integer','number','boolean','null'} | ({'object','array'} if kind=='exists' else set()))
        choices=[(p or 'Корень JSON-тела',p) for p,s in self.fields.items() if s.get('type') in kinds]
        field=self.query_one('#assertion-path',Select);old=field.value;field.set_options(choices)
        if old in dict((v,k) for k,v in choices):field.value=old
        field.display=kind!='schema'
        for key in ['assertion-value']:self.query_one('#'+key).display=kind in {'equals','not_equals'}
        self.query_one('#assertion-number').display=kind.startswith('number_')
        self.query_one('#assertion-tolerance').display=kind=='number_within'
        for key in ['collection-mode','collection-json','collection-help']:self.query_one('#'+key).display=kind=='collection'
    def on_select_changed(self,event):
        if self.is_mounted and event.select.id in {'assertion-target','assertion-kind'}:self.refresh_controls()
    def item(self):
        kind=self.query_one('#assertion-kind',Select).value;value={'predicate':kind}
        if kind!='schema':
            path=self.query_one('#assertion-path',Select).value
            if path is Select.NULL:raise ValueError('Выберите поле JSON-ответа')
            value['path']=path
        if kind in {'equals','not_equals'}:value['value']=typed_literal(self.query_one('#assertion-value',Input).value,self.fields[value['path']])
        elif kind.startswith('number_'):
            value['expected']=self.query_one('#assertion-number',Input).value
            if kind=='number_within':value['tolerance']=self.query_one('#assertion-tolerance',Input).value
        elif kind=='collection':
            value.update(mode=self.query_one('#collection-mode',Select).value,rows=collection_rows(json.loads(self.query_one('#collection-json',TextArea).text)))
        _assertion(value,self.fields.get(''));return value
    def on_button_pressed(self,event):
        event.stop();key=event.button.id
        try:
            if key=='assertion-cancel':self.dismiss(None)
            elif key=='assertion-add':
                item=self.item()
                if self.editing is None and len(self.items)>=16:raise ValueError('В карточке допускается не более 16 проверок')
                if self.editing is None:self.items.append(item)
                elif 0<=self.editing<len(self.items):self.items[self.editing]=item
                else:raise ValueError('Редактируемая проверка удалена. Начните новую строку.')
                self.edit_row(None);self.refresh_rows();self.query_one('#batch-error',Static).update('')
            elif key=='assertion-new':self.edit_row(None);self.query_one('#assertion-value',Input).value=''
            elif key=='batch-delete' and self.items:
                removed=self.query_one('#batch-assertions',DataTable).cursor_row
                self.items.pop(removed)
                if self.editing==removed:self.edit_row(None)
                elif self.editing is not None and removed<self.editing:self.edit_row(self.editing-1)
                self.refresh_rows()
            elif key=='batch-edit' and self.items:
                self.edit_row(self.query_one('#batch-assertions',DataTable).cursor_row);item=self.items[self.editing]
                self.query_one('#assertion-target',Select).value='response';self.query_one('#assertion-kind',Select).value=item['predicate'];self.refresh_controls()
                if 'path' in item:self.query_one('#assertion-path',Select).value=item['path']
                v=item.get('value','');self.query_one('#assertion-value',Input).value=v if isinstance(v,str) else json.dumps(v)
                self.query_one('#assertion-number',Input).value=str(item.get('expected',''))
                self.query_one('#assertion-tolerance',Input).value=str(item.get('tolerance',''))
                if item['predicate']=='collection':self.query_one('#collection-mode',Select).value=item['mode'];self.query_one('#collection-json',TextArea).text=rows_json(item['rows'])
            elif key=='negative-clear':self.negative=None;self.refresh_rows()
            elif key=='negative-add':
                value={'location':'body','path':self.query_one('#negative-field',Select).value,'rule':self.query_one('#negative-rule',Select).value}
                _negative(value,self.state.catalog,self.operation,self.statuses);self.negative=value;self.refresh_rows();self.query_one('#negative-error',Static).update('')
            elif key=='assertion-save':self.dismiss({'assertions':deepcopy(self.items),'negative':deepcopy(self.negative)})
        except (ValueError,KeyError,TypeError) as error:
            self.query_one('#negative-error' if key=='negative-add' else '#assertion-value-error',Static).update(error_text(error))

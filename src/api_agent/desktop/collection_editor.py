"""Таблица выбранных scalar столбцов; строки/дубликаты не нормализуются в set."""
from copy import deepcopy
import json
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from api_agent.desktop.constructor_state import schema_fields, typed_literal
from api_agent.desktop.constructor_widgets import EditorDialog, FieldPicker, LiteralDialog


class RowEditor(EditorDialog):
    def __init__(self,state,fields,columns,row=None):
        super().__init__();self.state=state;self.fields=fields;self.columns=columns;self.row=row or {}
    def compose(self):
        with Vertical(classes='editor-shell'):
            yield Label('Строка: все значения относятся к одному элементу коллекции')
            with VerticalScroll(classes='editor-body'):
                for index,path in enumerate(self.columns):
                    value=self.row.get(path,'')
                    yield Label(path+' · '+str(self.fields[path].get('type')))
                    yield Input(value if isinstance(value,str) else json.dumps(value,ensure_ascii=False),id='row-value-'+str(index))
                    yield Button('Из прикреплённых данных',id='row-attachment-'+str(index))
                yield Static('',classes='editor-error')
            with Horizontal(classes='editor-actions'):
                yield Button('Сохранить строку',id='row-save',variant='primary');yield Button('Отмена',id='row-cancel')
    def on_button_pressed(self,event):
        event.stop();key=event.button.id
        try:
            if key=='row-cancel':self.dismiss(None)
            elif key=='row-save':self.dismiss({path:typed_literal(self.query_one('#row-value-'+str(i),Input).value,
                self.fields[path]) for i,path in enumerate(self.columns)})
            elif key.startswith('row-attachment-'):
                index=int(key.rsplit('-',1)[1]);path=self.columns[index]
                def selected(value):
                    if value is not None:
                        v=value['value'];self.query_one('#row-value-'+str(index),Input).value=v if isinstance(v,str) else json.dumps(v,ensure_ascii=False)
                self.app.push_screen(LiteralDialog(self.state,self.fields[path],title=path),selected)
        except ValueError as error:self.error(error)


class CollectionEditor(EditorDialog):
    def __init__(self,state,schema,rows,mode):
        super().__init__();self.state=state;self.fields=schema_fields(schema['items'])
        self.rows=deepcopy(rows);self.mode=mode;self.columns=list(dict.fromkeys(p for row in rows for p in row))
    def compose(self):
        with Vertical(classes='editor-shell'):
            yield Label('Коллекция · столбцы и типизированные строки')
            with VerticalScroll(classes='editor-body'):
                yield Select([('Содержит ожидаемые элементы','includes'),('Точно ожидаемые элементы','exact')],
                    value=self.mode,allow_blank=False,id='collection-mode')
                yield Static('Порядок строк не важен. Повтор строки требует отдельного фактического элемента. Exact требует полноты коллекции.')
                yield DataTable(id='collection-table',cursor_type='cell')
                with Horizontal(classes='editor-actions'):
                    yield Button('+ Столбец',id='column-add');yield Button('− Столбец',id='column-delete')
                with Horizontal(classes='editor-actions'):
                    yield Button('+ Строка',id='row-add');yield Button('Изменить строку',id='row-edit');yield Button('− Строка',id='row-delete')
                yield Static('',classes='editor-error')
            with Horizontal(classes='editor-actions'):
                yield Button('Применить таблицу',id='collection-save',variant='primary');yield Button('Отмена',id='collection-cancel')
    def on_mount(self):self.refresh_table()
    def refresh_table(self):
        table=self.query_one(DataTable);table.clear(columns=True)
        for path in self.columns:table.add_column(path or 'Элемент',key=path or '__root__')
        for index,row in enumerate(self.rows):table.add_row(*(json.dumps(row[p],ensure_ascii=False)
            if p in row else 'Не задано' for p in self.columns),key=str(index))
    def column_selected(self,path):
        if path is None or path in self.columns:return
        if len(self.columns)>=16:self.error('Не более16 столбцов');return
        def add(value=None):
            if self.rows and value is None:return
            self.columns.append(path)
            for row in self.rows:row[path]=deepcopy(value['value'])
            self.refresh_table()
        if self.rows:self.app.push_screen(LiteralDialog(self.state,self.fields[path],
            title='Явное значение нового столбца для существующих строк'),add)
        else:add()
    def edit_row(self,index=None):
        if not self.columns:raise ValueError('Сначала добавьте столбец из схемы элемента')
        if index is None and len(self.rows)>=32:raise ValueError('Не более32 строк')
        def saved(row):
            if row is None:return
            if index is None:self.rows.append(row)
            else:self.rows[index]=row
            self.refresh_table()
        self.app.push_screen(RowEditor(self.state,self.fields,self.columns,self.rows[index] if index is not None else None),saved)
    def on_button_pressed(self,event):
        event.stop();key=event.button.id;table=self.query_one(DataTable)
        try:
            if key=='collection-cancel':self.dismiss(None)
            elif key=='column-add':self.app.push_screen(FieldPicker(self.fields,kinds={'string','integer','number','boolean'},
                title='Столбец относительно одного элемента'),self.column_selected)
            elif key=='column-delete' and self.columns:
                path=self.columns.pop(min(table.cursor_column,len(self.columns)-1))
                for row in self.rows:row.pop(path,None)
                if not self.columns:self.rows=[]
                self.refresh_table()
            elif key=='row-add':self.edit_row()
            elif key=='row-edit' and self.rows:self.edit_row(table.cursor_row)
            elif key=='row-delete' and self.rows:self.rows.pop(table.cursor_row);self.refresh_table()
            elif key=='collection-save':
                mode=self.query_one(Select).value
                if mode=='includes' and not self.rows:raise ValueError('Includes требует хотя бы одну строку')
                self.dismiss({'mode':mode,'rows':deepcopy(self.rows)})
        except ValueError as error:self.error(error)

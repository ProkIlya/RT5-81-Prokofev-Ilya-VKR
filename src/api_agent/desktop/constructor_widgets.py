"""Общие controls конструктора: schema picker и явное типизированное значение."""
import json
from textual.containers import Vertical, VerticalScroll, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from api_agent.desktop.constructor_state import typed_literal


class EditorDialog(ModalScreen):
    DEFAULT_CSS = '''
    EditorDialog {align: center middle; background: $background 70%;}
    .editor-shell {width: 100; max-width: 98%; height: 92%; border: round $accent; padding: 1 2; background: $surface;}
    .editor-body {height: 1fr;}
    .editor-actions {height: auto; layout: horizontal;}
    .editor-actions Button {margin-right: 1; min-width: 8;}
    EditorDialog DataTable {height: 8; margin-bottom: 1;}
    EditorDialog Input, EditorDialog Select {margin-bottom: 1;}
    EditorDialog Static {height: auto;}
    .editor-error {color: $error; height: auto;}
    '''
    BINDINGS=[('escape','back','Отмена')]
    def action_back(self):self.dismiss(None)
    def error(self,value):self.query_one('.editor-error',Static).update(str(value))


class FieldPicker(EditorDialog):
    def __init__(self,fields,*,kinds=None,title='Поле ответа'):
        super().__init__();self.fields={p:s for p,s in fields.items() if not kinds or s.get('type') in kinds}
        self.picker_title=title
    def compose(self):
        with Vertical(classes='editor-shell'):
            yield Label(self.picker_title)
            yield Input(placeholder='Поиск имени / пути поля',id='field-search')
            yield DataTable(id='field-table',cursor_type='row')
            yield Static('Enter — выбрать; Escape — отменить')
            yield Static('',classes='editor-error')
            yield Button('Назад',id='field-back')
    def on_mount(self):
        self.query_one(DataTable).add_columns('Поле','Тип');self.refresh_rows()
    def refresh_rows(self):
        table=self.query_one(DataTable);table.clear();search=self.query_one(Input).value.casefold()
        for path,schema in self.fields.items():
            if search in path.casefold():table.add_row(path or 'Корень ответа',str(schema.get('type')),key=path or '__root__')
    def on_input_changed(self,event):
        if self.is_mounted:self.refresh_rows()
    def on_data_table_row_selected(self,event):
        path=str(event.row_key.value);self.dismiss('' if path=='__root__' else path)
    def on_button_pressed(self,event):event.stop();self.dismiss(None)


class LiteralDialog(EditorDialog):
    def __init__(self,state,schema,*,value=None,title='Значение поля'):
        super().__init__();self.state=state;self.schema=schema;self.literal_title=title;self.initial=value
        self.choices=state.attachment_choices(schema)
    def compose(self):
        with Vertical(classes='editor-shell'):
            yield Label(self.literal_title+' · '+str(self.schema.get('type')))
            with VerticalScroll(classes='editor-body'):
                yield Static('Строки — без кавычек. Boolean: true/false. Nullable: null. Значение0 и false не означают отсутствие.')
                value=self.initial if isinstance(self.initial,str) else json.dumps(self.initial,ensure_ascii=False) if self.initial is not None else ''
                yield Input(value=value,id='literal-value')
                yield Button('Из прикреплённых данных',id='literal-attachment',disabled=not self.choices)
                yield Static('',id='literal-origin')
                yield Static('',classes='editor-error')
            with Horizontal(classes='editor-actions'):
                yield Button('Применить значение',id='literal-apply',variant='primary');yield Button('Отмена',id='literal-cancel')
    def chosen(self,key):
        if key is None:return
        field=self.choices[int(key)];value=field['value']
        self.query_one(Input).value=value if isinstance(value,str) else json.dumps(value,ensure_ascii=False)
        self.query_one('#literal-origin',Static).update('Исторический literal · '+json.dumps(field['source'],ensure_ascii=False))
    def on_button_pressed(self,event):
        event.stop();key=event.button.id
        if key=='literal-cancel':self.dismiss(None)
        elif key=='literal-attachment':
            from api_agent.desktop.dialogs import ChoiceDialog
            self.app.push_screen(ChoiceDialog('Выберите совместимое публичное значение',[(str(i),
                f"{f['path']} = {f['value']} · Run {f['source'].get('run_id','')}" ) for i,f in enumerate(self.choices)]),self.chosen)
        elif key=='literal-apply':
            try:self.dismiss({'value':typed_literal(self.query_one(Input).value,self.schema)})
            except ValueError as error:self.error(error)

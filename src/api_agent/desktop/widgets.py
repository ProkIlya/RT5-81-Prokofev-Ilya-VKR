"""Полноэкранные страницы, подсветка и доступные разделители панелей."""
import json
from rich.syntax import Syntax
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Button, DataTable, Input, Select, Static, TextArea


class PageDialog(ModalScreen):
    DEFAULT_CSS='''
    PageDialog {align: left top; background: $background;}
    .page-shell, .editor-shell {width: 100%; height: 100%; background: $surface; padding: 1 2;}
    .page-title {height: 2; text-style: bold; margin-bottom: 1; color: $accent;}
    .page-body, .editor-body {height: 1fr; scrollbar-size: 1 1;}
    .page-actions, .editor-actions {height: auto; layout: horizontal; padding-top: 1;}
    PageDialog Button {margin-right: 1; min-width: 8; border: none; height: 3;}
    PageDialog Input, PageDialog Select {margin-bottom: 1;}
    PageDialog Static {height: auto;}
    .editor-error, .form-error {color: $error; height: auto;}
    .field-help {color: $text-muted; margin-bottom: 1;}
    PageDialog DataTable {height: 10; margin-bottom: 1;}
    '''
    BINDINGS=[('escape','back','Назад')]
    def action_back(self):self.dismiss(None)
    def on_key(self,event):
        focused=self.focused;key=event.key
        if isinstance(focused,Button) and key in {'left','right','up','down'}:
            (self.focus_previous if key in {'left','up'} else self.focus_next)();event.stop();event.prevent_default()
        elif isinstance(focused,Input) and key in {'up','down'}:
            (self.focus_previous if key=='up' else self.focus_next)();event.stop();event.prevent_default()
        elif isinstance(focused,DataTable) and ((key=='up' and focused.cursor_row==0) or
                (key=='down' and focused.cursor_row>=focused.row_count-1)):
            searches={'operations':'#op-search','operation-table':'#operation-search'}
            if key=='up' and focused.id in searches:
                self.query_one(searches[focused.id]).focus();event.stop();event.prevent_default();return
            if key=='down' and focused.id in searches:
                self.query_one('#op-back' if focused.id=='operations' else '#operation-cancel').focus()
                event.stop();event.prevent_default();return
            (self.focus_previous if key=='up' else self.focus_next)();event.stop();event.prevent_default()
        elif isinstance(focused,Input) and key=='right' and focused.cursor_position==len(focused.value):
            # Выход в соседний фильтр только на границе текста.
            self.focus_next();event.stop();event.prevent_default()


class JsonCode(Static):
    def __init__(self,value=None,**kwargs):
        super().__init__('',markup=False,**kwargs);self.value=value
    def on_mount(self):self.show(self.value)
    def show(self,value):
        self.value=value
        self.update(Syntax(json.dumps(value,ensure_ascii=False,indent=2), 'json',
            theme='ansi_light' if not self.app.current_theme.dark else 'ansi_dark',word_wrap=True))


class SplitHandle(Widget,can_focus=True):
    DEFAULT_CSS='''
    SplitHandle {height: 1; width: 100%; background: $panel; color: $text-muted;}
    SplitHandle.vertical {width: 1; height: 100%;}
    SplitHandle:hover, SplitHandle:focus {background: $accent;}
    '''
    def __init__(self,target,*,vertical=False,**kwargs):
        super().__init__(classes='vertical' if vertical else '',**kwargs)
        self.target=target;self.vertical=vertical;self.dragging=False
        self.tooltip='Изменить размер: перетащите мышью или используйте стрелки'
    def render(self):return '│' if self.vertical else '─'
    def change(self,delta):
        target=self.app.query_one(self.target)
        current=target.size.width if self.vertical else target.size.height
        limit=self.app.size.width-22 if self.vertical else self.app.size.height-12
        value=max(8,min(limit,current+delta))
        if self.vertical:target.styles.width=value
        else:target.styles.height=value
    def on_mouse_down(self,event):self.dragging=True;self.capture_mouse();event.stop()
    def on_mouse_move(self,event):
        if self.dragging:self.change(event.delta.x if self.vertical else -event.delta.y);event.stop()
    def on_mouse_up(self,event):self.dragging=False;self.release_mouse();event.stop()
    def on_key(self,event):
        if event.key in {'left','right','up','down'}:
            self.change(-2 if event.key in {'left','down'} else 2);event.stop();event.prevent_default()

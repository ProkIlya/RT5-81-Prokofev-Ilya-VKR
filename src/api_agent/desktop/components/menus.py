"""Небольшое меню у строки/кнопки; Esc и щелчок снаружи возвращают фокус."""
from rich.text import Text
from textual.containers import Vertical
from textual.geometry import Offset
from textual.screen import ModalScreen
from textual.widgets import Label, OptionList
from textual.widgets.option_list import Option
from api_agent.desktop.components.controls import highlighted


class PopupMenu(ModalScreen):
    DEFAULT_CSS='''
    PopupMenu {background: transparent; align: left top;}
    PopupMenu #popup-box {width: 38; height: auto; max-height: 18; padding: 0 1;
        background: $surface; border: solid $accent;}
    PopupMenu #popup-title {height: 1; color: $text-muted;}
    PopupMenu OptionList {height: auto; max-height: 14; padding: 0; border: none;}
    '''
    BINDINGS=[('escape','close','Закрыть'),('left','close','Закрыть')]
    def __init__(self,title,options,anchor,*,selected=None,point=None):
        super().__init__();self.heading=title;self.options=options;self.anchor=anchor
        self.selected=selected;self.point=point
    def compose(self):
        with Vertical(id='popup-box'):
            yield Label(self.heading,id='popup-title')
            yield OptionList(*(Option(Text('✓ ' if key==self.selected else '  ')+highlighted(label),id=str(key))
                for key,label in self.options),id='choices')
    def on_mount(self):
        self.position_menu();options=self.query_one(OptionList)
        if self.selected is not None:
            keys=[key for key,_ in self.options]
            if self.selected in keys:options.highlighted=keys.index(self.selected)
        options.focus()
    def position_menu(self):
        # Рамки, padding, маркер выбора и scrollbar занимают до восьми ячеек.
        box=self.query_one('#popup-box');width=min(60,max(28,max((Text(str(label)).cell_len for _,label in self.options),default=22)+9))
        width=min(width,self.size.width);height=min(18,len(self.options)+3,self.size.height)
        region=self.anchor.region
        x,y=self.point if self.point else (region.x,region.bottom)
        if y+height>self.size.height:y=max(0,(self.point[1] if self.point else region.y)-height)
        box.styles.width=width;box.styles.max_height=max(4,self.size.height)
        box.styles.offset=Offset(max(0,min(x,self.size.width-width)),max(0,y))
    def on_resize(self):
        if self.is_mounted:self.position_menu()
    def on_option_list_option_selected(self,event):event.stop();self.dismiss(event.option.id)
    def on_mouse_down(self,event):
        if not self.query_one('#popup-box').region.contains(event.screen_x,event.screen_y):
            event.stop();self.dismiss(None)
    def action_close(self):self.dismiss(None)

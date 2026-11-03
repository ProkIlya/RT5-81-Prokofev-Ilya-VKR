"""Клавиатурная памятка: одно действие в строке, без меню выбора команд."""
from rich.text import Text
from textual.containers import Vertical, VerticalScroll, Horizontal
from textual.widgets import Label, Static
from api_agent.desktop.components.controls import Button
from api_agent.desktop.components.widgets import PageDialog


class HelpDialog(PageDialog):
    DEFAULT_CSS='''HelpDialog {align: center middle; background: $background 70%;}
    HelpDialog .page-shell {width: 68; max-width: 100%; height: 100%; max-height: 32;}
    HelpDialog .key-row {height: 1; margin-bottom: 1;}'''
    def compose(self):
        with Vertical(classes='page-shell'):
            yield Label('Клавиши и управление',classes='page-title')
            with VerticalScroll(classes='page-body'):
                for keys,description in [('↑ ↓ ← →','Перемещение между элементами'),('Tab / Shift+Tab','Следующее / предыдущее поле'),
                    ('Enter','Выбрать действие'),('Esc','Закрыть меню или окно'),('F2','Панель проектов'),('F3','Показать / скрыть инспектор'),
                    ('F5','Отправить / остановить / продолжить'),('ПКМ / → / Shift+F10','Меню проекта или сессии'),
                    ('Space','Прикрепить поле HTTP-ответа'),('Backspace','Удалить выбранную проверку / условие'),
                    ('Ctrl+P','Команды приложения'),('Ctrl+S','Сохранить ввод'),('Ctrl+Q','Выйти')]:
                    yield Static(Text(keys.ljust(23),style='bold cyan')+Text(description),classes='key-row')
            with Horizontal(classes='page-actions'):yield Button('Назад',id='help-close')
    def on_button_pressed(self,event):event.stop();self.dismiss(None)

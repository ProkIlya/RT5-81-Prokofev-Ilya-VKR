"""Контекстная навигация и настройки приложения, без постоянно видимой панели CRUD."""
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Tree, Button, Label, Input, Select, Static
from rich.text import Text
from api_agent.desktop.dialogs import ChoiceDialog, FormDialog
from api_agent.desktop.widgets import PageDialog

DEFAULT_WORKER='sha256:64ca2ab5a6e2c647181590be7c8553e77af76e6b073e70c4a4f38b83d6c30b03'


class NavigationTree(Tree):
    BINDINGS=[('right','context','Действия'),('shift+f10','context','Действия')]
    def action_context(self):self.app.navigation_menu(self.cursor_node.data if self.cursor_node else None)
    def on_mouse_down(self,event):
        if event.button!=3:return
        line=event.style.meta.get('line')
        node=self.get_node_at_line(line) if isinstance(line,int) else None
        if node:self.select_node(node)
        self.app.navigation_menu(node.data if node else None);event.stop()


class AppSettings(PageDialog):
    def __init__(self,store):super().__init__(id='app-settings');self.store=store
    def compose(self):
        with Vertical(classes='page-shell'):
            yield Label('Настройки приложения',classes='page-title')
            with VerticalScroll(classes='page-body'):
                yield Button('Модели',id='settings-models')
                yield Label('Изолированный исполнитель')
                yield Static('Общий подготовленный Docker-образ для новых проектов. Существующие запуски сохраняют исходный исполнитель.')
                yield Input(self.store.setting('worker_image',DEFAULT_WORKER),placeholder='sha256:…',id='settings-worker')
                yield Static('',id='settings-error',classes='editor-error')
                yield Label('Оформление')
                yield Select([('Тёмная','textual-dark'),('Светлая','textual-light')],value='textual-light' if self.app.theme=='textual-light' else 'textual-dark',allow_blank=False,id='settings-theme')
            with Horizontal(classes='page-actions'):
                yield Button('Сохранить',id='settings-save',variant='primary');yield Button('Назад',id='settings-close')
    def on_button_pressed(self,event):
        event.stop();key=event.button.id
        if key=='settings-models':self.dismiss('models')
        elif key=='settings-close':self.dismiss(None)
        elif key=='settings-save':
            import re
            value=self.query_one('#settings-worker',Input).value
            if not re.fullmatch(r'sha256:[0-9a-f]{64}',value):
                self.query_one('#settings-error',Static).update('Укажите идентификатор установленного образа: sha256 и 64 символа.');return
            self.store.set_setting('worker_image',value)
            theme=self.query_one('#settings-theme',Select).value
            self.store.set_setting('theme',theme);self.app.theme=theme;self.dismiss(None)


class NavigationMixin:
    def refresh_navigation(self):
        tree=self.query_one('#navigation',Tree)
        # Не терять клавиатурный курсор при обновлении маркеров.
        selected=tree.cursor_node.data if tree.cursor_node else None
        tree.clear();tree.show_root=False
        for project in self.store.projects():
            node=tree.root.add(Text(project['label']),data=(project['id'],None),expand=True)
            if selected==node.data:tree.move_cursor(node)
            for session in self.store.sessions(project['id']):
                marker={'active':'▶','input':'?','done':'●','error':'!'}.get(session['marker'],'')
                item=node.add_leaf(Text(session['label']+' '+marker),data=(project['id'],session['id']))
                if selected==item.data:tree.move_cursor(item)
        tree.root.add_leaf(Text('+ Добавить проект',style='bold cyan'),data=(None,None));tree.root.expand()
    def navigation_menu(self,data):
        if not data or data==(None,None):self.project_form();return
        project,session=data
        if session:
            def action(key):
                if key=='rename':
                    label=next(s['label'] for s in self.store.sessions(project) if s['id']==session)
                    def renamed(values):
                        if values:self.store.rename_session(project,session,values['label']);self.refresh_navigation()
                    self.push_screen(FormDialog('Переименовать сессию',[('label','Название сессии',label,None)]),renamed)
                elif key=='delete':
                    if self.controller.busy and self.controller.active_scope==(project,session):
                        self.set_status('Сначала остановите текущий запуск');return
                    def confirmed(value):
                        if value=='yes':
                            self.store.hide_session(project,session);self.refresh_navigation()
                            if (project,session)==(self.project,self.session):
                                others=self.store.sessions(project)
                                target=others[0]['id'] if others else self.store.add_session(project)
                                self.open_session(project,target)
                    self.push_screen(ChoiceDialog('Удалить сессию из списка? История запусков сохранится.',[('yes','Удалить из списка'),('no','Отмена')]),confirmed)
            self.push_screen(ChoiceDialog('Действия сессии',[('rename','Переименовать'),('delete','Удалить сессию')]),action)
        else:
            def action(key):
                if not key:return
                self.action_save()
                if project!=self.project:
                    sessions=self.store.sessions(project)
                    self.open_session(project,sessions[0]['id'] if sessions else self.store.add_session(project))
                if key=='session':
                    session=self.store.add_session(project,label='Новая сессия');self.open_session(project,session);self.refresh_navigation()
                elif key=='restore':
                    hidden=self.store.sessions(project,hidden=True)
                    def restore(value):
                        if value:self.store.hide_session(project,value,hidden=False);self.refresh_navigation()
                    self.push_screen(ChoiceDialog('Скрытые сессии',[(s['id'],s['label']) for s in hidden]),restore)
                else:self.project_action(key)
            self.push_screen(ChoiceDialog('Действия проекта',[('session','Новая сессия'),('results','Сохранённые результаты'),('catalog','Каталог операций'),('policy','Политики операций'),('settings','Настройки проекта'),('restore','Восстановить удалённую из списка сессию')]),action)
    def on_tree_node_selected(self,event):
        if event.control.id!='navigation' or not event.node.data:return
        project,session=event.node.data
        if project is None:self.call_after_refresh(self.project_form)
        elif session:self.open_session(project,session)
    def show_settings(self):self.push_screen(AppSettings(self.store),lambda value:self.show_models() if value=='models' else None)
    def action_command_palette(self):
        self.push_screen(ChoiceDialog('Команды приложения',[('help','Клавиши и управление'),('screenshot','Сохранить снимок экрана'),('settings','Настройки приложения'),('quit','Выйти')]),self.command_selected)
    def command_selected(self,key):
        if key=='settings':self.show_settings()
        elif key=='quit':self.action_quit()
        elif key=='screenshot':self.notify('Снимок сохранён: '+str(self.save_screenshot()),title='Снимок экрана')
        elif key=='help':self.push_screen(ChoiceDialog('Управление: Esc закрывает это окно',[
            ('','Tab / Shift+Tab — переход между элементами'),('','F2 — список проектов; F3 — показать / скрыть инспектор'),
            ('','F5 — запуск / остановка / продолжение'),('','ПКМ / стрелка вправо — действия проекта или сессии'),
            ('','Space / нажатие на поле HTTP — прикрепление'),('','Ctrl+S — сохранить ввод; Ctrl+Q — выход')]))

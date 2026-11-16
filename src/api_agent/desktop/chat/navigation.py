"""Контекстная навигация и настройки приложения, без постоянно видимой панели CRUD."""
from api_agent.desktop.components.controls import Button, Input, Select, Summary as Static
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Tree, Label
from rich.text import Text
from api_agent.desktop.views.dialogs import ChoiceDialog, FormDialog
from api_agent.desktop.components.widgets import PageDialog
from api_agent.desktop.components.menus import PopupMenu
from api_agent.desktop.application.login import legacy_login_name, save_session_account, session_login_available
from api_agent.desktop.presentation.presentation import error_text

DEFAULT_WORKER='sha256:64ca2ab5a6e2c647181590be7c8553e77af76e6b073e70c4a4f38b83d6c30b03'


class NavigationTree(Tree):
    BINDINGS=[('right','context','Действия'),('shift+f10','context','Действия')]

    def __init__(self, *args, **kwargs):
        self.session_tooltips = {}
        super().__init__(*args, **kwargs)

    def watch_hover_line(self, previous_hover_line, hover_line):
        super().watch_hover_line(previous_hover_line, hover_line)
        self.refresh_session_tooltip()

    def refresh_session_tooltip(self):
        node = self.get_node_at_line(self.hover_line) if self.hover_line >= 0 else None
        tooltip = self.session_tooltips.get(node.data) if node else None
        if self.tooltip != tooltip:
            self.tooltip = tooltip

    def action_context(self):self.app.navigation_menu(self.cursor_node.data if self.cursor_node else None)
    def on_mouse_down(self,event):
        if event.button!=3:return
        line=event.style.meta.get('line')
        node=self.get_node_at_line(line) if isinstance(line,int) else None
        if node:self.select_node(node)
        self.app.navigation_menu(node.data if node else None,point=(self.region.right-2,event.screen_y));event.stop()


class AppSettings(PageDialog):
    def __init__(self,store):super().__init__(id='app-settings');self.store=store
    def compose(self):
        with Vertical(classes='page-shell'):
            yield Label('Настройки приложения',classes='page-title')
            with VerticalScroll(classes='page-body'):
                with Vertical(classes='settings-group'):
                    yield Label('Модели',classes='section-title')
                    yield Static('Конфигураций: '+str(len(self.store.models()))+'\nВыбор для каждой новой цели — возле отправки.',classes='field-help')
                    yield Button('Открыть каталог моделей →',id='settings-models')
                    yield Button('Проверить окружение',id='settings-doctor')
                with Vertical(classes='settings-group'):
                    yield Label('Оформление',classes='section-title')
                    yield Select([('Тёмная','textual-dark'),('Светлая','textual-light')],value='textual-light' if self.app.theme=='textual-light' else 'textual-dark',allow_blank=False,id='settings-theme')
                with Vertical(classes='settings-group'):
                    yield Label('Изолированный исполнитель',classes='section-title')
                    yield Static('Подготовленный Docker-образ новых проектов. Прошлые запуски сохраняют свой исполнитель.',classes='field-help')
                    yield Input(self.store.setting('worker_image',DEFAULT_WORKER),placeholder='sha256:…',id='settings-worker')
                    yield Static('',id='settings-error',classes='editor-error')
            with Horizontal(classes='page-actions'):
                yield Button('Сохранить',id='settings-save',variant='primary');yield Button('Назад',id='settings-close')
    def on_button_pressed(self,event):
        event.stop();key=event.button.id
        if key=='settings-models':self.dismiss('models')
        elif key=='settings-doctor':self.dismiss('doctor')
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
        tree=self.query_one('#navigation',NavigationTree)
        # Не терять клавиатурный курсор при обновлении маркеров.
        selected=tree.cursor_node.data if tree.cursor_node else None
        tree.clear();tree.show_root=False;tree.session_tooltips.clear()
        for project in self.store.projects():
            node=tree.root.add(Text(project['label']),data=(project['id'],None),expand=True)
            if selected==node.data:tree.move_cursor(node)
            sessions=self.store.sessions(project['id'])
            config=project['configuration']
            legacy=legacy_login_name(self.store,project['id']) if (
                (config.get('connection_path') or config.get('mappings_path'))
                and any(not session['account_configured'] for session in sessions)) else None
            for session in sessions:
                marker={'active':'▶','input':'?','done':'●','error':'!'}.get(session['marker'],'')
                login=session['account_login'] if session['account_configured'] else legacy
                key=(project['id'],session['id'])
                label=Text(session['label'])
                tooltip=label.copy()
                if login:
                    label.append(' ').append(login, style='cyan')
                    tooltip.append('\n').append(login, style='cyan')
                if marker:label.append(' '+marker)
                tree.session_tooltips[key]=tooltip
                item=node.add_leaf(label,data=key)
                if selected==item.data:tree.move_cursor(item)
        tree.root.add_leaf(Text('+ Добавить проект',style='bold cyan'),data=(None,None));tree.root.expand()
        tree.refresh_session_tooltip()
    def navigation_menu(self,data,point=None):
        if not data or data==(None,None):self.project_form();return
        project,session=data
        tree=self.query_one('#navigation',Tree)
        point=point or (tree.region.right-2,max(tree.region.y,tree.region.y+tree.cursor_line-int(tree.scroll_y)))
        if session:
            def action(key):
                if key=='rename':
                    label=next(s['label'] for s in self.store.sessions(project) if s['id']==session)
                    def renamed(values):
                        if values:self.store.rename_session(project,session,values['label']);self.refresh_navigation()
                    self.push_screen(FormDialog('Переименовать сессию',[('label','Название сессии',label,None)]),renamed)
                elif key in {'account','remove-account'}:
                    self.session_account(project,session,remove=key=='remove-account')
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
            options=[('rename','✎ Переименовать')]
            if session_login_available(self.store,project):
                options.append(('account','Настроить учётную запись'))
            entry=next(s for s in self.store.sessions(project) if s['id']==session)
            login=entry['account_login'] if entry['account_configured'] else legacy_login_name(self.store,project)
            if login:options.append(('remove-account','Снять выбор пользователя'))
            options.append(('delete','× Удалить сессию'))
            self.push_screen(PopupMenu('Сессия',options,tree,point=point),action)
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
            self.push_screen(PopupMenu('Проект',[('session','+ Новая сессия'),('results','Сохранённые результаты'),('catalog','Каталог операций'),('policy','Политики операций'),('settings','Настройки проекта'),('restore','Восстановить сессию')],tree,point=point),action)
    def session_account(self,project,session,*,remove=False):
        if self.preparing or self.launching or self.controller.busy:
            self.set_status('Дождитесь завершения текущего запуска');return
        def save(account):
            try:
                save_session_account(self.store,project,session,account)
                self.refresh_navigation()
            except (ValueError,OSError) as error:
                self.set_status(error_text(error))
        if remove:
            save(None);return
        login=self.store.accounts.login_name(project,session) or ''
        self.push_screen(FormDialog('Учётная запись сессии',
            [('login','Логин',login,None),('password','Пароль','',None)]),
            lambda values:save((values['login'],values['password'])) if values else None)

    def on_tree_node_selected(self,event):
        if event.control.id!='navigation' or not event.node.data:return
        project,session=event.node.data
        if project is None:self.call_after_refresh(self.project_form)
        elif session:self.open_session(project,session)
    def show_settings(self):
        def selected(value):
            if value=='models':self.show_models()
            elif value=='doctor':
                from api_agent.desktop.views.environment_doctor import EnvironmentDialog
                self.push_screen(EnvironmentDialog(self.store,self.controller,project=self.project))
        self.push_screen(AppSettings(self.store),selected)
    def action_command_palette(self):
        self.push_screen(PopupMenu('Команды',[('help','Клавиши и управление'),('screenshot','Сохранить снимок экрана'),('settings','Настройки приложения'),('quit','Выйти')],self.query_one('#command-menu')),self.command_selected)
    def command_selected(self,key):
        if key=='settings':self.show_settings()
        elif key=='quit':self.action_quit()
        elif key=='screenshot':self.notify('Снимок сохранён: '+str(self.save_screenshot()),title='Снимок экрана')
        elif key=='help':
            from api_agent.desktop.views.help_view import HelpDialog
            self.push_screen(HelpDialog())

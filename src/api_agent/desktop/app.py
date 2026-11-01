"""Textual Projects/Sessions/chat. Сеть и выполнение принадлежат controller/Core."""
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import urlsplit
import queue
import sqlite3
import uuid
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import (Button, DataTable, Footer, Header, Label, ListItem, ListView,
    Static, TabbedContent, TabPane, TextArea, Tree)

from api_agent.desktop.controller import DesktopController
from api_agent.desktop.dialogs import (ChoiceDialog, FormDialog, HumanDialog, ModelSettings,
    MODEL_FIELDS, PROJECT_FIELDS, PreviewDialog)
from api_agent.desktop.inspector import JsonInspector
from api_agent.desktop.ui_errors import read_feedback
from api_agent.desktop.submission import SubmissionMixin
from api_agent.desktop.composer import ComposerMixin
from api_agent.desktop.conversation import ConversationMixin, InspectorMixin
from api_agent.desktop.navigation import NavigationMixin, NavigationTree, DEFAULT_WORKER
from api_agent.desktop.widgets import SplitHandle
from api_agent.desktop.presentation import error_text


class AgentApp(NavigationMixin,ConversationMixin,InspectorMixin,SubmissionMixin,ComposerMixin,App):
    # Textual8.2.8 native selection может сохранить уже удалённый Static без parent.
    # Копирование выполняют явные кнопки сообщения, не этот глобальный mouse handler.
    ALLOW_SELECT=False
    TITLE='API Testing Agent'
    ENABLE_COMMAND_PALETTE=False
    BINDINGS=[('f2','navigation','Проекты'),('f3','inspector','Инспектор'),('f5','send','Отправить'),
              ('ctrl+p','command_palette','Команды'),('ctrl+s','save','Сохранить ввод'),('ctrl+q','quit','Выход')]
    CSS="""
    Screen {background: $background; color: $text;}
    Footer {background: $panel;}
    #body {height: 1fr;}
    #navigation {width: 27; background: $panel;}
    #workspace {width: 1fr;}
    #app-bar {height: 3; background: $panel;}
    #app-title {width: 1fr; padding: 1; text-style: bold;}
    #app-bar Button {min-width: 5;}
    #controls {height: 3;}
    Button {min-width: 5; width: auto; border: none; height: 3; margin-right: 1;}
    * {scrollbar-size: 1 1;}
    #session-title {height: 1; color: $accent; padding-left: 1;}
    #conversation {height: 1fr; min-height: 3; background: $background;}
    #conversation ListItem {height: auto; padding: 0 1; margin: 0 0 1 0; background: $surface;}
    #conversation .user-message {margin-left: 8; border-right: solid $accent;}
    #conversation .agent-message {margin-right: 8; border-left: solid $primary;}
    #conversation Static {height: auto;}
    #conversation .user-message > Vertical, #conversation .agent-message > Vertical {height: auto;}
    .message-role {height: 1; text-style: bold; color: $accent;}
    .message-actions {height: 3;}
    .message-actions Button {height: 3; color: $text-muted;}
    .program-result {color: $text-muted; border-top: solid $panel; padding-top: 1;}
    #goal {height: 5; border: solid $accent;}
    #status {height: auto; max-height: 3; color: $text-muted;}
    #status.error {background: $error; color: $text; text-style: bold; padding: 0 1;}
    #draft-summary {display: none;}
    #messages-more {height: 1; display: none;}
    #model-picker {width: 1fr; min-width: 10;}
    #inspector {height: 45%; min-height: 8;}
    #inspector-head {height: 3;}
    #inspector-label {width: 1fr; padding-top: 1;}
    #inspector TabbedContent, #inspector TabPane {height: 1fr; padding: 0;}
    #exchanges {width: 35%; height: 1fr;}
    #json-tree {width: 1fr; height: 1fr;}
    #configuration {display: none;}
    #config-summary {height: auto; padding: 1;}
    .narrow #navigation, .narrow #nav-split {display: none;}
    .narrow.show-navigation #navigation {display: block; width: 24;}
    .narrow.inspector-full #conversation, .narrow.inspector-full #goal, .narrow.inspector-full #controls {display: none;}
    .narrow.inspector-full #inspector {height: 1fr;}
    """

    def __init__(self,store,*,controller=None):
        super().__init__();self.store=store;self.events=queue.SimpleQueue()
        self.controller=controller or DesktopController(store,notify=lambda k,v:self.events.put((k,v)))
        if controller is not None:self.controller.notify=lambda k,v:self.events.put((k,v))
        self.project=self.session=None;self.draft={};self.loading_draft=False
        self.inspected_run=None;self.inspected_scope=None;self.selected_exchange=None
        self.follow_live=False;self.preparing=False;self.launching=False;self.human_dialog=None
        self.message_offset=0;self.exchange_offset=0;self.message_data={};self.conditions=[]
        self.selection=None;self.closing=False;self.prepared=None
        self.draft_revision=0;self.pending_submission=None
        self.pending_goal=None;self.rendered_message_ids=[];self.rendered_scope=None;self.inspected_role=None
        self.theme=store.setting('theme','textual-dark')

    def compose(self)->ComposeResult:
        with Horizontal(id='app-bar'):
            yield Static(self.TITLE,id='app-title')
            yield Button('Команды',id='command-menu',tooltip='Команды приложения · Ctrl+P')
            yield Button('⚙',id='app-settings',tooltip='Настройки приложения')
        with Horizontal(id='body'):
            yield NavigationTree('Проекты',id='navigation')
            yield SplitHandle('#navigation',vertical=True,id='nav-split')
            with Vertical(id='workspace'):
                yield Label('Добавьте или выберите проект',id='session-title')
                yield Button('Показать более ранние сообщения',id='messages-more')
                yield ListView(id='conversation')
                with Vertical(id='inspector'):
                    yield SplitHandle('#inspector',id='inspector-split')
                    with Horizontal(id='inspector-head'):
                        yield Label('Инспектор выбранного запуска',id='inspector-label')
                        yield Button('×',id='inspector-close',tooltip='Закрыть инспектор · F3')
                    with TabbedContent():
                        with TabPane('HTTP',id='trace-tab'):
                            with Horizontal():
                                yield DataTable(id='exchanges',cursor_type='row')
                                yield SplitHandle('#exchanges',vertical=True,id='http-split')
                                yield JsonInspector(id='json-tree')
                        with TabPane('Условия',id='config-tab'):
                            from textual.containers import VerticalScroll
                            with VerticalScroll():
                                yield Static('',id='config-summary',markup=False)
                                yield Button('⧉ Условия в JSON',id='copy-conditions')
                                yield Button('Открыть сохранённые карточки',id='config-constructor')
                            yield TextArea(read_only=True,id='configuration')
                        with TabPane('Проверки',id='report-tab'):
                            yield DataTable(id='conditions',cursor_type='row')
                yield Static('',id='status',markup=False)
                yield TextArea(id='goal',placeholder='Опишите цель для выбранного API…')
                yield Static('',id='draft-summary')
                with Horizontal(id='controls'):
                    yield Button('Условия · 0',id='conditions-edit')
                    yield Button('Модель · выбрать',id='model-picker')
                    yield Button('Подробнее',id='draft-details')
                    yield Button('↑',id='send',variant='primary',tooltip='Отправить цель · F5')
        yield Footer()

    def on_mount(self):
        self.query_one('#inspector').display=False
        self.query_one('#exchanges',DataTable).add_columns('№','Метод','Путь','HTTP','Время / эффект')
        self.query_one('#conditions',DataTable).add_columns('Условие','Ожидалось','Получено','Итог / причина')
        self.restore_sessions();self.refresh_navigation();self.refresh_composer();self.set_interval(0.15,self.poll_events)
        last=self.store.setting('selection')
        if last:
            try:self.open_session(*last)
            except ValueError:pass
        if not self.store.setting('onboarded',False):
            self.push_screen(ChoiceDialog('Первый запуск: настройте модель и при желании добавьте проект',
                [('models','Настроить и загрузить первую модель'),('project','Добавить первый проект'),('skip','Пропустить setup')]),self.setup_action)

    def setup_action(self,key):
        self.store.set_setting('onboarded',True)
        if key=='models':self.show_models()
        elif key=='project':self.project_form()

    def on_resize(self,event):
        self.set_class(event.size.width<100,'narrow')
        if self.inspected_run and self.query_one('#inspector').display:
            self.set_class(event.size.width<100,'inspector-full')
    def action_navigation(self):self.toggle_class('show-navigation')
    def action_inspector(self):
        if not self.inspected_run:return
        inspector=self.query_one('#inspector');inspector.display=not inspector.display
        self.set_class(inspector.display and self.size.width<100,'inspector-full')


    def restore_sessions(self):
        for project in self.store.projects():
            key=project['id']
            if not self.controller.busy:
                for session in self.store.sessions(key):
                    if session['marker'] in {'active','input'}:self.store.mark(key,session['id'],'error')
            if not self.controller.history_path(key).exists():continue
            try:
                with self.controller.history(key) as history:
                    offset=0
                    while True:
                        page=history.list_sessions(key,limit=20,offset=offset)
                        for item in page['items']:self.store.add_session(key,item['session_id'],item['session_id'])
                        if not page['more']:break
                        offset+=20
            except (ValueError,OSError,sqlite3.Error) as error:self.set_status('История проекта: '+str(error))


    def action_save(self):
        if self.project and not self.loading_draft:
            self.draft['goal']=self.query_one('#goal',TextArea).text
            if self.draft!=self.store.draft(self.project,self.session):self.draft_revision+=1
            self.store.save_draft(self.project,self.session,self.draft)
            self.refresh_composer()

    def on_text_area_changed(self,event):
        if event.text_area.id=='goal':self.action_save()

    @read_feedback
    def open_session(self,project,session):
        self.action_save()
        self.store.project(project)
        if session not in {s['id'] for s in self.store.sessions(project)}:raise ValueError('desktop_unknown_session')
        if (project,session)!=(self.project,self.session):self.draft_revision+=1
        self.project,self.session=project,session;self.follow_live=False
        self.store.set_setting('selection',[project,session]);self.draft=self.store.draft(project,session)
        self.loading_draft=True;self.query_one('#goal',TextArea).text=self.draft['goal'];self.loading_draft=False
        self.query_one('#session-title',Label).update(self.store.project(project)['label']+' / '+session)
        self.query_one('#inspector').display=False;self.remove_class('inspector-full');self.inspected_run=None
        self.message_offset=0;self.selection=None;self.query_one('#json-tree',JsonInspector).reset_exchange();self.refresh_messages();self.update_controls()

    def update_controls(self):
        count=sum(bool(c.get('enabled',True)) for c in self.draft.get('cards',[]))
        self.query_one('#conditions-edit',Button).label='Условия · '+str(count)+(' · 📎 '+str(len(self.draft.get('attachments',[]))) if self.draft.get('attachments') else '')
        record=self.store.model(self.draft.get('model_id'),required=False)
        name=record['profile']['name'] if record else 'выбрать'
        button=self.query_one('#model-picker',Button)
        button.label='Модель · '+(name if len(name)<=22 else name[:19]+'…');button.tooltip=name
        self.refresh_composer()








    def show_models(self):self.push_screen(ModelSettings(self.store),self.model_settings_action)
    def model_settings_action(self,result):
        if not result:return
        action,key,profile=result
        if action=='check':self.check_model(key);return
        self.push_screen(FormDialog('Конфигурация модели',MODEL_FIELDS,values=profile),lambda v:self.save_model(key,v))

    def save_model(self,key,values):
        if not values:return
        try:
            profile={k:v for k,v in values.items() if v!=''}
            for field in ['port','context_tokens','output_tokens','gpu_layers']:profile[field]=int(profile[field])
            self.store.save_model(key,profile);self.check_model(key)
        except (KeyError,ValueError):self.notify('Проверьте название и числовые поля конфигурации',severity='error')

    def check_model(self,key):
        if self.preparing or self.launching or self.controller.busy or self.closing:
            self.set_status('Дождитесь завершения текущей подготовки / Run');return
        self.preparing=True;self.controller.cancel_event.clear();self.load_model(key)

    @work(thread=True)
    def load_model(self,key):
        try:
            self.call_from_thread(self.set_status,'Загрузка / проверка модели…')
            self.controller.manager.prepare(self.store.model(key),cancel=self.controller.cancel_event)
            self.call_from_thread(self.set_status,'Модель загружена и проверена')
        except Exception as error:self.call_from_thread(self.set_status,'Модель недоступна: '+error_text(error))
        finally:self.preparing=False

    def choose_model(self):
        records=self.store.models()
        if not records:self.show_models();return
        self.push_screen(ChoiceDialog('Модель новой цели',[(r['id'],r['profile']['name']) for r in records]),self.model_selected)
    def model_selected(self,key):
        if key:self.draft['model_id']=key;self.action_save();self.update_controls()

    def project_form(self):self.push_screen(FormDialog('Добавить проект',PROJECT_FIELDS),self.create_project)
    def create_project(self,values):
        if not values:return
        try:
            from api_agent.openapi import load_source
            from api_agent.policy.config import ProjectConfig
            if not Path(values['spec_path']).is_file():raise ValueError('Укажите локальный OpenAPI JSON')
            catalog=load_source(values['spec_path'],target=values['target'])
            if values['policy_path']:policy=ProjectConfig.load(values['policy_path'],catalog)
            else:
                project=uuid.uuid4().hex
                policy=ProjectConfig.create(project,values['target'],catalog,mode='TEST',resettable=False,
                    operations=__import__('api_agent.desktop.project_views',fromlist=['preset_operations']).preset_operations(catalog,'DEFAULT'))
                values['policy_path']=str(self.store.path.parent/(project+'-policy.json'));policy.save(values['policy_path'])
            project=policy.snapshot['project_id'];label=values.pop('label') or project
            previous=self.store.project(project,required=False)
            values['image']=(previous['configuration'].get('image') if previous else None) or self.store.setting('worker_image',DEFAULT_WORKER)
            self.store.add_project(project,label,{k:v for k,v in values.items() if v})
            self.restore_sessions();sessions=self.store.sessions(project)
            session=sessions[0]['id'] if sessions else self.store.add_session(project,label='Новая сессия')
            self.refresh_navigation();self.open_session(project,session)
        except Exception as error:self.notify(error_text(error),severity='error')

    def set_status(self,value):
        status=self.query_one('#status',Static)
        translated=error_text(value)
        is_error=translated!=str(value) and str(value) not in {'finished','cancelled'} or any(w in str(value).lower() for w in ['ошиб','недоступ','не удалось','invalid','error','denied','unavailable'])
        status.set_class(is_error,'error');status.update(('Ошибка: ' if is_error else '')+translated)

    def copy_to_clipboard(self,text):
        from api_agent.desktop.clipboard import copy_windows
        if copy_windows(text):
            self._clipboard=text
        else:super().copy_to_clipboard(text)

    def on_button_pressed(self,event):
        event.stop();key=event.button.id
        if self.copy_message(key):return
        if key=='app-settings':self.show_settings()
        elif key=='command-menu':self.action_command_palette()
        elif key=='inspector-close':self.action_inspector()
        elif key=='send':self.action_send()
        elif key=='cancel-run':self.controller.cancel();self.set_status('Запрошена отмена; ожидаем сохранения результата')
        elif key=='models-settings':self.show_models()
        elif key=='model-picker':self.choose_model()
        elif key=='session-add' and self.project:
            session=self.store.add_session(self.project);self.refresh_navigation();self.open_session(self.project,session)
        elif key=='messages-more':self.message_offset+=20;self.refresh_messages()
        elif key=='project-menu':self.project_menu()
        elif key=='conditions-edit':self.edit_conditions()
        elif key=='draft-details':self.show_draft_details()
        else:self.inspector_action(key)

    def project_menu(self):
        self.push_screen(ChoiceDialog('Действия проекта', [('new','Добавить проект'),('results','Сохранённые результаты'),
            ('catalog','Каталог операций'),('policy','Политики операций'),('settings','Настройки проекта')]),self.project_action)

    @read_feedback
    def project_action(self,key):
        if key=='new':self.project_form()
        elif key and self.project:
            from api_agent.desktop.project_views import open_project_view
            open_project_view(self,key)

    def edit_conditions(self):
        from api_agent.desktop.constructor import open_constructor
        open_constructor(self)

    def inspector_action(self,key):
        if not self.inspected_run:return
        try:
            if key=='config-constructor':
                from api_agent.desktop.constructor import open_constructor
                run=self.controller.read_run(*self.inspected_scope,self.inspected_run)
                manifest=run['admission_manifest']['goal_contract']['manifest']
                open_constructor(self,manifest=manifest);return
            from api_agent.desktop.project_views import inspector_action
            inspector_action(self,key)
        except Exception as error:self.set_status(str(error))

    def poll_events(self):
        if self.query_one_optional('#navigation') is None:return
        while not self.events.empty():
            kind,value=self.events.get();self.refresh_navigation()
            if kind=='human':
                request=value
                self.human_dialog=HumanDialog(request)
                self.push_screen(self.human_dialog,lambda reply,r=request:self.controller.channel.reply(r['id'],reply))
            elif kind=='human_closed' and self.human_dialog and self.human_dialog.request['id']==value['request_id']:
                if self.human_dialog.is_mounted and self.screen is self.human_dialog:self.human_dialog.dismiss(None)
                self.human_dialog=None
                if self.controller.busy and self.controller.active_scope:self.store.mark(*self.controller.active_scope,'active')
            elif kind=='trace':
                scope=(value['project'],value['session'])
                if scope==(self.project,self.session):
                    self.pending_goal=None;self.refresh_messages()
                if self.inspected_run==value['run_id'] and self.inspected_scope==scope:
                    self.refresh_exchanges(preserve=True)
                elif self.follow_live and scope==(self.project,self.session):
                    self.inspect_message(value['run_id'],'user');self.query_one(TabbedContent).active='trace-tab';self.follow_live=True
            elif kind=='finished':
                if (value['project'],value['session'])==(self.project,self.session):
                    self.pending_goal=None;self.refresh_messages()
                if self.inspected_run==value['run_id'] and self.inspected_scope==(value['project'],value['session']):
                    self.finish_inspected()
                self.set_status('Завершено: '+error_text(value.get('stop_reason')))
            elif kind=='error':self.set_status('Ошибка выполнения: '+error_text(value.get('stop_reason')))
            elif kind=='preparation':self.set_status(value['message'])
            elif kind=='event' and self.controller.active_scope==(self.project,self.session):
                label={'model_preflight_started':'Проверяем контекст и ресурсы',
                    'model_requested':'Модель выбирает действие…','model_received':'Проверяем ответ модели',
                    'model_observed':'Результат действия сохранён'}.get(value.get('kind'))
                if label:self.set_status(label)
        self.update_run_button()
        if self.closing and not self.controller.busy and not self.preparing and not self.launching:
            try:self.controller.close();self.exit()
            except ValueError:pass


    def action_quit(self):
        self.action_save();self.controller.cancel();self.closing=True
        if self.prepared:self.prepared.close();self.prepared=None
        self.set_status('Сохраняем результат и останавливаем собственный runtime…')

    def on_unmount(self):
        self.closing=True;self.pending_submission=None;self.controller.cancel()
        if self.prepared:self.prepared.close();self.prepared=None

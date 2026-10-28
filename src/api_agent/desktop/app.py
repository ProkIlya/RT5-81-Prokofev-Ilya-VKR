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


class AgentApp(SubmissionMixin,ComposerMixin,App):
    TITLE='API Testing Agent'
    BINDINGS=[('f2','navigation','Проекты'),('f3','inspector','Инспектор'),('f5','send','Отправить'),
              ('ctrl+s','save','Сохранить ввод'),('ctrl+q','quit','Выход')]
    CSS='''
    Screen {background: #18212b; color: #e0e7ed;}
    Header, Footer {background: #263544;}
    #body {height: 1fr;}
    #navigation {width: 27; border-right: solid #526b7b; background: #202d39;}
    #workspace {width: 1fr;}
    #toolbar, #controls, #inspect-controls {height: 3;}
    Button {min-width: 9; width: auto; margin-right: 1;}
    #session-title {height: 1; color: #9fc9db; padding-left: 1;}
    #conversation {height: 1fr; min-height: 3;}
    #conversation ListItem {height: auto; padding: 1; border-bottom: solid #344855;}
    #conversation Static {height: auto;}
    #goal {height: 5; border: round #668da5;}
    #status {height: 1; color: #9fc9db;}
    #draft-summary {height: 2; color: #9fc9db;}
    #model-picker {width: 1fr; min-width: 12;}
    #inspector {height: 45%; min-height: 8; border-top: solid #668da5;}
    #inspector TabbedContent, #inspector TabPane {height: 1fr; padding: 0;}
    #exchanges {width: 40%; height: 1fr;}
    #json-tree {width: 1fr; height: 1fr;}
    #configuration {height: 1fr;}
    .narrow #navigation {display: none;}
    .narrow.show-navigation #navigation {display: block; width: 24;}
    .narrow.inspector-full #conversation, .narrow.inspector-full #goal, .narrow.inspector-full #controls, .narrow.inspector-full #draft-summary {display: none;}
    .narrow.inspector-full #inspector {height: 1fr;}
    '''

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

    def compose(self)->ComposeResult:
        yield Header()
        with Horizontal(id='body'):
            yield Tree('Проекты / сессии',id='navigation')
            with Vertical(id='workspace'):
                with Horizontal(id='toolbar'):
                    yield Button('Проект',id='project-menu');yield Button('Сессия +',id='session-add')
                    yield Button('Ещё сообщения',id='messages-more');yield Button('Модели',id='models-settings')
                yield Label('Выберите или добавьте проект',id='session-title')
                yield ListView(id='conversation')
                with Vertical(id='inspector'):
                    with Horizontal(id='inspect-controls'):
                        yield Button('Закрепить',id='pin');yield Button('Копировать условия',id='copy-conditions')
                        yield Button('Продолжить',id='continue');yield Button('В новую цель',id='reuse-fields')
                    with TabbedContent():
                        with TabPane('HTTP',id='trace-tab'):
                            with Horizontal():
                                yield DataTable(id='exchanges',cursor_type='row')
                                yield JsonInspector(id='json-tree')
                        with TabPane('Условия',id='config-tab'):
                            yield Button('Открыть конструктор · только чтение',id='config-constructor')
                            yield TextArea(read_only=True,id='configuration')
                        with TabPane('Проверки',id='report-tab'):
                            yield DataTable(id='conditions',cursor_type='row')
                yield Static('',id='status',markup=False)
                yield TextArea(id='goal',placeholder='Опишите цель для разрешённого API…')
                yield Static('',id='draft-summary',markup=False)
                with Horizontal(id='controls'):
                    yield Button('Условия · 0',id='conditions-edit');yield Button('Модель · выбрать',id='model-picker')
                    yield Button('Сводка',id='draft-details')
                    yield Button('Отправить',id='send',variant='primary');yield Button('Отмена Run',id='cancel-run')
        yield Footer()

    def on_mount(self):
        self.query_one('#inspector').display=False
        self.query_one('#exchanges',DataTable).add_columns('№','Метод / путь','HTTP / исход','Время / эффект')
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

    def refresh_navigation(self):
        tree=self.query_one('#navigation',Tree);tree.clear()
        for project in self.store.projects():
            node=tree.root.add(Text(project['label']),expand=True)
            for session in self.store.sessions(project['id']):
                marker={'active':'▶','input':'?','done':'●','error':'!'}.get(session['marker'],'')
                node.add_leaf(Text(session['label']+' '+marker),data=(project['id'],session['id']))
        tree.root.expand()

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

    def on_tree_node_selected(self,event):
        if event.control.id=='navigation' and event.node.data:
            self.open_session(*event.node.data)

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
        self.message_offset=0;self.refresh_messages();self.update_controls()

    def update_controls(self):
        count=sum(bool(c.get('enabled',True)) for c in self.draft.get('cards',[]))
        self.query_one('#conditions-edit',Button).label='Условия · '+str(count)
        record=self.store.model(self.draft.get('model_id'),required=False)
        name=record['profile']['name'] if record else 'выбрать'
        button=self.query_one('#model-picker',Button)
        button.label='Модель · '+(name if len(name)<=22 else name[:19]+'…');button.tooltip=name
        self.refresh_composer()

    @read_feedback
    def refresh_messages(self):
        view=self.query_one('#conversation',ListView);view.clear();self.message_data={}
        if not self.project:return
        page=self.controller.messages(self.project,self.session,offset=self.message_offset)
        for index,message in enumerate(page['items']):
            label=('Вы: ' if message['role']=='user' else 'Агент: ')+message['content']
            if message['role']=='assistant':
                run=self.controller.read_run(self.project,self.session,message['run_id']);report=run.get('report') or {}
                label+='\n'+report.get('summary',run.get('stop_reason') or 'Прервано')
            item=ListItem(Static(label,markup=False),id='message-'+uuid.uuid4().hex)
            self.message_data[item.id]=message;view.append(item)
        self.query_one('#messages-more',Button).disabled=not page.get('more',False)

    def on_list_view_selected(self,event):
        message=self.message_data.get(event.item.id)
        if message:self.inspect_message(message['run_id'],message['role'])

    @read_feedback
    def inspect_message(self,run_id,role):
        scope=(self.project,self.session)
        run=self.controller.read_run(*scope,run_id)
        self.follow_live=False;self.inspected_run=run_id;self.inspected_scope=(self.project,self.session)
        self.exchange_offset=0
        manifest=(run.get('admission_manifest',{}).get('goal_contract') or {}).get('manifest',{})
        self.query_one('#configuration',TextArea).text=json.dumps(manifest,ensure_ascii=False,indent=2)
        self.query_one('#inspector').display=True
        self.set_class(self.size.width<100,'inspector-full')
        self.query_one(TabbedContent).active='config-tab' if role=='user' else 'trace-tab'
        self.refresh_exchanges();self.refresh_report(run)

    def refresh_report(self,run):
        table=self.query_one('#conditions',DataTable);table.clear()
        self.conditions=(run.get('report') or {}).get('conditions',[])
        for index,result in enumerate(self.conditions):
            table.add_row(result['condition_id'] if 'condition_id' in result else result.get('id',''),
                json.dumps(result.get('expected'),ensure_ascii=False),json.dumps(result.get('actual'),ensure_ascii=False),
                result['verdict']+' · '+result.get('reason',''),key=str(index))

    @read_feedback
    def refresh_exchanges(self,*,preserve=False):
        if not self.inspected_run:return
        page=self.controller.exchanges(*self.inspected_scope,self.inspected_run,offset=self.exchange_offset)
        table=self.query_one('#exchanges',DataTable);cursor=table.cursor_row
        table.clear()
        if not preserve:self.selected_exchange=None
        for row in page['items']:
            request=row['request'];url=urlsplit(str(request.get('url') or ''))
            route=url.path+('?' + url.query if url.query else '')
            table.add_row(str(row['sequence']),str(request.get('method'))+' '+route,
                str(row.get('status') or row['outcome']),
                (str(row['wall_seconds'])+' с' if row.get('wall_seconds') is not None else 'н/д')+
                (' · НЕОПРЕДЕЛЁН' if row.get('effect_status')=='uncertain' else ''),key=row['exchange_id'])
        if page.get('more'):table.add_row('…','Показать следующие20','','',key='__more__')
        if preserve and table.row_count:table.move_cursor(row=min(cursor,table.row_count-1),animate=False)

    def on_data_table_row_selected(self,event):
        key=str(event.row_key.value)
        if event.control.id=='conditions':
            ids=self.conditions[int(key)].get('evidence_ids',[])
            if ids:self.show_exchange(ids[-1]);self.query_one(TabbedContent).active='trace-tab'
        elif event.control.id=='exchanges':
            if key=='__more__':self.exchange_offset+=20;self.refresh_exchanges()
            else:self.show_exchange(key)

    @read_feedback
    def show_exchange(self,key):
        run=self.controller.read_run(*self.inspected_scope,self.inspected_run)
        exchange=next(e for e in run['exchanges'] if e['id']==key)
        self.selected_exchange=key;self.selection=None
        self.query_one('#json-tree',JsonInspector).show_exchange(
            lambda section,path,offset:self.controller.section(*self.inspected_scope,self.inspected_run,key,
                section=section,pointer=path,offset=offset),field_picker=self.pick_fields,
            metadata={k:exchange.get(k) for k in ['request_parameters','request_principal','outcome','truncated',
                'wall_seconds','effect_status','transport_error']})

    def pick_fields(self,paths):
        if not paths:self.selection=None;return
        parents={p.rsplit('/',1)[0] for p in paths}
        if len(parents)!=1:raise ValueError('Выберите поля одного объекта')
        parent=parents.pop()
        with self.controller.history(self.project) as history:
            self.selection=history.select_attachment(*self.inspected_scope,self.inspected_run,self.selected_exchange,
                object_path=parent,fields=[p[len(parent):] for p in paths])

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
        except Exception as error:self.call_from_thread(self.set_status,'Модель недоступна: '+str(error))
        finally:self.preparing=False

    def choose_model(self):
        records=self.store.models()
        if not records:self.show_models();return
        self.push_screen(ChoiceDialog('Модель новой цели',[(r['id'],r['profile']['name']) for r in records]),self.model_selected)
    def model_selected(self,key):
        if key:self.draft['model_id']=key;self.action_save();self.update_controls()

    def project_form(self):self.push_screen(FormDialog('Добавить Project',PROJECT_FIELDS),self.create_project)
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
                policy=ProjectConfig.create(project,values['target'],catalog,mode='READ_ONLY',resettable=False,
                    operations={k:'A' for k,v in catalog.operations.items() if v['method'] in {'GET','HEAD'}})
                values['policy_path']=str(self.store.path.parent/(project+'-policy.json'));policy.save(values['policy_path'])
            project=policy.snapshot['project_id'];label=values.pop('label') or project
            self.store.add_project(project,label,{k:v for k,v in values.items() if v})
            self.restore_sessions();session=self.store.add_session(project);self.refresh_navigation();self.open_session(project,session)
        except Exception as error:self.notify(str(error),severity='error')

    def set_status(self,value):self.query_one('#status',Static).update(value)

    def on_button_pressed(self,event):
        key=event.button.id
        if key=='send':self.action_send()
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
        self.push_screen(ChoiceDialog('Project', [('new','Добавить проект'),('results','Сохранённые результаты'),
            ('catalog','Каталог операций'),('policy','Политики операций'),('settings','Настройки проекта')]),self.project_action)

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
                if self.inspected_run==value['run_id'] and self.inspected_scope==scope:
                    self.refresh_exchanges(preserve=True)
                elif value['exchanges'] and self.follow_live and scope==(self.project,self.session):
                    self.inspect_message(value['run_id'],'assistant');self.follow_live=True
            elif kind=='finished':
                if (value['project'],value['session'])==(self.project,self.session):self.refresh_messages()
                if self.inspected_run==value['run_id'] and self.inspected_scope==(value['project'],value['session']):
                    self.finish_inspected()
                self.set_status('Завершено: '+str(value.get('stop_reason')))
            elif kind=='error':self.set_status('Остановлено: '+str(value.get('stop_reason')))
            elif kind=='preparation':self.set_status(value['message'])
            elif kind=='event' and self.controller.active_scope==(self.project,self.session):
                label={'model_preflight_started':'Проверяем контекст и ресурсы',
                    'model_requested':'Модель выбирает действие…','model_received':'Проверяем ответ модели',
                    'model_observed':'Результат действия сохранён'}.get(value.get('kind'))
                if label:self.set_status(label)
        if self.closing and not self.controller.busy and not self.preparing and not self.launching:
            try:self.controller.close();self.exit()
            except ValueError:pass

    @read_feedback
    def finish_inspected(self):
        run=self.controller.read_run(*self.inspected_scope,self.inspected_run)
        self.refresh_exchanges(preserve=True);self.refresh_report(run)

    def action_quit(self):
        self.action_save();self.controller.cancel();self.closing=True
        if self.prepared:self.prepared.close();self.prepared=None
        self.set_status('Сохраняем результат и останавливаем собственный runtime…')

    def on_unmount(self):
        self.closing=True;self.pending_submission=None;self.controller.cancel()
        if self.prepared:self.prepared.close();self.prepared=None

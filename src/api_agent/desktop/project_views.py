"""Каталог/политики используют OpenAPI и ProjectConfig; history — сохранённый report."""
from copy import deepcopy
import json
import os
from pathlib import Path
import uuid
from rich.text import Text
from textual.containers import Horizontal, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Input, Label, Select, Static

from api_agent.desktop.dialogs import ChoiceDialog, PreviewDialog, FormDialog, PROJECT_FIELDS
from api_agent.openapi import load_source
from api_agent.policy.config import ProjectConfig


def operation_rows(catalog,config,*,search='',method='ALL'):
    rows=[]
    for key,operation in catalog.operations.items():
        if method!='ALL' and operation['method']!=method:continue
        if search.casefold() not in (key+' '+operation['path']).casefold():continue
        rows.append({'id':key,'method':operation['method'],'path':operation['path'],
            'risk':config.snapshot['operations'].get(key,{}).get('risk','—')})
    return rows


def save_policy(path,catalog,old,mode,operations):
    current=ProjectConfig.load(path,catalog)
    if current.snapshot!=old.snapshot:raise ValueError('project_policy_changed')
    snap=old.snapshot
    updated=ProjectConfig.create(snap['project_id'],snap['target'],catalog,mode=mode,
        resettable=snap['resettable'],operations=operations,version=snap['version']+1)
    path=Path(path);temporary=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    try:updated.save(temporary);os.replace(temporary,path)
    finally:
        if temporary.exists():temporary.unlink()
    return updated


class OperationsDialog(ModalScreen):
    DEFAULT_CSS='''
    OperationsDialog {align: center middle; background: $background 70%;}
    OperationsDialog > VerticalScroll {width: 100; max-width: 97%; height: 90%; padding: 1; border: round $accent; background: $surface;}
    OperationsDialog DataTable {height: 8;}
    OperationsDialog Horizontal {height: auto;}
    OperationsDialog Input, OperationsDialog Select {width: 1fr;}
    OperationsDialog #op-detail {height: auto;}
    '''
    BINDINGS=[('escape','back','Назад')]
    def __init__(self,catalog,config,*,path=None):
        super().__init__();self.catalog=catalog;self.config=config;self.path=path
        self.operations={k:v['risk'] for k,v in config.snapshot['operations'].items()};self.selected=None
    def compose(self):
        with VerticalScroll():
            yield Label('Политики операций' if self.path else 'Каталог операций')
            with Horizontal():
                yield Input(placeholder='Поиск по имени / пути',id='op-search')
                yield Select([(m,m) for m in ['ALL','GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS']],value='ALL',id='op-method',allow_blank=False)
            yield DataTable(id='operations',cursor_type='row')
            yield Static('Выберите операцию для schema/parameters/security.',id='op-detail',markup=False)
            if self.path:
                yield Select([(m,m) for m in ['READ_ONLY','SAFE','TEST']],value=self.config.snapshot['mode'],id='policy-mode',allow_blank=False)
                yield Label('Допуск выбранной операции: A — чтение; остальные классы — согласно политике проекта')
                yield Select([(m,m) for m in ['Запретить','A','B','C','D','E']],value='Запретить',id='op-risk',allow_blank=False)
                yield Button('Применить к операции',id='op-grant')
                yield Button('Сохранить новую версию политики',id='policy-save',variant='primary')
            yield Button('Назад',id='op-back')
    def on_mount(self):
        self.query_one(DataTable).add_columns('Операция','Метод','Путь','Класс');self.refresh_rows()
    def refresh_rows(self):
        table=self.query_one(DataTable);table.clear()
        for row in operation_rows(self.catalog,self.config,search=self.query_one('#op-search',Input).value,
                                  method=self.query_one('#op-method',Select).value):
            table.add_row(row['id'],row['method'],row['path'],self.operations.get(row['id'],'—'),key=row['id'])
    def on_input_changed(self,event):self.refresh_rows()
    def on_select_changed(self,event):
        if event.select.id=='op-method':self.refresh_rows()
    def on_data_table_row_selected(self,event):
        self.selected=str(event.row_key.value)
        self.query_one('#op-detail',Static).update(json.dumps(self.catalog.operations[self.selected],ensure_ascii=False,indent=2))
        if self.path:self.query_one('#op-risk',Select).value=self.operations.get(self.selected,'Запретить')
    def on_button_pressed(self,event):
        if event.button.id=='op-back':self.dismiss(None)
        elif event.button.id=='op-grant' and self.selected:
            risk=self.query_one('#op-risk',Select).value
            if risk=='Запретить':self.operations.pop(self.selected,None)
            else:self.operations[self.selected]=risk
            self.refresh_rows()
        elif event.button.id=='policy-save':
            try:
                save_policy(self.path,self.catalog,self.config,self.query_one('#policy-mode',Select).value,self.operations)
                self.dismiss(True)
            except ValueError as error:self.app.notify(str(error),severity='error')
    def action_back(self):self.dismiss(None)


def open_project_view(app,key,offset=0):
    if key=='results':
        if not app.controller.history_path(app.project).exists():app.notify('Сохранённых результатов пока нет');return
        with app.controller.history(app.project) as history:page=history.pinned_reports(app.project,limit=7,offset=offset)
        rows=page['items'];options=[(str(i),r['label'] or r['summary']) for i,r in enumerate(rows)]
        if page['more']:options.append(('more','Показать ещё'))
        def selected(value):
            if value=='more':open_project_view(app,key,offset+7)
            elif value is not None:
                row=rows[int(value)];app.open_session(app.project,row['session_id']);app.inspect_message(row['run_id'],'assistant')
        app.push_screen(ChoiceDialog('Сохранённые результаты',options),selected);return
    config=app.store.project(app.project)['configuration']
    if key=='settings':
        values={**config,'label':app.store.project(app.project)['label']}
        app.push_screen(FormDialog('Настройки проекта: target сохраняется',PROJECT_FIELDS,values=values),app.create_project);return
    if not Path(config['spec_path']).is_file():raise ValueError('desktop_local_spec_required')
    catalog=load_source(config['spec_path'],target=config['target']);policy=ProjectConfig.load(config['policy_path'],catalog)
    app.push_screen(OperationsDialog(catalog,policy,path=config['policy_path'] if key=='policy' else None))


def inspector_action(app,key):
    scope=app.inspected_scope;run_id=app.inspected_run
    if key=='pin':
        with app.controller.history(scope[0],write=True) as history:history.pin_report(*scope,run_id)
        app.set_status('Отчёт закреплён в проекте')
    elif key=='reuse-fields':
        if not app.selection:raise ValueError('Выберите публичные поля одного объекта клавишей Space')
        if scope[0]!=app.project:raise ValueError('attachment_foreign_project')
        if app.selection not in app.draft.setdefault('attachments',[]):app.draft['attachments'].append(deepcopy(app.selection))
        app.action_save();app.set_status('Данные прикреплены к черновику; условия не копировались')
    elif key=='copy-conditions':
        run=app.controller.read_run(*scope,run_id)
        manifest=run['admission_manifest']['goal_contract']['manifest']
        app.draft['cards']=deepcopy(manifest['cards']);app.action_save();app.update_controls()
        app.set_status('Условия скопированы в черновик; перед запуском нужна валидация')
    elif key=='continue':
        if app.controller.busy or app.preparing or app.launching or app.closing:raise ValueError('desktop_run_busy')
        app.preparing=True;app.prepare_send(app.project,app.session,{},origin=(scope[1],run_id))

"""Каталог, права и автоматически сохранённые результаты проекта."""
from copy import deepcopy
import json
import os
from pathlib import Path
import uuid
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, DataTable, Input, Label, Select, Static, OptionList, Collapsible
from textual.widgets.option_list import Option
from api_agent.desktop.dialogs import ChoiceDialog, FormDialog, PROJECT_FIELDS
from api_agent.desktop.widgets import PageDialog, JsonCode
from api_agent.desktop.presentation import method_text, conditions_summary, error_text, scope_summary, literal
from api_agent.openapi import load_source
from api_agent.openapi.documentation import documentation_preview
from api_agent.policy.config import ProjectConfig, decision

ACCESS=[('Разрешено','ALLOW'),('Требуется подтверждение','CONFIRM'),('Запрещено (видно модели)','DENY'),('Скрыто от модели','HIDE')]


def operation_rows(catalog,config,*,search='',method='ALL'):
    rows=[]
    for key,operation in catalog.operations.items():
        if method!='ALL' and operation['method']!=method:continue
        if search.casefold() not in (key+' '+operation['path']).casefold():continue
        rows.append({'id':key,'method':operation['method'],'path':operation['path'],
            'risk':config.snapshot['operations'].get(key,{}).get('risk','—')})
    return rows


def preset_operations(catalog,preset):
    result={}
    for key,op in catalog.operations.items():
        method=op['method']
        if preset=='TEST' or method in {'GET','HEAD'}:result[key]='ALLOW'
        elif method=='DELETE' and preset!='READ_ONLY':result[key]='DENY'
        else:result[key]='CONFIRM'
    return result


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


class OperationsDialog(PageDialog):
    DEFAULT_CSS='''OperationsDialog #catalog-layout {height: 1fr; min-height: 8;}
    OperationsDialog #catalog-list {width: 43%;}
    OperationsDialog #catalog-details {width: 1fr; padding-left: 2;}
    OperationsDialog #operations {height: 1fr;}
    OperationsDialog #op-search {width: 2fr;}
    OperationsDialog #op-method {width: 1fr;}
    OperationsDialog #catalog-filters {height: 3;}
    OperationsDialog #policy-controls {height: auto;}
    OperationsDialog #policy-controls .page-actions {padding-top: 0;}
    OperationsDialog #op-description {margin-bottom: 1;}
    '''
    def __init__(self,catalog,config,*,path=None):
        super().__init__();self.catalog=catalog;self.config=config;self.path=path
        self.operations={k:v['risk'] for k,v in config.snapshot['operations'].items()};self.selected=None
    def compose(self):
        with Vertical(classes='page-shell'):
            yield Label('Политики операций' if self.path else 'Каталог операций',classes='page-title')
            with VerticalScroll(id='catalog-body',classes='page-body'):
                with Horizontal(id='catalog-filters'):
                    yield Input(placeholder='Поиск по имени / пути',id='op-search')
                    yield Select([(m,m) for m in ['ALL','GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS']],value='ALL',id='op-method',allow_blank=False)
                with Horizontal(id='catalog-layout'):
                    with Vertical(id='catalog-list'):yield DataTable(id='operations',cursor_type='row')
                    with VerticalScroll(id='catalog-details'):
                        yield Static('Выберите операцию',id='op-description',markup=False)
                        yield JsonCode({},id='op-detail')
                if self.path:
                    with Vertical(id='policy-controls'):
                        with Horizontal(classes='page-actions'):
                            yield Select([('Стандартная: удаление запрещено','DEFAULT'),('Тестовая: все операции разрешены','TEST'),('Только чтение: остальное с подтверждением','READ_ONLY')],prompt='Шаблон для всех операций',id='policy-preset')
                            yield Button('Применить шаблон',id='policy-preset-apply')
                        with Horizontal(classes='page-actions'):
                            yield Select(ACCESS,prompt='Доступ выбранной операции',id='op-risk')
                            yield Button('Применить к операции',id='op-grant')
                    with Collapsible(title='Как работают шаблоны',collapsed=True,id='policy-help'):
                        yield Static('Стандартная: чтение разрешено, запись с подтверждением, удаление запрещено. Тестовая: всё разрешено. Только чтение: GET/HEAD разрешены, остальное с подтверждением. Шаблон меняет весь набор; отдельные права настраиваются выше. Сохранение новой версии выполняется отдельно.')
            with Horizontal(classes='page-actions'):
                yield Button('Назад',id='op-back')
                if self.path:yield Button('Сохранить политику',id='policy-save',variant='primary')
    def on_mount(self):
        self.query_one(DataTable).add_columns('Метод','Путь','Доступ');self.refresh_rows()
    def access(self,key):
        risk=self.operations.get(key,'DENY')
        if risk in {'HIDE','DENY','ALLOW','CONFIRM'}:return risk
        return decision(self.config.snapshot,key,self.catalog.operation_hashes[key])
    def refresh_rows(self):
        table=self.query_one(DataTable);table.clear()
        search=self.query_one('#op-search',Input).value;method=self.query_one('#op-method',Select).value
        for row in operation_rows(self.catalog,self.config,search=search,method=method):
            access=self.access(row['id']);title=dict((v,k) for k,v in ACCESS).get(access,access)
            table.add_row(method_text(row['method']),row['path'],title,key=row['id'])
    def on_input_changed(self,event):
        if self.is_mounted:self.refresh_rows()
    def on_select_changed(self,event):
        if event.select.id=='op-method' and self.is_mounted:self.refresh_rows()
    def show_operation(self,key):
        self.selected=key;op=self.catalog.operations[key]
        description=documentation_preview(op,self.catalog.documentation.get(key,{})).get('description','')
        summary=self.catalog.search_metadata.get(key,{}).get('summary','')
        if summary:description=summary+('\n'+description if description else '')
        self.query_one('#op-description',Static).update(op['method']+' '+op['path']+'\n'+key+('\n'+description if description else ''))
        self.query_one('#op-detail',JsonCode).show(op)
        if self.path:self.query_one('#op-risk',Select).value=self.access(key)
    def on_data_table_row_selected(self,event):self.show_operation(str(event.row_key.value))
    def on_data_table_row_highlighted(self,event):
        if event.row_key.value is not None:self.show_operation(str(event.row_key.value))
    def on_button_pressed(self,event):
        event.stop();key=event.button.id
        if key=='op-back':self.dismiss(None)
        elif key=='op-grant' and self.selected:
            value=self.query_one('#op-risk',Select).value
            if value is not Select.NULL:self.operations[self.selected]=value;self.refresh_rows()
        elif key=='policy-preset-apply':
            preset=self.query_one('#policy-preset',Select).value
            if preset is not Select.NULL:self.operations=preset_operations(self.catalog,preset);self.refresh_rows()
        elif key=='policy-save':
            try:save_policy(self.path,self.catalog,self.config,self.config.snapshot['mode'],self.operations);self.dismiss(True)
            except ValueError as error:self.app.notify(error_text(error),severity='error')


class ResultsDialog(PageDialog):
    DEFAULT_CSS='''ResultsDialog #result-layout {height: 1fr;}
    ResultsDialog #result-list {height: 1fr; width: 40%;}
    ResultsDialog #result-detail {width: 1fr; padding: 0 2;}
    ResultsDialog #result-open {height: 3;}'''
    def __init__(self,rows,*,more=False):super().__init__();self.rows=rows;self.more=more
    def compose(self):
        with Vertical(classes='page-shell'):
            yield Label('Сохранённые результаты',classes='page-title')
            options=[]
            for i,row in enumerate(self.rows):
                conditions=row.get('conditions') or [];passed=sum(x.get('verdict')=='PASS' for x in conditions)
                checks='Проверки не заданы' if not conditions else 'Проверки: '+str(row.get('checks_verdict'))+' ('+str(passed)+'/'+str(len(conditions))+')'
                label=(row.get('goal') or 'Цель не сохранена')+'\n'+checks+' · '+str(row.get('summary') or '')
                options.append(Option(label,id=str(i)))
            if self.more:options.append(Option('Показать следующие результаты',id='more'))
            with Horizontal(id='result-layout'):
                yield OptionList(*options,id='result-list')
                with VerticalScroll(id='result-detail'):
                    yield Static('Выберите результат: справа появятся цель, условия и результаты проверок.',id='result-summary',markup=False)
            if not self.rows:yield Static('Завершённых запусков пока нет.')
            yield Static('Enter / нажатие — открыть инспектор соответствующего запуска.')
            yield Button('Назад',id='results-back')
    def on_option_list_option_highlighted(self,event):
        key=event.option.id
        if key=='more':return
        row=self.rows[int(key)]
        lines=['Цель: '+str(row.get('goal') or 'не сохранена'),'','Условия:',conditions_summary({'cards':row.get('cards',[])}),'','Результат приложения: '+str(row.get('summary') or '')]
        for result in row.get('conditions') or []:
            expected=result.get('expected') or {}
            field=expected.get('path','HTTP-статус');value=expected.get('value',expected.get('values',expected.get('expected','наличие поля')))
            lines.append(str(result.get('verdict'))+' · '+field+' · ожидалось: '+literal(value)+' · получено: '+literal(result.get('actual')))
        self.query_one('#result-summary',Static).update('\n'.join(lines))
    def on_option_list_option_selected(self,event):
        event.stop();key=event.option.id
        self.dismiss('more' if key=='more' else self.rows[int(key)])
    def on_button_pressed(self,event):event.stop();self.dismiss(None)


def open_project_view(app,key,offset=0):
    if key=='results':
        if not app.controller.history_path(app.project).exists():app.notify('Сохранённых результатов пока нет');return
        with app.controller.history(app.project) as history:page=history.saved_reports(app.project,limit=7,offset=offset)
        def selected(value):
            if value=='more':open_project_view(app,key,offset+7)
            elif value:
                # Отложить перестройку до полного закрытия modal и обработки его mouse event.
                def open_result():
                    # Явный переход к истории восстанавливает её сессию в дереве.
                    app.store.hide_session(app.project,value['session_id'],hidden=False)
                    app.refresh_navigation()
                    app.open_session(app.project,value['session_id']);app.inspect_message(value['run_id'],'assistant')
                app.call_after_refresh(open_result)
        app.push_screen(ResultsDialog(page['items'],more=page['more']),selected);return
    config=app.store.project(app.project)['configuration']
    if key=='settings':
        values={**config,'label':app.store.project(app.project)['label']}
        app.push_screen(FormDialog('Настройки проекта',PROJECT_FIELDS,values=values),app.create_project);return
    if not Path(config['spec_path']).is_file():raise ValueError('desktop_local_spec_required')
    catalog=load_source(config['spec_path'],target=config['target']);policy=ProjectConfig.load(config['policy_path'],catalog)
    app.push_screen(OperationsDialog(catalog,policy,path=config['policy_path'] if key=='policy' else None))


def inspector_action(app,key):
    scope=app.inspected_scope;run_id=app.inspected_run
    if key=='copy-conditions':
        run=app.controller.read_run(*scope,run_id)
        manifest=run['admission_manifest']['goal_contract']['manifest']
        app.copy_to_clipboard(json.dumps([c for c in manifest['cards'] if c.get('enabled',True)],ensure_ascii=False,indent=2))
        app.set_status('Условия скопированы')
    elif key=='continue':
        if app.controller.busy or app.preparing or app.launching or app.closing:raise ValueError('desktop_run_busy')
        app.preparing=True;app.prepare_send(app.project,app.session,{},origin=(scope[1],run_id))

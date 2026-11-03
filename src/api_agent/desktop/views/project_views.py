"""Каталог, права и автоматически сохранённые результаты проекта."""
from api_agent.desktop.components.controls import Button, Input, Select, Summary as Static
from api_agent.desktop.components.controls import highlighted
from copy import deepcopy
import json
import os
from pathlib import Path
import uuid
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import DataTable, Label, OptionList, Collapsible
from textual.widgets.option_list import Option
from api_agent.desktop.views.dialogs import ChoiceDialog, FormDialog, PROJECT_FIELDS
from api_agent.desktop.components.widgets import PageDialog, JsonCode
from api_agent.desktop.presentation.presentation import method_text, conditions_summary, error_text, scope_summary, literal
from api_agent.openapi import load_source
from api_agent.openapi.documentation import documentation_preview
from api_agent.policy.config import ProjectConfig, decision

ACCESS=[('Разрешено','ALLOW'),('Требуется подтверждение','CONFIRM'),('Запрещено','DENY'),('Скрыто','HIDE')]


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
    DEFAULT_CSS='''OperationsDialog #catalog-layout {height: 1fr; min-height: 17;}
    OperationsDialog #catalog-list {width: 47%;}
    OperationsDialog #catalog-details {width: 1fr; padding-left: 2;}
    OperationsDialog #operations {height: 1fr; min-height: 5;}
    OperationsDialog #op-search {width: 2fr;}
    OperationsDialog #op-method {width: 1fr;}
    OperationsDialog #catalog-filters {height: 4;}
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
                    yield Select([(method_text(m),m) for m in ['ALL','GET','POST','PUT','PATCH','DELETE','HEAD','OPTIONS']],value='ALL',id='op-method',allow_blank=False)
                    yield Select([('Все права','ALL')]+[(highlighted(label),value) for label,value in ACCESS],value='ALL',id='op-access',allow_blank=False)
                with Horizontal(id='catalog-layout'):
                    with Vertical(id='catalog-list'):
                        yield DataTable(id='operations',cursor_type='row')
                        if self.path:
                            with Vertical(id='policy-controls'):
                                yield Label('Шаблон всех операций',classes='section-title')
                                yield Select([(highlighted('Стандартная · запись с подтверждением'),'DEFAULT'),(highlighted('TEST · всё разрешено'),'TEST'),(highlighted('Только чтение · остальное с подтверждением'),'READ_ONLY')],prompt='Шаблон',id='policy-preset')
                                yield Label('Доступ выбранной операции',classes='section-title')
                                yield Select([(highlighted(label),value) for label,value in ACCESS],prompt='Доступ',id='op-risk')
                            with Collapsible(title='О шаблонах',collapsed=True,id='policy-help'):
                                yield Static('Выбор шаблона меняет черновик всех операций. После него можно настроить отдельные операции. Изменения вступят в силу только после сохранения. Стандартная: чтение разрешено, запись с подтверждением, DELETE запрещён; TEST: всё разрешено; Только чтение: GET/HEAD разрешены, остальное с подтверждением.')
                    with VerticalScroll(id='catalog-details'):
                        yield Static('Выберите операцию',id='op-description',markup=False)
                        yield JsonCode({},id='op-detail')
            with Horizontal(classes='page-actions'):
                yield Button('Назад',id='op-back')
                if self.path:yield Button('Сохранить политику',id='policy-save',variant='primary')
    def on_mount(self):
        table=self.query_one(DataTable)
        table.add_columns('👁','Метод','Доступ','Путь')
        table.tooltip='👁 Операция видна модели, включая запрещённые; ⊘ операция скрыта от модели. Доступ и видимость — разные свойства.'
        self.refresh_rows()
    def access(self,key):
        risk=self.operations.get(key,'DENY')
        if risk in {'HIDE','DENY','ALLOW','CONFIRM'}:return risk
        return decision(self.config.snapshot,key,self.catalog.operation_hashes[key])
    def refresh_rows(self):
        table=self.query_one(DataTable);selected=self.selected
        search=self.query_one('#op-search',Input).value;method=self.query_one('#op-method',Select).value
        with table.prevent(DataTable.RowHighlighted):
            table.clear()
            for row in operation_rows(self.catalog,self.config,search=search,method=method):
                access=self.access(row['id']);title=dict((v,k) for k,v in ACCESS).get(access,access)
                if self.query_one('#op-access',Select).value not in {'ALL',access}:continue
                if access=='CONFIRM':title='Подтверждение'
                table.add_row('⊘' if access=='HIDE' else '👁',method_text(row['method']),highlighted(title),highlighted(row['path']),key=row['id'])
            keys=[str(key.value) for key in table.rows]
            if keys:
                key=selected if selected in keys else keys[0]
                table.move_cursor(row=table.get_row_index(key));self.show_operation(key)
    def on_input_changed(self,event):
        if self.is_mounted:self.refresh_rows()
    def on_select_changed(self,event):
        if not self.is_mounted or event.value!=event.select.value:return
        if event.select.id in {'op-method','op-access'}:self.refresh_rows()
        elif self.path and event.select.id=='op-risk' and self.selected and event.value is not Select.NULL:
            self.operations[self.selected]=event.value;self.refresh_rows()
        elif self.path and event.select.id=='policy-preset' and event.value is not Select.NULL:
            self.operations=preset_operations(self.catalog,event.value);self.refresh_rows()
    def show_operation(self,key):
        self.selected=key;op=self.catalog.operations[key]
        description=documentation_preview(op,self.catalog.documentation.get(key,{})).get('description','')
        summary=self.catalog.search_metadata.get(key,{}).get('summary','')
        if summary:description=summary+('\n'+description if description else '')
        self.query_one('#op-description',Static).update(op['method']+' '+op['path']+'\n'+key+('\n'+description if description else ''))
        self.query_one('#op-detail',JsonCode).show(op)
        if self.path:
            select=self.query_one('#op-risk',Select)
            with select.prevent(Select.Changed):select.value=self.access(key)
    def on_data_table_row_selected(self,event):self.show_operation(str(event.row_key.value))
    def on_data_table_row_highlighted(self,event):
        if event.row_key.value is not None:self.show_operation(str(event.row_key.value))
    def on_button_pressed(self,event):
        event.stop();key=event.button.id
        if key=='op-back':self.dismiss(None)
        elif key=='policy-save':
            try:save_policy(self.path,self.catalog,self.config,self.config.snapshot['mode'],self.operations);self.dismiss(True)
            except ValueError as error:self.app.notify(error_text(error),severity='error')


class ResultsDialog(PageDialog):
    DEFAULT_CSS='''ResultsDialog #result-layout {height: 1fr;}
    ResultsDialog #result-list {height: 1fr; width: 40%;}
    ResultsDialog #result-detail {width: 1fr; padding: 0 2;}
    ResultsDialog #result-open {height: 1;}
    ResultsDialog #result-list > .option-list--option {padding: 0 1 1 1;}
    ResultsDialog #result-summary {height: auto;}'''
    def __init__(self,rows,*,more=False,loader=None):
        super().__init__();self.rows=list(rows);self.more=more;self.loader=loader
    def option(self,row,index):
        conditions=row.get('conditions') or [];passed=sum(x.get('verdict')=='PASS' for x in conditions)
        checks='Проверки не заданы' if not conditions else 'Проверки: '+str(row.get('checks_verdict'))+' ('+str(passed)+'/'+str(len(conditions))+')'
        goal=' '.join((row.get('goal') or 'Цель не сохранена').split())
        goal=goal if len(goal)<=74 else goal[:73]+'…'
        return Option(highlighted(goal+'\n'+checks),id=str(index))
    def load_more(self):
        if not self.more or self.loader is None:return
        page=self.loader(len(self.rows));start=len(self.rows)
        self.rows.extend(page['items']);self.more=bool(page['items']) and page['more']
        self.query_one('#result-list',OptionList).add_options(self.option(row,start+i) for i,row in enumerate(page['items']))
    def on_mount(self):self.set_interval(.3,self.load_visible_page)
    def load_visible_page(self):
        views=list(self.query('#result-list'))
        if not self.is_active or not views:return
        view=views[0]
        if view.scroll_y>=view.max_scroll_y-1:self.load_more()
    def on_mouse_scroll_down(self,event):
        view=self.query_one('#result-list',OptionList)
        if view.is_mouse_over and view.scroll_y>=view.max_scroll_y-2:self.load_more()
    def compose(self):
        with Vertical(classes='page-shell'):
            yield Label('Сохранённые результаты',classes='page-title')
            options=[self.option(row,i) for i,row in enumerate(self.rows)]
            with Horizontal(id='result-layout'):
                yield OptionList(*options,id='result-list')
                with VerticalScroll(id='result-detail'):
                    yield Static('Выберите результат: справа появятся цель, условия и результаты проверок.',id='result-summary',markup=False)
            if not self.rows:yield Static('Завершённых запусков пока нет.')
            yield Static('Enter / нажатие — открыть инспектор соответствующего запуска.')
            yield Button('Назад',id='results-back')
    def on_option_list_option_highlighted(self,event):
        key=event.option.id
        if int(key)>=len(self.rows)-1:self.load_more()
        row=self.rows[int(key)]
        lines=['Цель: '+str(row.get('goal') or 'не сохранена'),'','Условия:',conditions_summary({'cards':row.get('cards',[])}),'','Результат приложения: '+str(row.get('summary') or ''),'','Результаты проверок:']
        for result in row.get('conditions') or []:
            expected=result.get('expected') or {}
            field=expected.get('path','HTTP-статус');value=expected.get('value',expected.get('values',expected.get('expected','наличие поля')))
            lines.append(str(result.get('verdict'))+' · '+field+' · ожидалось: '+literal(value)+' · получено: '+literal(result.get('actual')))
        self.query_one('#result-summary',Static).update('\n'.join(lines))
    def on_option_list_option_selected(self,event):
        event.stop();key=event.option.id
        self.dismiss(self.rows[int(key)])
    def on_button_pressed(self,event):event.stop();self.dismiss(None)


def open_project_view(app,key,offset=0):
    if key=='results':
        if not app.controller.history_path(app.project).exists():app.notify('Сохранённых результатов пока нет');return
        project=app.project
        def load(start):
            with app.controller.history(project) as history:return history.saved_reports(project,limit=20,offset=start)
        page=load(0)
        def selected(value):
            if value:
                # Отложить перестройку до полного закрытия modal и обработки его mouse event.
                def open_result():
                    # Явный переход к истории восстанавливает её сессию в дереве.
                    app.store.hide_session(app.project,value['session_id'],hidden=False)
                    app.refresh_navigation()
                    app.open_session(app.project,value['session_id']);app.inspect_message(value['run_id'],'assistant')
                app.call_after_refresh(open_result)
        app.push_screen(ResultsDialog(page['items'],more=page['more'],loader=load),selected);return
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

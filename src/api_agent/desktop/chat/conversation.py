"""Сообщения, актуальный HTTP-инспектор и явное копирование результатов."""
from api_agent.desktop.components.controls import Button, Summary as Static
from api_agent.desktop.components.controls import highlighted
from copy import deepcopy
import json
import uuid
from urllib.parse import urlsplit
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Label, ListItem, ListView, TextArea, TabbedContent
from api_agent.desktop.views.inspector import JsonInspector
from api_agent.desktop.presentation.presentation import conditions_summary, card_summary, duration, method_text, literal, error_text
from api_agent.desktop.presentation.trace_export import run_trace
from api_agent.desktop.presentation.ui_errors import read_feedback


class MessageItem(ListItem):
    def __init__(self,message,key,summary=''):
        super().__init__(id=key,classes='user-message' if message['role']=='user' else 'agent-message')
        self.message=message;self.summary=summary
    def compose(self):
        with Vertical(classes='bubble'):
            yield Label('Вы' if self.message['role']=='user' else 'Ответ агента' if self.message.get('model_answer',True) else 'Сообщение приложения',classes='message-role')
            yield Static(self.message['content'],markup=False,classes='message-text')
            if self.summary:yield Static(self.summary,markup=False,classes='program-result')
            if self.message.get('run_id'):
                with Horizontal(classes='message-actions'):
                    if self.message['role']=='assistant':
                        yield Button('⧉ Ответ',id='answer-'+self.id,tooltip='Копировать текст ответа')
                        yield Button('⧉ JSON-трейс',id='trace-'+self.id,tooltip='Копировать JSON-трейс этого запуска')
                    else:
                        yield Button('⧉ Цель',id='goal-'+self.id,tooltip='Копировать текст цели')
                        yield Button('⧉ Условия',id='conditions-'+self.id,tooltip='Копировать включённые условия в JSON')
    def on_mount(self):self.fit_bubble()
    def on_resize(self):self.fit_bubble()
    def fit_bubble(self):
        available=max(12,self.parent.size.width-4) if self.parent else 60
        maximum=max(12,int(available*.9))
        width=max(24,max((len(line) for line in (self.message['content']+'\n'+self.summary).splitlines()),default=0)+4)
        # Отложенный Mount/Resize допустим после снятия children через clear().
        for bubble in self.query('.bubble'):bubble.styles.width=min(maximum,width)


class ConversationMixin:
    @read_feedback
    def refresh_messages(self):
        view=self.query_one('#conversation',ListView)
        if not self.project:return
        page=self.controller.messages(self.project,self.session,offset=self.message_offset)
        messages=list(page['items'])
        if self.pending_goal and self.pending_goal['scope']==(self.project,self.session):
            messages.append({'message_id':'pending','role':'user','content':self.pending_goal['goal'],'run_id':None})
        identities=[m['message_id'] for m in messages]
        if identities==getattr(self,'rendered_message_ids',[]) and self.rendered_scope==(self.project,self.session):return
        self.rendered_message_ids=identities;self.rendered_scope=(self.project,self.session)
        # clear() снимает виджеты асинхронно: при соседних checkpoint-событиях
        # прежние ID ещё могут быть зарегистрированы до завершения remove.
        self.message_generation=getattr(self,'message_generation',0)+1
        view.clear();self.message_data={}
        for message in messages:
            key='message-'+str(self.message_generation)+'-'+str(message['message_id']);summary=''
            if message['role']=='assistant':
                run=self.controller.read_run(self.project,self.session,message['run_id']);report=run.get('report') or {}
                message['model_answer']=bool((run.get('final') or {}).get('summary'))
                summary='Результат приложения: '+report.get('summary',error_text(run.get('stop_reason') or 'Прервано'))
            self.message_data[key]=message;view.append(MessageItem(message,key,summary))
        self.query_one('#messages-more',Button).display=bool(page.get('more',False))
    def on_list_view_selected(self,event):
        message=self.message_data.get(event.item.id)
        if message and message.get('run_id'):self.inspect_message(message['run_id'],message['role'])
        elif message:self.set_status('Цель принята к подготовке. Инспектор откроется после создания запуска.')
    def copy_message(self,key):
        for prefix in ['answer-','goal-','trace-','conditions-']:
            if not key.startswith(prefix):continue
            message=self.message_data.get(key[len(prefix):])
            if not message:return False
            run=self.controller.read_run(self.project,self.session,message['run_id'])
            if prefix in {'answer-','goal-'}:text=message['content']
            elif prefix=='trace-':text=run_trace(run)
            else:
                manifest=run['admission_manifest']['goal_contract']['manifest']
                text=json.dumps([c for c in manifest.get('cards',[]) if c.get('enabled',True)],ensure_ascii=False,indent=2)
            self.copy_to_clipboard(text);self.set_status('Скопировано в буфер обмена');return True
        return False


class InspectorMixin:
    @read_feedback
    def inspect_message(self,run_id,role):
        run=self.controller.read_run(self.project,self.session,run_id)
        self.follow_live=False;self.inspected_run=run_id;self.inspected_scope=(self.project,self.session)
        self.inspected_role=role;self.exchange_offset=0;self.selection=None
        self.query_one('#json-tree',JsonInspector).reset_exchange()
        manifest=(run.get('admission_manifest',{}).get('goal_contract') or {}).get('manifest',{})
        self.query_one('#configuration',TextArea).text=json.dumps(manifest,ensure_ascii=False,indent=2)
        self.query_one('#config-summary',Static).update(conditions_summary(manifest))
        self.query_one('#copy-conditions',Button).display=role=='user'
        self.query_one('#config-constructor',Button).display=role=='user'
        self.query_one('#inspector').display=True
        self.set_class(self.size.width<100,'inspector-full')
        self.query_one(TabbedContent).active='config-tab' if role=='user' else 'trace-tab'
        if role=='assistant':self.store.mark(self.project,self.session,'');self.refresh_navigation()
        self.refresh_exchanges();self.refresh_report(run);self.update_controls()
    def refresh_report(self,run):
        table=self.query_one('#conditions',DataTable);table.clear()
        self.conditions=(run.get('report') or {}).get('conditions',[])
        manifest=((run.get('admission_manifest') or {}).get('goal_contract') or {}).get('manifest',{})
        names={c['id']:(c.get('name') or c['operation_id']).strip() for c in manifest.get('cards',[]) if c.get('enabled',True)}
        previous=None
        for index,result in enumerate(self.conditions):
            scope=result.get('scope') or {};name=names.get(scope.get('id'),'Проверка')
            if scope.get('id')!=previous:
                previous=scope.get('id')
                table.add_row(highlighted('Карточка: '+name),'','','',key='group-'+str(index))
            expected=result.get('expected') or {};field=expected.get('path','HTTP-статус')
            expected_value=expected.get('value',expected.get('values',expected.get('expected','Наличие поля')))
            table.add_row(highlighted('  '+field),highlighted(literal(expected_value)),highlighted(literal(result.get('actual'))),
                highlighted(result['verdict']+' · '+('ожидание подтверждено' if result.get('reason')=='expectation_met' else error_text(result.get('reason','')))),key=str(index))
    @read_feedback
    def refresh_exchanges(self,*,preserve=False):
        if not self.inspected_run:return
        page=self.controller.exchanges(*self.inspected_scope,self.inspected_run,offset=self.exchange_offset)
        table=self.query_one('#exchanges',DataTable);cursor=table.cursor_row;table.clear()
        if not preserve:
            self.selected_exchange=None;self.selection=None;self.query_one('#json-tree',JsonInspector).reset_exchange()
        for row in page['items']:
            request=row['request'];url=urlsplit(str(request.get('url') or ''))
            route=url.path+('?' + url.query if url.query else '')
            outcome=row['outcome'];status=str(row['status']) if row.get('status') else ''
            if outcome!='received':status+=(' · ' if status else '')+error_text(outcome)
            table.add_row(str(row['sequence']),method_text(str(request.get('method'))),highlighted(route),
                highlighted(status),duration(row.get('wall_seconds'))+
                (' · НЕОПРЕДЕЛЁН' if row.get('effect_status')=='uncertain' else ''),key=row['exchange_id'])
        if page.get('more'):table.add_row('…','','Следующие 20','','',key='__more__')
        if preserve and table.row_count:table.move_cursor(row=min(cursor,table.row_count-1),animate=False)
        if preserve and self.selected_exchange:
            # Обновлять только тот же выбранный обмен; новый HTTP не перехватывает фокус.
            self.show_exchange(self.selected_exchange)
    def on_data_table_row_selected(self,event):
        key=str(event.row_key.value)
        if event.control.id=='conditions':
            if key.startswith('group-'):return
            ids=self.conditions[int(key)].get('evidence_ids',[])
            if ids:self.show_exchange(ids[-1]);self.query_one(TabbedContent).active='trace-tab'
        elif event.control.id=='exchanges':
            if key=='__more__':self.exchange_offset+=20;self.refresh_exchanges()
            else:self.show_exchange(key)
    @read_feedback
    def show_exchange(self,key):
        run=self.controller.read_run(*self.inspected_scope,self.inspected_run)
        exchange=next((e for e in run['exchanges'] if e['id']==key),None)
        if exchange is None:return
        self.selected_exchange=key;self.selection=None
        tree=self.query_one('#json-tree',JsonInspector)
        tree.show_exchange(lambda section,path,offset:self.controller.section(*self.inspected_scope,self.inspected_run,key,
            section=section,pointer=path,offset=offset),field_picker=self.pick_fields,
            metadata={k:exchange.get(k) for k in ['request_parameters','request_principal','outcome','truncated','wall_seconds','effect_status','transport_error']})
        chosen=set()
        for item in self.draft.get('attachments',[]):
            if item.get('run_id')==self.inspected_run and item.get('exchange_id')==key:
                chosen.update(item.get('object_path','')+p for p in item.get('fields',[]))
        tree.chosen=chosen
    def pick_fields(self,paths):
        if not self.inspected_run or not self.selected_exchange:return
        descriptors=self.draft.setdefault('attachments',[])
        matching=lambda d:d.get('run_id')==self.inspected_run and d.get('exchange_id')==self.selected_exchange
        # Снять отметки удаляет только прикрепление текущего обмена, не другие объекты.
        remaining=[d for d in descriptors if not matching(d)]
        if not paths:self.selection=None;self.draft['attachments']=remaining;self.action_save();self.update_controls();return
        parents={p.rsplit('/',1)[0] for p in paths}
        if len(parents)!=1:raise ValueError('Выберите поля одного объекта')
        parent=parents.pop()
        with self.controller.history(self.project) as history:
            self.selection=history.select_attachment(*self.inspected_scope,self.inspected_run,self.selected_exchange,
                object_path=parent,fields=[p[len(parent):] for p in paths])
        self.draft['attachments']=remaining+[deepcopy(self.selection)];self.action_save();self.update_controls()
    @read_feedback
    def finish_inspected(self):
        run=self.controller.read_run(*self.inspected_scope,self.inspected_run)
        self.refresh_exchanges(preserve=True);self.refresh_report(run)

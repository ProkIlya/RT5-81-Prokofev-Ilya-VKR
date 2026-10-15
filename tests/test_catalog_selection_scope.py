"""Условия выборки не переходят на другой запрос даже при том же operationId."""
import json
import threading
import tempfile
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/'src'),str(Path(__file__).resolve().parent)]
from api_agent.agent import StartAgentRun
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig
from agent_support import reply,count_test_tokens


class SelectionScopeTests(unittest.TestCase):
    def run_case(self, scope, *, extra=False, global_goal=False, present=False, empty_global=False, history=False):
        journal=[]
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                journal.append(self.path)
                body=json.dumps([{'label':'archived'}] if present or ('?' not in self.path and not empty_global) else []).encode()
                self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        threading.Thread(target=server.serve_forever,daemon=True).start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        target='http://127.0.0.1:'+str(server.server_port)
        op={'operationId':'scanRecords','parameters':[{'name':'category','in':'query','required':False,'schema':{'type':'string'}}],
            'responses':{'200':{'description':'ok','content':{'application/json':{'schema':{'type':'array','items':{'type':'object','properties':{'label':{'type':'string'}},'required':['label']}}}}}}}
        cat=parse_document({'openapi':'3.0.3','info':{'title':'scope','version':'1'},'paths':{'/records':{'get':op}}})
        cfg=ProjectConfig.create('p',target,cat,mode='READ_ONLY',resettable=True,operations={'scanRecords':'A'})
        conditions={'scanRecords':[{'predicate':'json_collection_matches','parameters':{'path':'','rows':[{'/label':'archived'}],'mode':'absent'},'intent':'positive','preconditions':'verified'}]}
        class InspectableWorkflow(CatalogWorkflow):
            # Дополнительный read в контроле разрешаем явно, чтобы проверить
            # сам validator/report, а не только подсказку finish-only.
            def phase_registry(self,run,registry):return registry if extra else super().phase_registry(run,registry)
            def next_tool(self,run):return None if extra else super().next_tool(run)
        options={} if scope is None else {'selection_scopes':{'scanRecords':scope}}
        wf=InspectableWorkflow(cat,cfg,goal='Проверь отсутствие в заданной области',conditions=conditions,**options)
        payloads=[]
        def model(payload):
            payloads.append(payload)
            data=[json.loads(m['content']).get('data') for m in payload['messages'] if m['role']=='tool']
            exchanges=[d for d in data if isinstance(d,dict) and 'exchange_id' in d]
            if not exchanges:return reply('execute_operation',{'operation_id':'scanRecords','parameters':{'category':'active'}})
            if len(exchanges)==1 and (extra or global_goal):return reply('execute_operation',{'operation_id':'scanRecords'})
            return reply('finish_run',{'summary':'Проверка завершена','evidence_ids':[d['exchange_id'] for d in exchanges]})
        store=None
        if history:
            from api_agent.storage import SQLiteHistory
            temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
            history_path=Path(temporary.name)/'history.sqlite3'
            store=SQLiteHistory(history_path)
        app=StartAgentRun(target,cat,workflow=wf,token_counter=count_test_tokens,history_store=store)
        run=app.start('p','s','m',wf.goal,model)
        self.assertEqual(app.start('p','s','m',wf.goal,model)['run_id'],run['run_id'])
        if history:
            snapshot=store.snapshot('p','s',run['run_id'])
            self.assertEqual(snapshot['metadata']['selection_scopes'],wf.selection_scopes)
            store.close()
            with SQLiteHistory(history_path) as reopened:
                def forbidden(payload):self.fail('История повторно вызвала модель')
                app=StartAgentRun(target,cat,workflow=wf,token_counter=count_test_tokens,history_store=reopened)
                restored=app.start('p','s','m',wf.goal,forbidden)
                self.assertEqual(restored,run)
        return run,journal,payloads

    def test_explicit_filtered_absence_finishes_without_widening(self):
        run,journal,_=self.run_case({'mode':'exact','parameters':{'category':'active'}})
        self.assertEqual(journal,['/records?category=active'])
        self.assertEqual(run['status'],'completed')
        self.assertEqual(run['report']['checks_verdict'],'PASS')
        self.assertEqual(run['report']['goal_verdict'],'UNKNOWN')
        self.assertFalse(run['steps'][0]['observation']['data'].get('verify_unfiltered',False))

    def test_extra_unfiltered_response_cannot_replace_filtered_result(self):
        run,journal,_=self.run_case({'mode':'exact','parameters':{'category':'active'}},extra=True)
        self.assertEqual(len(journal),2)
        self.assertEqual(run['validations'][0]['intent_verdict'],'pass')
        self.assertEqual(run['validations'][1]['intent_verdict'],'not_checked')
        self.assertEqual(run['validations'][1]['contract_verdict'],'pass')
        self.assertEqual(run['status'],'completed')
        self.assertEqual(run['report']['checks_verdict'],'PASS')
        self.assertEqual(run['report']['goal_verdict'],'UNKNOWN')

    def test_global_absence_does_not_finish_on_guessed_filter(self):
        run,journal,_=self.run_case({'mode':'unfiltered'},global_goal=True)
        self.assertEqual(journal,['/records?category=active','/records'])
        self.assertEqual(run['validations'][0]['intent_verdict'],'not_checked')
        self.assertTrue(run['steps'][0]['observation']['data']['verify_unfiltered'])
        self.assertEqual(run['report']['checks_verdict'],'FAIL')
        self.assertEqual(run['report']['goal_verdict'],'FAIL')

    def test_actual_scoped_mismatch_still_fails(self):
        run,_,_=self.run_case({'mode':'exact','parameters':{'category':'active'}},present=True)
        self.assertEqual(run['report']['checks_verdict'],'FAIL')
        self.assertEqual(run['report']['goal_verdict'],'FAIL')

    def test_undeclared_scope_never_forces_global_request(self):
        run,journal,_=self.run_case(None)
        self.assertEqual(journal,['/records?category=active'])
        self.assertEqual(run['status'],'completed')
        self.assertEqual(run['report']['goal_verdict'],'UNKNOWN')

    def test_default_scope_is_not_replaced_by_another_query(self):
        run,_,_=self.run_case(None,extra=True)
        self.assertEqual(run['report']['checks_verdict'],'PASS')
        self.assertEqual(run['report']['goal_verdict'],'UNKNOWN')

    def test_global_absence_passes_only_after_unfiltered_empty_result(self):
        run,journal,_=self.run_case({'mode':'unfiltered'},global_goal=True,empty_global=True)
        self.assertEqual(len(journal),2)
        self.assertEqual(run['validations'][0]['intent_verdict'],'not_checked')
        self.assertEqual(run['report']['checks_verdict'],'PASS')

    def test_nonempty_filtered_response_does_not_decide_global_check(self):
        run,_,_=self.run_case({'mode':'unfiltered'},global_goal=True,present=True)
        self.assertEqual(run['validations'][0]['intent_verdict'],'not_checked')
        self.assertEqual(run['report']['checks_verdict'],'FAIL')

    def test_scope_is_pinned_in_sqlite_and_not_replayed_after_restart(self):
        run,journal,_=self.run_case({'mode':'exact','parameters':{'category':'active'}},history=True)
        self.assertEqual(len(journal),1)
        self.assertEqual(run['report']['checks_verdict'],'PASS')

    def test_invalid_scope_does_not_reach_http(self):
        for scope in [{'mode':'guess'},{'mode':'exact','parameters':{'category':1}},
                      {'mode':'unfiltered','parameters':{'category':'active'}},
                      {'mode':'exact','parameters':{'foreign':'active'}}]:
            with self.subTest(scope=scope),self.assertRaises(ValueError):self.run_case(scope)

    def test_trusted_scope_is_visible_before_the_first_action(self):
        scope={'mode':'exact','parameters':{'category':'active'}}
        _,_,payloads=self.run_case(scope)
        text=next(m['content'] for m in payloads[0]['messages'] if m['content'].startswith('Supported checks: '))
        checks=json.loads(text.removeprefix('Supported checks: '))
        self.assertEqual(checks[0]['selection_scope'],scope)

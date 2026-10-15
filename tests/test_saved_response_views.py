"""Большой ответ доступен по частям без повторного HTTP и утечки secret paths."""

import sys,json,unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.openapi import load_source
from api_agent.policy import ProjectConfig
from api_agent.state import RunState
from demo_api.server import DemoServer
from agent_support import reply,count_test_tokens
from test_catalog_execution import status_condition


class SavedResponseTests(unittest.TestCase):
    def workflow(self,target='http://127.0.0.1:18080'):
        cat=load_source(str(ROOT/'tests/fixtures/demo_openapi.json'))
        cfg=ProjectConfig.create('p',target,cat,mode='READ_ONLY',resettable=True,operations={'getPet':'A'})
        return cat,CatalogWorkflow(cat,cfg,goal='Read saved data',conditions={'getPet':[status_condition(200)]})

    def session(self):
        body=[{'id':i,'name':'Условие '+str(i),'speed':110,'notes':'x'*300} for i in range(30)]
        exchange={'id':'own','run_id':'r','operation_id':'getPet','outcome':'received','response':{'status':200,'json':body},'truncated':False}
        state=RunState('p','r',{})
        return SimpleNamespace(exchanges=[exchange],state=state,run={'run_id':'r','expectations':[],'validations':[]}),exchange

    def test_large_response_keeps_meaningful_preview_and_explicit_incompleteness(self):
        cat,wf=self.workflow();session,exchange=self.session();original=deepcopy(exchange)
        data=wf.observation(session,exchange,{'contract_verdict':'pass','intent_verdict':'pass','task_postcondition':True,'checks':[]})
        self.assertIn('response_json',data)
        self.assertEqual(data['response_json'][0]['name'],'Условие 0')
        self.assertFalse(data['response_body_complete'])
        self.assertEqual(data['response_view']['total'],30)
        self.assertIn('read_response',data['response_read_hint'])
        self.assertEqual(exchange,original)

    def test_saved_page_and_nested_field_have_correct_bounds_and_foreign_id_rejected(self):
        cat,wf=self.workflow();session,exchange=self.session()
        self.assertTrue(hasattr(wf,'read_response'),'Нет чтения сохранённого ответа')
        page=wf.read_response(session,{'exchange_id':'own','offset':16,'limit':2})
        self.assertEqual([v['id'] for v in page['value']],[16,17])
        self.assertEqual(page['next_offset'],18)
        self.assertFalse(page['complete'])
        field=wf.read_response(session,{'exchange_id':'own','json_pointer':'/16/notes'})
        self.assertEqual(field['value'],'x'*300)
        self.assertTrue(field['complete'])
        for args in [{'exchange_id':'foreign'}, {'exchange_id':'own','json_pointer':'/00'}, {'exchange_id':'own','offset':-1}]:
            with self.subTest(args=args),self.assertRaises(ValueError):wf.read_response(session,args)

    def test_secret_branch_is_redacted_before_pointer_selection(self):
        cat,wf=self.workflow();session,exchange=self.session()
        self.assertTrue(hasattr(wf,'read_response'))
        session.state=RunState('p','r',{'getPet':{'/token':{'type':'string','secret':True}}})
        exchange['response']['json']={'token':{'private-key':'private-value'},'name':'visible'}
        page=wf.read_response(session,{'exchange_id':'own','json_pointer':'/token'})
        self.assertEqual(page['value'],'[REDACTED]')
        self.assertNotIn('private-',json.dumps(page))
        with self.assertRaises(ValueError):wf.read_response(session,{'exchange_id':'own','json_pointer':'/token/private-key'})

    def test_valid_json_null_is_distinct_from_an_unavailable_response(self):
        cat,wf=self.workflow();session,exchange=self.session()
        exchange['response'].update(json=None,json_valid=True)
        data=wf.observation(session,exchange,{'contract_verdict':'pass','intent_verdict':'pass','task_postcondition':True,'checks':[]})
        self.assertIn('response_json',data)
        self.assertIsNone(data['response_json'])
        page=wf.read_response(session,{'exchange_id':'own'})
        self.assertIsNone(page['value'])
        self.assertTrue(page['complete'])

    def test_runtime_read_response_does_not_issue_second_http_request(self):
        with DemoServer() as server:
            cat,wf=self.workflow(server.target)
            self.assertIn('read_response',[t['function']['name'] for t in wf.registry(cat)])
            def model(payload):
                obs=[json.loads(m['content']) for m in payload['messages'] if m['role']=='tool']
                if not obs:return reply('execute_operation',{'operation_id':'getPet'})
                if len(obs)==1:return reply('read_response',{'exchange_id':obs[0]['data']['exchange_id']})
                return reply('finish_run',{'summary':'Read own response','evidence_ids':[obs[0]['data']['exchange_id']]})
            run=StartAgentRun(server.target,cat,workflow=wf,token_counter=count_test_tokens).start('p','s','m',wf.goal,model)
            self.assertEqual(run['status'],'completed')
            self.assertEqual(len(server.journal),1)
            self.assertEqual(run['steps'][1]['observation']['data']['value']['id'],1)


if __name__=='__main__':unittest.main()

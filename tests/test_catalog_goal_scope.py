"""Полнота HTTP и полнота цели различаются; пустой фильтр не доказывает отсутствие."""
import unittest
from pathlib import Path
from types import SimpleNamespace
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from api_agent.agent import StartAgentRun
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig
from api_agent.state import RunState
from demo_api.server import DemoServer
from agent_support import count_test_tokens
import test_catalog_execution as support


class GoalScopeTests(unittest.TestCase):
    def test_cancelled_action_is_reported_as_cancelled_not_verified_success(self):
        with DemoServer() as server:
            def decline(preview):
                return {'action_hash':preview['action_hash'],'config_version':preview['config_version'],'approved':False}
            cat,_,wf,create,read=support.GenericExecutionTests().workflow(server,mode='SAFE',approval=decline)
            run=StartAgentRun(server.target,cat,workflow=wf,token_counter=count_test_tokens).start('p','s','m',wf.goal,support.GenericModel(create,read))
            self.assertEqual(run['status'],'blocked')
            self.assertEqual(run['stop_reason'],'approval_rejected')
            self.assertIn('отмен',run['report']['summary'].lower())
            self.assertEqual(run['report']['checks_verdict'],'UNKNOWN')
            self.assertEqual(server.connections,0)

    def test_supported_checks_do_not_silently_verify_free_goal(self):
        with DemoServer() as server:
            cat,_,wf,create,read=support.GenericExecutionTests().workflow(server)
            run=StartAgentRun(server.target,cat,workflow=wf,token_counter=count_test_tokens).start('p','s','m',wf.goal,support.GenericModel(create,read))
            self.assertIn('goal_verdict',run['report'])
            self.assertEqual(run['report']['goal_verdict'],'UNKNOWN')
            self.assertEqual(run['report']['checks_verdict'],'PASS')

    def test_failed_binding_falsifies_condition_even_if_coverage_is_partial(self):
        with DemoServer() as server:
            server.mode='state_id_mismatch'
            cat,_,wf,create,read=support.GenericExecutionTests().workflow(server)
            wf.binding_rules[read]['order_id']['response_path']='/id'
            run=StartAgentRun(server.target,cat,workflow=wf,token_counter=count_test_tokens).start('p','s','m',wf.goal,support.GenericModel(create,read))
            self.assertIn('goal_verdict',run['report'])
            self.assertEqual(run['report']['goal_verdict'],'FAIL')

    def test_empty_optional_query_requests_bounded_unfiltered_verification(self):
        operation={'operationId':'browse','parameters':[{'name':'category','in':'query','required':False,'schema':{'type':'string'}}],
            'responses':{'200':{'description':'ok','content':{'application/json':{'schema':{'type':'array','items':{'type':'object'}}}}}}}
        cat=parse_document({'openapi':'3.0.3','info':{'title':'arbitrary','version':'1'},'paths':{'/entries':{'get':operation}}})
        cfg=ProjectConfig.create('p','http://127.0.0.1:18080',cat,mode='READ_ONLY',resettable=True,operations={'browse':'A'})
        wf=CatalogWorkflow(cat,cfg,goal='Покажи записи',conditions={'browse':[support.status_condition(200)]})
        validation={'operation_id':'browse','contract_verdict':'pass','intent_verdict':'pass','task_postcondition':True}
        exchange={'id':'e','operation_id':'browse','outcome':'received','request':{'url':'http://127.0.0.1:18080/entries?category=guessed'},'response':{'status':200,'json':[],'json_valid':True}}
        run={'run_id':'r','expectations':wf.prepared(cat,'m',wf.goal)['browse'],'validations':[validation],'steps':[],'exchanges':[exchange]}
        session=SimpleNamespace(state=RunState('p','r',{}),run=run,exchanges=[exchange])
        data=wf.observation(session,exchange,validation)
        self.assertTrue(data.get('verify_unfiltered'),'Пустая фильтрованная выборка допускает преждевременный finish')
        self.assertFalse(data.get('checks_complete',False))
        run['steps']=[{'action':{'name':'execute_operation','arguments':{'operation_id':'browse'}},'observation':{'data':data}}]
        tools=wf.phase_registry(run,wf.registry(cat))
        execute=next(t['function']['parameters'] for t in tools if t['function']['name']=='execute_operation')
        self.assertNotIn('parameters',execute['properties'])

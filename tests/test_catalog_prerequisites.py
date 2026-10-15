"""Диагностика входов и выбор фаз предотвращают пустые поиски и угадывание auth."""

import sys,json,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.openapi import load_source
from api_agent.policy import ProjectConfig
from demo_api.server import DemoServer
from agent_support import reply,count_test_tokens
from test_catalog_execution import GenericExecutionTests,status_condition


class PrerequisiteTests(unittest.TestCase):
    def auth_workflow(self,server):
        cat=load_source(str(ROOT/'tests/fixtures/auth_openapi.json'))
        cfg=ProjectConfig.create('p',server.target,cat,mode='TEST',resettable=True,operations={'loginUser':'C','readProfile':'A'})
        wf=CatalogWorkflow(cat,cfg,goal='Read profile',conditions={'readProfile':[status_condition(200)]},extraction={'loginUser':{'/token':{'type':'string','secret':True}}},auth={'login_operation':'loginUser','token_path':'/token','body':{'login':'fixture','password':'fixture-password'}})
        return cat,wf

    def test_login_inspection_explains_application_body_without_credentials(self):
        with DemoServer() as server:
            cat,wf=self.auth_workflow(server)
            inspected=wf.inspect(cat,'loginUser')
            self.assertEqual(inspected['input_requirements']['body'],'application_supplied')
            self.assertEqual(inspected['input_readiness'],'ready')
            self.assertNotIn('fixture-password',json.dumps(inspected))
            profile=wf.inspect(cat,'readProfile')
            self.assertEqual(profile['input_requirements']['authentication'],'credential_ref_required')
            self.assertEqual(profile['input_requirements']['credential_source'],'loginUser')
            self.assertEqual(server.connections,0)

    def test_missing_auth_ref_offers_login_source_and_never_connects(self):
        with DemoServer() as server:
            cat,wf=self.auth_workflow(server)
            run=StartAgentRun(server.target,cat,workflow=wf,token_counter=count_test_tokens).start('p','s','m',wf.goal,lambda p:reply('execute_operation',{'operation_id':'readProfile'}))
            hint=run['steps'][0]['observation']['error']['hint']
            self.assertIn('credential_ref',hint)
            self.assertIn('loginUser',hint)
            self.assertNotIn('fixture-password',json.dumps(run))
            self.assertEqual(server.connections,0)

    def test_body_fields_in_parameters_produce_specific_repair(self):
        with DemoServer() as server:
            cat,cfg,wf,create,read=GenericExecutionTests().workflow(server)
            run=StartAgentRun(server.target,cat,workflow=wf,token_counter=count_test_tokens).start('p','s','m',wf.goal,lambda p:reply('execute_operation',{'operation_id':create,'parameters':{'name':'A'}}))
            hint=run['steps'][0]['observation']['error']['hint']
            self.assertIn('body',hint)
            self.assertIn('name',hint)
            self.assertEqual(server.connections,0)

    def test_known_start_cards_restrict_phase_without_changing_tool_definitions(self):
        with DemoServer() as server:
            cat,cfg,wf,create,read=GenericExecutionTests().workflow(server)
            seen=[]
            def model(payload):
                seen.append(payload)
                observations=[json.loads(h['content']) for h in payload['messages'] if h['role']=='tool']
                if not observations:return reply('inspect_operation',{'operation_id':create})
                if len(observations)==1:return reply('execute_operation',{'operation_id':create,'body':{'name':'A'}})
                if len(observations)==2:return reply('execute_operation',{'operation_id':read,'bindings':{'order_id':observations[-1]['data']['runtime_values'][0]['ref']}})
                return reply('finish_run',{'summary':'Checked','evidence_ids':observations[-1]['data']['finish_evidence_ids']})
            run=StartAgentRun(server.target,cat,workflow=wf,token_counter=count_test_tokens).start('p','s','m',wf.goal,model)
            self.assertEqual(run['status'],'completed')
            self.assertEqual([p['tool_choice']['function']['name'] for p in seen],['inspect_operation','execute_operation','execute_operation','finish_run'])
            definitions={t['function']['name']:t for t in wf.registry(cat)}
            self.assertTrue(all(t==definitions[t['function']['name']] for p in seen for t in p['tools']))
            self.assertNotIn('search_operations',[t['function']['name'] for t in seen[0]['tools']])
            self.assertEqual(run['model_calls'],4)

    def test_no_required_inputs_or_auth_skips_unnecessary_inspection(self):
        from test_catalog_readiness import hundred_catalog
        cat=hundred_catalog()
        cfg=ProjectConfig.create('p','http://127.0.0.1:18080',cat,mode='READ_ONLY',resettable=True,operations={'z_pet_observation':'A'})
        wf=CatalogWorkflow(cat,cfg,goal='Observe',conditions={'z_pet_observation':[status_condition(200)]})
        run={'selection_manifest':{'selected_ids':['z_pet_observation']}}
        self.assertEqual(wf.next_tool(run)['function']['name'],'execute_operation')

    def test_complete_checks_with_partial_body_keep_saved_read_and_finish(self):
        with DemoServer() as server:
            cat,cfg,wf,create,read=GenericExecutionTests().workflow(server)
            run={'steps':[{'observation':{'data':{'checks_complete':True,'response_body_complete':False}}}]}
            self.assertTrue(hasattr(wf,'phase_registry'))
            names={t['function']['name'] for t in wf.phase_registry(run,wf.registry(cat))}
            self.assertEqual(names,{'read_response','finish_run'})


if __name__=='__main__':unittest.main()

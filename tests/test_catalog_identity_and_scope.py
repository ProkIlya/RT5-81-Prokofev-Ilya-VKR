"""Публичная идентичность задаётся явно, права агента не объявляют security API."""
import json
import tempfile
import unittest
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'tests')]
from api_agent.agent.catalog import CatalogWorkflow, CatalogLimits
from api_agent.openapi import load_source
from api_agent.policy import ProjectConfig
from api_agent.agent import StartAgentRun
from demo_api.server import DemoServer
from agent_support import reply, count_test_tokens
from test_catalog_execution import status_condition


class IdentityAndScopeTests(unittest.TestCase):
    def workflow(self, server, public=False):
        cat = load_source(str(ROOT/'tests/fixtures/auth_openapi.json'))
        cfg = ProjectConfig.create('p',server.target,cat,mode='TEST',resettable=True,operations={'loginUser':'C','readProfile':'A'})
        auth = dict(login_operation='loginUser',token_path='/token',body={'login':'fixture','password':'fixture-password'})
        if public:
            auth['public_body_fields'] = ['login']
        return cat, CatalogWorkflow(cat,cfg,goal='Покажи мой логин',conditions={'readProfile':[status_condition(200)]},
            extraction={'loginUser':{'/token':{'type':'string','secret':True}}},auth=auth)

    def test_explicit_public_identity_reaches_model_without_credentials(self):
        with DemoServer() as server:
            cat,wf = self.workflow(server,True)
            seen=[]
            def model(payload):
                serialized=json.dumps(payload)
                self.assertNotIn('fixture-password',serialized)
                self.assertNotIn(server.auth_token,serialized)
                obs=[json.loads(m['content'])['data'] for m in payload['messages'] if m['role']=='tool']
                if not obs: return reply('execute_operation',{'operation_id':'loginUser'})
                if len(obs)==1: return reply('execute_operation',{'operation_id':'readProfile','credential_ref':obs[0]['runtime_values'][0]['ref']})
                seen.append(app.safe({'login':'fixture','password':'fixture-password'}))
                return reply('finish_run',{'summary':'Профиль получен','evidence_ids':obs[-1]['finish_evidence_ids']})
            app=StartAgentRun(server.target,cat,workflow=wf,token_counter=count_test_tokens)
            run=app.start('p','s','m',wf.goal,model)
            self.assertEqual(run['status'],'completed')
            self.assertEqual(seen,[{'login':'fixture','password':'[REDACTED]'}])
            self.assertNotIn('fixture-password',json.dumps(run))

    def test_default_identity_remains_private(self):
        with DemoServer() as server:
            _,wf=self.workflow(server)
            self.assertIn('fixture',wf.secrets())

    def test_card_separates_api_security_and_execution_permission(self):
        with DemoServer() as server:
            _,wf=self.workflow(server)
            card=wf._discovery.card('readProfile','ALLOW')
            self.assertIn('api_security',card)
            self.assertEqual(card['agent_permission'],'ALLOW')
            self.assertTrue(card['api_security'])
            self.assertNotIn('auth_required',card)

    def test_catalog_total_budget_does_not_expand_context(self):
        limits=CatalogLimits()
        self.assertEqual(limits.max_context_tokens,8192)
        self.assertEqual(limits.max_tokens_total,32768)

    def test_history_pins_public_classification_without_auth_body(self):
        from api_agent.storage import SQLiteHistory
        with DemoServer() as server,tempfile.TemporaryDirectory() as directory:
            cat,wf=self.workflow(server,True)
            with SQLiteHistory(Path(directory)/'history.sqlite3') as store:
                app=StartAgentRun(server.target,cat,workflow=wf,history_store=store,token_counter=count_test_tokens)
                def model(payload):
                    return reply('finish_run',{'summary':'Не выполнено','evidence_ids':[]})
                run=app.start('p','s','m',wf.goal,model)
                snapshot=store.snapshot('p','s',run['run_id'])
                self.assertEqual(snapshot['metadata']['auth']['public_body_fields'],['login'])
                self.assertNotIn('body',snapshot['metadata']['auth'])
                self.assertNotIn('fixture-password',json.dumps(snapshot))

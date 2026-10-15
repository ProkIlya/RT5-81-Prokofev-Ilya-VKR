"""Native grammar выводится из выбранной операции и существующих Run refs."""
import json
import unittest
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from api_agent.agent import StartAgentRun
from api_agent.validation import schema_issues
from agent_support import count_test_tokens
from demo_api.server import DemoServer
import test_catalog_execution as helpers
import test_catalog_identity_and_scope as identity


class ExactInputTests(unittest.TestCase):
    def test_two_renamed_operations_get_typed_body_and_scoped_binding_enum(self):
        for prefix in ('gamma','delta'):
            with self.subTest(prefix=prefix),DemoServer() as server:
                cat,_,wf,create,read=helpers.GenericExecutionTests().workflow(server,prefix)
                schemas=[];base=helpers.GenericModel(create,read)
                def model(payload):
                    schemas.append(next(t['function']['parameters'] for t in payload['tools'] if t['function']['name']=='execute_operation') if len(schemas)<2 else {})
                    return base(payload)
                run=StartAgentRun(server.target,cat,workflow=wf,token_counter=count_test_tokens).start('p','s','m',wf.goal,model)
                self.assertEqual(run['status'],'completed')
                self.assertIn('name',schemas[0]['properties']['body'].get('properties',{}),'Тело остаётся нетипизированным object')
                self.assertTrue(schema_issues({'operation_id':create,'body':{'name':99}},schemas[0]))
                self.assertNotIn('body',schemas[1]['properties'])
                binding=schemas[1]['properties']['bindings']['properties']['order_id']
                self.assertEqual(len(binding['enum']),1)
                self.assertTrue(schema_issues({'operation_id':read,'bindings':{'order_id':'invented'}},schemas[1]))

    def test_missing_auth_only_offers_login_then_actual_credential_reference(self):
        with DemoServer() as server:
            cat,wf=identity.IdentityAndScopeTests().workflow(server)
            first=wf.phase_registry({'steps':[],'exchanges':[]},wf.registry(cat))
            schema=next(t['function']['parameters'] for t in first if t['function']['name']=='execute_operation')
            self.assertEqual(schema['properties']['operation_id']['enum'],['loginUser'])
            self.assertNotIn('body',schema['properties'])
            run={'steps':[{'action':{'name':'execute_operation','arguments':{'operation_id':'loginUser'}},'observation':{'data':{'runtime_values':[{'ref':'issued','secret':True,'operation_id':'loginUser','path':'/token'}]}}}], 'exchanges':[{'operation_id':'loginUser'}]}
            schema=next(t['function']['parameters'] for t in wf.phase_registry(run,wf.registry(cat)) if t['function']['name']=='execute_operation')
            self.assertEqual(schema['properties']['operation_id']['enum'],['readProfile'])
            self.assertEqual(schema['properties']['credential_ref']['enum'],['issued'])
            self.assertIn('credential_ref',schema['required'])

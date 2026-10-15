"""HTTP200 не доказывает наличие требуемых объектов в JSON произвольного API."""

import sys,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from api_agent.openapi import parse_document
from api_agent.validation import prepare_expectations,validate_exchange
from test_response_validation import user_condition


class CollectionExpectationTests(unittest.TestCase):
    def catalog(self):
        return parse_document({'openapi':'3.0.3','info':{'title':'collection','version':'1'},'paths':{'/specimens':{'get':{'operationId':'observe','responses':{'200':{'description':'ok','content':{'application/json':{'schema':{'type':'array','items':{'type':'object','properties':{'label':{'type':'string'},'size':{'type':'number'}},'required':['label','size']}}}}}}}}}})

    def condition(self,mode='contains'):
        return user_condition('json_collection_matches',{'path':'','rows':[{'/label':'A','/size':2},{'/label':'B','/size':3}],'mode':mode})

    def evaluate(self,body,condition=None,outcome='received'):
        cat=self.catalog();checks=prepare_expectations(cat,[condition or self.condition()],'m','Find specimens A and B',operation_id='observe')
        return validate_exchange(cat,{'id':'e','run_id':'r','operation_id':'observe','outcome':outcome,'response':{'status':200,'content_type':'application/json','json':body,'json_valid':True}},checks)

    def test_matching_objects_are_order_independent_and_allow_extra_rows(self):
        result=self.evaluate([{'label':'B','size':3},{'label':'other','size':9},{'label':'A','size':2}])
        self.assertEqual(result['intent_verdict'],'pass')

    def test_empty_wrong_value_duplicate_and_boolean_number_do_not_pass_with_200(self):
        for body in [[],[{'label':'A','size':2},{'label':'B','size':99}],[{'label':'A','size':2},{'label':'A','size':2},{'label':'B','size':3}],[{'label':'A','size':True},{'label':'B','size':3}]]:
            with self.subTest(body=body):self.assertEqual(self.evaluate(body)['intent_verdict'],'fail')

    def test_exact_mode_rejects_unexpected_objects(self):
        self.assertEqual(self.evaluate([{'label':'A','size':2},{'label':'B','size':3},{'label':'extra','size':9}],self.condition('exact'))['intent_verdict'],'fail')

    def test_absence_is_checked_against_the_complete_collection(self):
        condition=user_condition('json_collection_matches',{'path':'','rows':[{'/label':'missing'}],'mode':'absent'})
        self.assertEqual(self.evaluate([{'label':'A','size':2}],condition)['intent_verdict'],'pass')
        self.assertEqual(self.evaluate([{'label':'missing','size':2}],condition)['intent_verdict'],'fail')

    def test_incomplete_response_cannot_establish_missing_objects(self):
        self.assertEqual(self.evaluate([],outcome='response_limit')['intent_verdict'],'not_checked')

    def test_business_values_cannot_claim_contract_basis_or_inject_expression(self):
        cat=self.catalog();condition=self.condition();condition['source']='CONTRACT'
        condition['basis']={'pointer':'/responses/200/content/application~1json/schema','operation_hash':cat.operation_hashes['observe'],'spec_hash':cat.spec_hash}
        with self.assertRaises(ValueError):prepare_expectations(cat,[condition],'m','Find',operation_id='observe')
        condition=self.condition();condition['parameters']['code']='eval()'
        with self.assertRaises(ValueError):prepare_expectations(cat,[condition],'m','Find',operation_id='observe')


if __name__=='__main__':unittest.main()

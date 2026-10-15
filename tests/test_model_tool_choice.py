"""Named tool_choice совместим с locked runtime без расхождения tokenizer/generation."""

import unittest,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from api_agent.agent.model import LocalModel


class ToolChoiceCompatibilityTests(unittest.TestCase):
    def payload(self):
        return {'messages':[{'role':'user','content':'Choose'}], 'tools':[
            {'type':'function','function':{'name':n,'parameters':{'type':'object'}}} for n in ['inspect_operation','execute_operation']],
            'tool_choice':{'type':'function','function':{'name':'inspect_operation'}}}

    def test_named_choice_becomes_one_tool_and_required_without_mutating_input(self):
        payload=self.payload();model=LocalModel('http://127.0.0.1:18081')
        self.assertTrue(hasattr(model,'prepare_payload'))
        prepared=model.prepare_payload(payload)
        self.assertEqual(prepared['tool_choice'],'required')
        self.assertEqual([t['function']['name'] for t in prepared['tools']],['inspect_operation'])
        self.assertIn('inspect_operation',prepared['messages'][-1]['content'])
        self.assertIn('later turns',prepared['messages'][-1]['content'])
        self.assertEqual(len(payload['tools']),2)
        self.assertIsInstance(payload['tool_choice'],dict)
        self.assertEqual(model.prepare_payload(prepared),prepared)

    def test_unknown_named_choice_is_rejected_before_network(self):
        model=LocalModel('http://127.0.0.1:18081');self.assertTrue(hasattr(model,'prepare_payload'))
        payload=self.payload();payload['tool_choice']['function']['name']='foreign'
        with self.assertRaises(ValueError):model.prepare_payload(payload)

    def test_tokenizer_receives_exact_effective_tool_shape(self):
        model=LocalModel('http://127.0.0.1:18081');captured=[]
        def fake(path,payload):
            captured.append((path,payload))
            return {'prompt':'rendered'} if path=='/apply-template' else {'tokens':[1,2]}
        model._json_request=fake
        self.assertEqual(model.count_tokens(self.payload()),2)
        self.assertEqual(captured[0][1]['tool_choice'],'required')
        self.assertEqual([t['function']['name'] for t in captured[0][1]['tools']],['inspect_operation'])


if __name__=='__main__':unittest.main()

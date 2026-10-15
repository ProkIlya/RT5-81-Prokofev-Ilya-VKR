"""Изолированный reviewer советует, но не меняет проверенные результаты и HTTP."""
import json
import importlib.util
import unittest
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
from api_agent.agent import StartAgentRun
from api_agent.agent.catalog import CatalogLimits
from agent_support import reply
from demo_api.server import DemoServer
import test_catalog_execution as support


class SummaryReviewTests(unittest.TestCase):
    def run_case(self, response, *, budget=32768):
        self.assertIsNotNone(importlib.util.find_spec('api_agent.agent.summary_review'),'Нет изолированного reviewer')
        from api_agent.agent.summary_review import FinalSummaryReviewer
        with DemoServer() as server:
            server.mode='state_id_mismatch'
            cat,_,wf,create,read=support.GenericExecutionTests().workflow(server)
            wf.binding_rules[read]['order_id']['response_path']='/id'
            primary=support.GenericModel(create,read)
            requests=[]
            def reviewer(payload):
                requests.append(payload)
                if isinstance(response,Exception): raise response
                return reply('review_summary',response)
            app=StartAgentRun(server.target,cat,workflow=wf,limits=CatalogLimits(max_tokens_total=budget),token_counter=lambda payload:100,
                summary_reviewer=FinalSummaryReviewer(reviewer))
            run=app.start('p','s','m',wf.goal,primary)
            duplicate=app.start('p','s','m',wf.goal,primary)
            self.assertEqual(len(server.journal),2)
            self.assertEqual(duplicate['run_id'],run['run_id'])
            return run,requests

    def test_review_isolated_from_agent_history_and_cannot_override_mismatch(self):
        run,requests=self.run_case({'model_verdict':'PASS'})
        self.assertEqual(len(requests),1)
        self.assertEqual([m['role'] for m in requests[0]['messages']],['system','user'])
        self.assertEqual([t['function']['name'] for t in requests[0]['tools']],['review_summary'])
        self.assertEqual(run['report']['intent_verdict'],'fail')
        self.assertEqual(run['summary_review']['assessment']['status'],'contradiction')
        self.assertEqual(run['summary_review']['assessment']['verification'],'advisory')
        self.assertEqual(run['review_model_calls'],1)

    def test_review_failure_does_not_repeat_http_or_erase_program_result(self):
        run,requests=self.run_case(RuntimeError('private-exception-text'))
        self.assertEqual(len(requests),1)
        self.assertEqual(run['report']['intent_verdict'],'fail')
        self.assertEqual(run['summary_review']['assessment']['status'],'unavailable')
        self.assertNotIn('private-exception-text',json.dumps(run))

    def test_review_respects_total_budget_and_does_not_call_when_exhausted(self):
        run,requests=self.run_case({'model_verdict':'FAIL'},budget=2048)
        self.assertEqual(run['status'],'completed')
        self.assertEqual(requests,[])
        self.assertEqual(run['summary_review']['assessment']['status'],'budget_exhausted')

    def test_checker_uses_only_bounded_classifications_without_free_prose(self):
        run,requests=self.run_case({'model_verdict':'FAIL'})
        self.assertEqual(len(requests),1)
        self.assertEqual(run['summary_review']['assessment']['status'],'consistent')
        properties=requests[0]['tools'][0]['function']['parameters']['properties']
        self.assertEqual(set(properties),{'model_verdict'})
        self.assertTrue(all('enum' in prop for prop in properties.values()))

    def test_equal_verdicts_cannot_be_reported_as_contradiction(self):
        run,requests=self.run_case({'model_verdict':'FAIL'})
        self.assertEqual(run['summary_review']['assessment']['status'],'consistent')

    def test_claim_extraction_is_blind_to_program_verdict_and_raw_evidence(self):
        run,requests=self.run_case({'model_verdict':'PASS'})
        data=json.loads(requests[0]['messages'][1]['content'])
        self.assertEqual(set(data),{'primary_summary','task_goal'})
        self.assertEqual(data['task_goal'],run['goal'])
        self.assertNotIn('program_verdict',data)

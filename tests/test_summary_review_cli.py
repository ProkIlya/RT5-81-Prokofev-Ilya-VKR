"""CLI включает reviewer явно; история после рестарта не вызывает его повторно."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT)]
from agent_support import ObserverModel,reply,count_test_tokens
from demo_api.server import DemoServer


class ReviewCliTests(unittest.TestCase):
    def test_review_flag_persists_result_and_read_only_delivery_never_rechecks(self):
        from api_agent.agent.__main__ import main
        with DemoServer() as server,tempfile.TemporaryDirectory() as directory:
            observer=ObserverModel();review_calls=[]
            def model(payload):
                if payload['tools'][0]['function']['name']=='review_summary':
                    review_calls.append(payload)
                    return reply('review_summary',{'model_verdict':'PASS'})
                return observer(payload)
            args=['Check pet','--target',server.target,'--spec',str(ROOT/'tests/fixtures/demo_openapi.json'),'--evidence-dir',directory,
                '--project-id','p','--session-id','s','--message-id','m','--review-summary']
            output=io.StringIO()
            try:
                with contextlib.redirect_stdout(output):code=main(args,model=model,token_counter=count_test_tokens)
            except SystemExit:
                self.fail('CLI не поддерживает изолированную проверку объяснения')
            self.assertEqual(code,0)
            self.assertEqual(len(review_calls),1)
            result=json.loads(output.getvalue())
            self.assertEqual(result['summary_review']['status'],'consistent')
            self.assertEqual(result['goal_verdict'],'UNKNOWN')
            def forbidden(payload):self.fail('Повторное получение ответа вызвало LLM')
            with contextlib.redirect_stdout(io.StringIO()):self.assertEqual(main(args,model=forbidden,token_counter=count_test_tokens),0)
            self.assertEqual(server.connections,1)

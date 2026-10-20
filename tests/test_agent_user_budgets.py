"""Пользовательский HTTP budget ограничивает реальную сеть и переживает restart."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.agent.catalog import CatalogWorkflow,CatalogLimits
from api_agent.agent.__main__ import main
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig
from agent_support import reply,count_test_tokens,ObserverModel

@contextlib.contextmanager
def checks_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            self.server.paths.append(self.path)
            body=b'{"id":1,"name":"Murka"}'
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
    with ThreadingHTTPServer(('127.0.0.1',0),Handler) as s:
        s.paths=[];s.target=f'http://127.0.0.1:{s.server_port}'
        thread=threading.Thread(target=s.serve_forever,daemon=True);thread.start()
        try:yield s
        finally:s.shutdown();thread.join()

def status():
    return {'predicate':'status_in','parameters':{'values':[200]},'intent':'positive','preconditions':'verified'}

def many_checks(count):
    document=json.loads((ROOT/'tests/fixtures/demo_openapi.json').read_text(encoding='utf-8'))
    operation=document['paths']['/pets/1']['get']
    document['paths']={f'/checks/{n}':{'get':{**operation,'operationId':f'check{n}'}} for n in range(1,count+1)}
    return parse_document(document)

class UserBudgetTests(unittest.TestCase):
    def run_limit(self,limit=None):
        with checks_server() as server:
            catalog=many_checks(13)
            cfg=ProjectConfig.create('p',server.target,catalog,mode='READ_ONLY',resettable=True,operations={f'check{n}':'A' for n in range(1,14)})
            goal='Проверь тринадцать независимых ответов'
            workflow=CatalogWorkflow(catalog,cfg,goal=goal,conditions={f'check{n}':[status()] for n in range(1,14)})
            calls=0
            def model(payload):
                nonlocal calls
                calls+=1
                return reply('execute_operation',{'operation_id':f'check{calls}'})
            try:limits=CatalogLimits(**({} if limit is None else {'max_requests':limit}))
            except ValueError:self.fail('Пользовательский лимит больше четырёх отклонён до запуска')
            run=StartAgentRun(server.target,catalog,workflow=workflow,limits=limits,token_counter=count_test_tokens).start('p','s','m',goal,model)
            want=10 if limit is None else limit
            self.assertEqual(server.paths,[f'/checks/{n}' for n in range(1,want+1)])
            self.assertEqual(run['requests'],want)
            self.assertEqual(run['stop_reason'],'request_limit')
            self.assertEqual(run['malformed_calls'],0)
            self.assertEqual(run['status'],'partial')

    def test_default_allows_ten_http_and_denies_eleventh(self):self.run_limit()
    def test_smaller_user_budget_stops_network_without_malformed_retries(self):self.run_limit(5)
    def test_user_can_raise_budget_above_ten(self):self.run_limit(12)

    def test_pilot_chain_also_reports_http_exhaustion_without_malformed_retry(self):
        from api_agent.agent.chains import ChainLimits,CreateRead
        from demo_api.server import DemoServer
        from test_runtime_chain import contract,CreateReadModel
        with DemoServer() as server:
            run=StartAgentRun(server.target,contract(),workflow=CreateRead(),limits=ChainLimits(max_requests=1),token_counter=count_test_tokens).start('p','s','m','Create and read order',CreateReadModel())
            self.assertEqual(len(server.journal),1)
            self.assertEqual(run['stop_reason'],'request_limit')
            self.assertEqual(run['malformed_calls'],0)

    def test_cli_budgets_are_saved_and_duplicate_keeps_original_limits(self):
        with checks_server() as server,tempfile.TemporaryDirectory() as temporary:
            d=Path(temporary)
            document=json.loads((ROOT/'tests/fixtures/demo_openapi.json').read_text(encoding='utf-8'))
            (d/'openapi.json').write_text(json.dumps(document),encoding='utf-8')
            cat=parse_document(document)
            ProjectConfig.create('p',server.target,cat,mode='READ_ONLY',resettable=True,operations={'getPet':'A'}).save(d/'project.json')
            (d/'checks.json').write_text(json.dumps({'getPet':[status()]}),encoding='utf-8')
            args=['Check pet','--target',server.target,'--spec',str(d/'openapi.json'),'--project-config',str(d/'project.json'),'--goal-checks',str(d/'checks.json'),'--evidence-dir',str(d/'runs'),'--message-id','m','--max-requests','12','--max-steps','30','--max-seconds','300','--max-tokens-total','65536']
            try:
                with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
                    code=main(args,model=ObserverModel(),token_counter=count_test_tokens)
            except SystemExit:self.fail('CLI не принимает пользовательские budgets')
            self.assertEqual(code,0)
            trace=json.loads(next((d/'runs').glob('*.json')).read_text(encoding='utf-8'))
            self.assertEqual({k:trace['limits'][k] for k in ['max_requests','max_steps','max_seconds','max_tokens_total']},{'max_requests':12,'max_steps':30,'max_seconds':300,'max_tokens_total':65536})
            def forbidden(payload):self.fail('Повторная доставка запустила модель')
            args[args.index('--max-requests')+1]='2'
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(args,model=forbidden,token_counter=count_test_tokens),0)
            self.assertEqual(server.paths,['/pets/1'])
            saved=json.loads(next((d/'runs').glob('*.json')).read_text(encoding='utf-8'))
            self.assertEqual(saved['limits']['max_requests'],12)

    def test_nonpositive_budgets_are_rejected_before_model_or_http(self):
        for kwargs in [{'max_requests':0},{'max_requests':True},{'max_steps':0},{'max_seconds':float('nan')},{'max_tokens_total':0}]:
            with self.subTest(kwargs=kwargs),self.assertRaises(ValueError):CatalogLimits(**kwargs)

if __name__=='__main__':unittest.main()

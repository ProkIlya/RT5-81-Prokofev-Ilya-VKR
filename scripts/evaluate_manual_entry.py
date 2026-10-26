"""S10 live smoke: настоящая локальная LLM/worker/HTTP, независимое состояние."""
from pathlib import Path
import hashlib
import json
import os
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT)]
from api_agent.agent.model import LocalModel
from api_agent.goals.entry.manual_application import ManualApplication
from api_agent.storage import SQLiteHistory
from api_agent.agent.runtime import WorkspaceOptions
from api_agent.workspace.sdk import build_sdk
from selected_support import DeviceServer,catalog_config,card,configuration,saved_run
from manual_live_order import feedback_order


def main(output):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    source={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'src').rglob('*') if p.is_file() and p.suffix in {'.py','.sql'}}
    cases=[('partial_pass',configuration([card(operation_id='createDevice',statuses=[201],assertions=[])],
        goal='Создай новый прибор со значением57. В первом execute_code только создай прибор, сохрани полный ответ в переменной и заверши блок, чтобы получить feedback проверок. Не печатай ответ. После получения feedback отдельным следующим блоком установи у того же прибора значение80 и прочитай его. Для ID используй поле id из body сохранённого ответа создания, не придумывай ID и не создавай прибор повторно. PASS первой проверки не завершает поручение. Покажи ID и фактическое значение.')),
        ('independent_fail',configuration([card(assertions=[{'predicate':'equals','path':'/value','value':999}])],
        goal='Выполни два последовательных блока execute_code, затем finish_run. В первом блоке только прочитай прибор42 и получи feedback проверки значения999. Если значение отличается, сохрани FAIL. Во втором блоке после feedback независимо от FAIL создай новый прибор со значением80 и сохрани ответ создания в отдельной переменной. После успешного создания заверши Run через finish_run, сообщи несоответствие и ID созданного прибора из body ответа. Первый прибор не изменяй. FAIL проверки не отменяет обязательное независимое создание и не требует добиваться PASS.')),
        ('covered',configuration([card(assertions=[{'predicate':'equals','path':'/value','value':57}])],
        goal='Прочитай прибор42, проверь значение57 и покажи ID и значение. Ничего не изменяй.')),
        ('research',configuration([],mode='research',goal='Создай новый прибор со значением80. Прочитай созданный прибор и покажи его ID и значение.')),
        ('attachment',configuration(goal='У прикреплённого прибора установи значение80. Прочитай его и покажи ID и фактическое значение. Другой прибор не создавай.'))]
    results=[]
    for name,document in cases:
        directory=output/name;directory.mkdir()
        with DeviceServer() as server:
            catalog,config=catalog_config(server.target,project='live-'+name)
            model=LocalModel('http://127.0.0.1:18081')
            with SQLiteHistory(directory/'history.sqlite3') as history:
                setup_http=0
                if name=='attachment':
                    old=saved_run(history,server,project=config.snapshot['project_id'],message='origin')
                    descriptor=history.select_attachment(config.snapshot['project_id'],'s',old['run_id'],old['exchanges'][0]['id'],fields=['/id','/value'])
                    document['attachments']=[descriptor]
                    setup_http=len(server.journal)
                    (directory/'origin-run.json').write_text(json.dumps(old,ensure_ascii=False,indent=2),encoding='utf-8')
                app=ManualApplication(catalog,config,history=history,
                    workspace=WorkspaceOptions(build_sdk(catalog,config),os.environ['WORKSPACE_IMAGE'],model_name='Qwen3.5-4B-IQ4_XS'),
                    token_counter=model.count_tokens)
                preview=app.preview(document)
                (directory/'accepted-preview.json').write_text(json.dumps(preview,ensure_ascii=False,indent=2),encoding='utf-8')
                started=time.monotonic()
                run=app.start(config.snapshot['project_id'],'s','m',document,model,accepted_hash=preview['preview_hash'])
                (directory/'run.json').write_text(json.dumps(run,ensure_ascii=False,indent=2),encoding='utf-8')
            checks=run['report']['goal_checklist']['verdict']
            created=set(server.devices)-{42}
            if name=='partial_pass':
                state=bool(created) and all(server.devices[i]['value']==80 for i in created) and any(m=='PUT' for m,_,_ in server.journal) and any(m=='GET' and p.endswith('/'+str(i)) for i in created for m,p,_ in server.journal)
                want='PASS'
            elif name=='independent_fail':
                state=server.devices[42]['value']==57 and any(server.devices[i]['value']==80 for i in created)
                want='FAIL'
            elif name=='covered':state=not created and server.devices[42]['value']==57 and bool(server.journal) and all(m=='GET' and p=='/devices/42' for m,p,_ in server.journal);want='PASS'
            elif name=='attachment':state=not created and server.devices[42]['value']==80 and any(m=='PUT' and p=='/devices/42' for m,p,_ in server.journal[setup_http:]) and any(m=='GET' and p=='/devices/42' for m,p,_ in server.journal[setup_http:]);want='PASS'
            else:state=bool(created) and any(m=='GET' and p.endswith('/'+str(i)) for i in created for m,p,_ in server.journal);want='N/A'
            answer=(run.get('final') or {}).get('summary','')
            required_number='57' if name=='covered' else '80'
            honest_answer=required_number in answer
            if name=='independent_fail':honest_answer=honest_answer and ('57' in answer or '999' in answer)
            order=feedback_order(run,name) if name in {'partial_pass','independent_fail'} else None
            record={'case':name,'status':run['status'],'stop_reason':run['stop_reason'],'checks':checks,'expected_checks':want,
                'state_confirmed':state,'answer_numeric_witness':honest_answer,'http':len(server.journal)-setup_http,'setup_http':setup_http,
                'model_calls':run['model_calls'],'active_seconds':run.get('wall_seconds'),
                'feedback_order':order,
                'successful':run['status']=='completed' and checks==want and state and honest_answer and (order is None or order['confirmed'])}
            (directory/'observer.json').write_text(json.dumps({'record':record,'journal':server.journal,'devices':server.devices},ensure_ascii=False,indent=2),encoding='utf-8')
            results.append(record);print(json.dumps(record,ensure_ascii=False),flush=True)
            technical = all(r['successful'] for r in results if r['case']!='research') and all(
                r['checks']=='N/A' and r['http']>0 and r['stop_reason'] in {'finished','code_correction_limit','step_limit','token_limit','run_time_limit'}
                for r in results if r['case']=='research')
            summary={'records':results,'source_hashes':source,'kind':__doc__,'all_successful':all(r['successful'] for r in results),
                'technical_controls_passing':technical,'business_successes':sum(r['successful'] for r in results),
                'observer_source_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'acceptance_scope':'Partial coverage/independent FAIL behavior; research must retain N/A and safe stop. Live task success is diagnostic, not a 100% per-slice gate.'}
            (output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    return 0 if summary['technical_controls_passing'] else 1


if __name__=='__main__':raise SystemExit(main(sys.argv[1]))

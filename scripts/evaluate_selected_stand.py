"""S10 предметный smoke: две живые цели и явный перенос заявки57→80 без reset."""
from pathlib import Path
import hashlib
import importlib.util
import json
import os
import secrets
import sys
import uuid
from urllib.parse import quote

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from api_agent.agent.model import LocalModel
from api_agent.goals.entry.manual_application import ManualApplication
from api_agent.openapi import load_source
from api_agent.policy.config import ProjectConfig
from api_agent.storage import SQLiteHistory
from api_agent.agent.runtime import WorkspaceOptions
from api_agent.workspace.sdk import build_sdk


def main(output):
    root=Path(output);root.mkdir(parents=True,exist_ok=False)
    spec=importlib.util.spec_from_file_location('s10_stand_owner','C:/RT5-51-Prokofiev-Ilya-RIP-Frontend/scripts/stand.py')
    owner=importlib.util.module_from_spec(spec);spec.loader.exec_module(owner)
    state=owner.control('GET','/state')
    if not state['ready'] or state['mode']!='good':raise ValueError('stand_not_good')
    catalog=load_source('C:/RT5-51-Prokofiev-Ilya-RIP-Frontend/backend/docs/openapi.json')
    project='s10-stand-'+uuid.uuid4().hex
    operations={'post_api_users_login':'C','get_api_trips_scenarioscart':'A',
        'get_api_trips_trip_id':'A','put_api_trips_trip_id':'C'}
    config=ProjectConfig.create(project,'http://127.0.0.1:18080',catalog,mode='TEST',resettable=True,operations=operations)
    login='s10_'+uuid.uuid4().hex[:12];password=secrets.token_urlsafe(24)
    credentials=[]
    def save(name,value):
        text=json.dumps(value,ensure_ascii=False,indent=2)
        if any(secret and secret in text for secret in [password,owner.initialize(),*credentials]):
            raise ValueError('private_evidence_denied')
        (root/name).write_text(text,encoding='utf-8')
    def request(method,path,body=None,token=None,want=200):
        response=owner.request(owner.API_PORT,method,path,body,token=token)
        if response['status']!=want:raise ValueError('stand_preparation_rejected')
        return response['value']
    request('POST','/api/users/register',{'login':login,'password':password},want=201)
    token=request('POST','/api/users/login',{'login':login,'password':password})['token'];credentials.append(token)
    scenarios=request('GET','/api/scenarios?name='+quote('Загородная трасса'))
    road=next(s for s in scenarios if s['name']=='Загородная трасса')
    request('POST',f"/api/scenarios/{road['id']}/add-to-trip",{},token)
    trip_id=request('GET','/api/trips/scenarioscart',token=token)['trip_id']
    request('PUT',f'/api/trips/{trip_id}',{'start_charge':57},token)
    before=request('GET',f'/api/trips/{trip_id}',token=token)
    mappings={'auth':{'login_operation':'post_api_users_login','token_path':'/token',
        'body':{'login':login,'password':password},'public_body_fields':['login']},
        'extraction':{'post_api_users_login':{'/token':{'type':'string','secret':True}}}}
    model=LocalModel('http://127.0.0.1:18081');artifact=build_sdk(catalog,config)
    source={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'src').rglob('*') if p.is_file() and p.suffix in {'.py','.sql'}}
    results=[]
    with SQLiteHistory(root/'history.sqlite3') as history:
        app=ManualApplication(catalog,config,history=history,mappings=mappings,
            workspace=WorkspaceOptions(artifact,os.environ['WORKSPACE_IMAGE']),token_counter=model.count_tokens)
        initial={'schema_version':'selected-checks-v1','mode':'testing',
            'goal':'Войди в выделенный приложением тестовый аккаунт. Открой корзину, прочитай её существующую заявку. Проверь начальную энергию57 кВт·ч, покажи номер и статус. Ничего не изменяй.',
            'cards':[{'id':'trip','operation_id':'get_api_trips_trip_id','statuses':[200],
                'selector':'last','auth':'current','where':{'parameters':{'trip_id':{'response':{'operation_id':'get_api_trips_scenarioscart','path':'/trip_id','occurrence':'last'}}}},
                'assertions':[{'predicate':'equals','path':'/start_charge','value':57}]}]}
        preview=app.preview(initial);save('origin-preview.json',preview)
        origin=app.start(project,'origin','origin',initial,model,accepted_hash=preview['preview_hash']);save('origin-run.json',origin)
        read=next((e for e in reversed(origin.get('exchanges',[])) if e['operation_id']=='get_api_trips_trip_id' and (e.get('response') or {}).get('status')==200),None)
        actual=request('GET',f'/api/trips/{trip_id}',token=token)
        results.append({'case':'origin','status':origin['status'],'stop_reason':origin['stop_reason'],
            'checks':origin['report']['checks_verdict'],'http':len(origin['exchanges']),
            'successful':origin['status']=='completed' and origin['report']['checks_verdict']=='PASS' and actual==before})
        if read:
            descriptor=history.select_attachment(project,'origin',origin['run_id'],read['id'],fields=['/id','/status','/start_charge'])
            document={'schema_version':'selected-checks-v1','mode':'testing',
                'goal':'Войди в выделенный приложением аккаунт. У прикреплённой заявки установи начальную энергию80 кВт·ч. Состав сохрани. Новую заявку не создавай, на рассмотрение не отправляй. Прочитай и покажи её номер, энергию и статус.',
                'attachments':[descriptor],'cards':[{'id':'updated','operation_id':'get_api_trips_trip_id','statuses':[200],
                    'auth':'current','where':{'parameters':{'trip_id':trip_id}},'assertions':[
                        {'predicate':'equals','path':'/id','value':trip_id},
                        {'predicate':'equals','path':'/start_charge','value':80},
                        {'predicate':'equals','path':'/status','value':before['status']}]}]}
            preview=app.preview(document);save('reuse-preview.json',preview)
            run=app.start(project,'reuse','reuse',document,model,accepted_hash=preview['preview_hash']);save('reuse-run.json',run)
            after=request('GET',f'/api/trips/{trip_id}',token=token)
            cart=request('GET','/api/trips/scenarioscart',token=token)
            save('observed.json',{'before':before,'after':after,'cart':cart})
            unchanged=history.read_run(project,'origin',origin['run_id'])==origin
            results.append({'case':'reuse','status':run['status'],'stop_reason':run['stop_reason'],
                'checks':run['report']['checks_verdict'],'http':len(run['exchanges']),'origin_unchanged':unchanged,
                'successful':run['status']=='completed' and run['report']['checks_verdict']=='PASS' and run['parent_run_id']==origin['run_id']
                    and unchanged and after['id']==trip_id and after['start_charge']==80 and after['status']==before['status']
                    and after['scenarios']==before['scenarios'] and cart['trip_id']==trip_id})
    final_state=owner.control('GET','/state')
    summary={'records':results,'source_hashes':source,'business_successes':sum(r['successful'] for r in results),
        'all_successful':len(results)==2 and all(r['successful'] for r in results),
        'counts_before':state['counts'],'counts_after':final_state['counts'],
        'scope':'Реальная primary/HTTP18080; отдельный новый обычный аккаунт и setup draft. Без reset/смены режима; не STAND40x3.',
        'observer_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    save('summary.json',summary)
    print(json.dumps({k:v for k,v in summary.items() if k!='source_hashes'},ensure_ascii=False))
    return 0 if summary['all_successful'] else 1


if __name__=='__main__':raise SystemExit(main(sys.argv[1]))

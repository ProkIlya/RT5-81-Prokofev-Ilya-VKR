"""Focused live S12: настоящий LocalModel/worker/HTTP, отдельный task observer."""
import json
import os
from pathlib import Path
import sys
import uuid
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/p) for p in ('src','tests','')]

from api_agent.desktop.controller import DesktopController
from api_agent.desktop.store import DesktopStore
from api_agent.policy.config import ProjectConfig
from selected_support import DeviceServer,catalog_config


def main():
    root=Path(sys.argv[1] if len(sys.argv)>1 else 'evidence/S12/live-'+uuid.uuid4().hex[:10]).resolve()
    root.mkdir(parents=True,exist_ok=False)
    with DeviceServer() as server,DesktopStore(root/'ui.sqlite3') as store:
        catalog,_=catalog_config(server.target,'desktop-live')
        spec={'openapi':'3.0.3','info':{'title':'Devices','version':'1'},'paths':{}}
        for name,op in catalog.operations.items():
            target={k:v for k,v in op.items() if k not in {'method','path'}};target['operationId']=name
            for response in target['responses'].values():response['description']='Response'
            spec['paths'].setdefault(op['path'],{})[op['method'].lower()]=target
        (root/'openapi.json').write_text(json.dumps(spec,ensure_ascii=False),encoding='utf-8')
        policy=ProjectConfig.create('desktop-live',server.target,catalog,mode='READ_ONLY',resettable=False,
                                    operations={'readDevice':'A'})
        policy.save(root/'policy.json')
        store.add_project('desktop-live','Devices · Live',{'target':server.target,'spec_path':str(root/'openapi.json'),
            'policy_path':str(root/'policy.json'),'image':os.environ['WORKSPACE_IMAGE'],
            'history_path':str(root/'history.sqlite3')})
        store.add_session('desktop-live','live','Проверка интерфейса')
        store.set_setting('onboarded',True);store.set_setting('selection',['desktop-live','live'])
        store.save_model('local',{'name':'Фактическая локальная модель','kind':'server',
            'endpoint':'http://127.0.0.1:18081','context_tokens':8192,'output_tokens':1024})
        controller=DesktopController(store)
        draft={'goal':'Прочитай прибор42, сообщи фактическое значение поля value. Ничего не изменяй.',
               'cards':[],'attachments':[],'model_id':'local'}
        store.save_draft('desktop-live','live',draft)
        prepared=controller.prepare('desktop-live','live',draft)
        (root/'model-snapshot.json').write_text(json.dumps(prepared.model_snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
        controller.start(prepared,prepared.preview['preview_hash'])
        while controller.busy:controller.join(1)
        result=controller.result
        (root/'run.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        (root/'journal.json').write_text(json.dumps(server.journal,ensure_ascii=False,indent=2),encoding='utf-8')
        text=(result.get('final') or {}).get('summary','')
        verdict={'technical':result.get('status')=='completed','task':server.journal==[('GET','/devices/42',None)] and '57' in text,
            'checks':(result.get('report') or {}).get('checks_verdict'), 'model':prepared.model_snapshot['model_name'],
            'identity':prepared.model_snapshot['identity'],'run_id':result.get('run_id'),'stop_reason':result.get('stop_reason'),
            'root':str(root),'scope':'Focused live, not STAND40/readiness or independent ACCEPT'}
        (root/'verdict.json').write_text(json.dumps(verdict,ensure_ascii=False,indent=2),encoding='utf-8')
        controller.close();print(json.dumps(verdict,ensure_ascii=False))
        return 0 if verdict['technical'] and verdict['task'] else 1


if __name__=='__main__':raise SystemExit(main())

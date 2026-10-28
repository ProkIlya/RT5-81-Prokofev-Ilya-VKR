"""Focused real LLM + Textual Send/preview + Docker/API, отдельный task observer."""
import asyncio
import json
import os
from pathlib import Path
import sys
import time
import uuid
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/p) for p in ('src','tests','')]
from api_agent.desktop.app import AgentApp
from api_agent.desktop.controller import DesktopController
from api_agent.desktop.dialogs import PreviewDialog
from api_agent.desktop.store import DesktopStore
from api_agent.policy.config import ProjectConfig
from desktop_support import project_files
from selected_support import DeviceServer,card
from check_desktop_stage import identities


def write(root,name,value):
    (root/name).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


async def evaluate(root):
    root.mkdir(parents=True,exist_ok=False);before=identities();write(root,'identity-before.json',before)
    verdicts=[]
    with DeviceServer() as server,DesktopStore(root/'ui.sqlite3') as store:
        catalog,_=project_files(store,root,server)
        policy=ProjectConfig.create('p',server.target,catalog,mode='READ_ONLY',resettable=False,operations={'readDevice':'A'})
        policy.save(root/'policy.json');store.set_setting('onboarded',True)
        store.save_model('local',{'name':'Реальная локальная модель','kind':'server',
            'endpoint':os.environ.get('WORKSPACE_TOKENIZER_URL','http://127.0.0.1:18081'),
            'context_tokens':8192,'output_tokens':1024})
        controller=DesktopController(store);app=AgentApp(store,controller=controller)
        try:
            async with app.run_test(size=(132,46)) as pilot:
                for mode in ['research','testing']:
                    store.add_session('p',mode,mode)
                    store.save_draft('p',mode,{'goal':'Прочитай прибор42. Сообщи фактическое значение поля value. Ничего не изменяй.',
                        'cards':[] if mode=='research' else [card(where={'parameters':{'id':42}},
                            assertions=[{'predicate':'equals','path':'/value','value':57}])],
                        'attachments':[],'model_id':'local'})
                    app.open_session('p',mode);await pilot.pause();controller.result=None
                    offset=len(server.journal);start=time.monotonic();await pilot.click('#send');preview_seen=False
                    while time.monotonic()-start<240:
                        if isinstance(app.screen,PreviewDialog):
                            preview_seen=True;await pilot.pause()
                            write(root,mode+'-preview.json',app.screen.value)
                            if mode=='research':raise AssertionError('Unexpected Research preview')
                            if server.journal[offset:]:raise AssertionError('API before acceptance')
                            if not await pilot.click('#preview-accept'):raise AssertionError('Start button unavailable')
                            await pilot.pause()
                        if controller.result is not None and not controller.busy:break
                        await asyncio.sleep(.1)
                    result=controller.result or {};journal=server.journal[offset:]
                    write(root,mode+'-run.json',result);write(root,mode+'-journal.json',journal)
                    text=(result.get('final') or {}).get('summary','')
                    verdict={'mode':mode,'technical':result.get('status')=='completed',
                        'task':journal==[('GET','/devices/42',None)] and '57' in text,
                        'checks':(result.get('report') or {}).get('checks_verdict'),
                        'expected_checks':'N/A' if mode=='research' else 'PASS','preview_seen':preview_seen,
                        'run_id':result.get('run_id'),'stop_reason':result.get('stop_reason'),
                        'identity':(result.get('model') or {}).get('snapshot',{}).get('identity'),
                        'seconds':time.monotonic()-start}
                    verdicts.append(verdict);write(root,'verdicts.json',verdicts)
                    await pilot.pause();(root/(mode+'-screen.svg')).write_text(app.export_screenshot(),encoding='utf-8')
        finally:controller.shutdown()
    unchanged=before==identities();passing=unchanged and len(verdicts)==2 and all(v['technical'] and v['task']
        and v['checks']==v['expected_checks'] and v['preview_seen']==(v['mode']=='testing') for v in verdicts)
    result={'passing':passing,'source_tests_unchanged':unchanged,'verdicts':verdicts,
        'scope':'Focused real LLM/Textual/Docker/API; not full STAND40 or independent ACCEPT'}
    write(root,'result.json',result);print(json.dumps(result,ensure_ascii=False));return 0 if passing else 1


if __name__=='__main__':
    root=Path(sys.argv[1] if len(sys.argv)>1 else 'evidence/S12/ui-live-'+uuid.uuid4().hex[:10]).resolve()
    raise SystemExit(asyncio.run(evaluate(root)))

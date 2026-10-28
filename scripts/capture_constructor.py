"""Actual Textual renders конструктора на публичном fixture; без LLM/HTTP."""
import asyncio
import json
import os
from pathlib import Path
import sys
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/p) for p in ('src','tests','')]
os.environ.pop('NO_COLOR',None);os.environ['FORCE_COLOR']='1';os.environ['COLORTERM']='truecolor'
from textual.app import App
from api_agent.desktop.constructor import ConstructorDialog
from api_agent.desktop.constructor_state import ConstructorState
from api_agent.desktop.card_editor import CardEditor
from api_agent.desktop.collection_editor import CollectionEditor
from api_agent.desktop.dialogs import PreviewDialog
from selected_support import catalog_config,card


async def main():
    root=Path(sys.argv[1]);root.mkdir(parents=True,exist_ok=False)
    catalog,config=catalog_config();selected=card(name='Проверить значение выбранного прибора',
        where={'parameters':{'id':{'response':{'operation_id':'createDevice','path':'/id','occurrence':'first'}}}},
        assertions=[{'predicate':'equals','path':'/value','value':57}])
    state=ConstructorState(catalog,config,[selected])
    for size_name,size in [('wide',(132,44)),('narrow',(74,28))]:
        for name,factory in [('constructor',lambda:ConstructorDialog(state)),('card',lambda:CardEditor(state,selected)),
            ('collection',lambda:CollectionEditor(state,state.response_fields('readDevice',[200])['/rows'],
                [{'/name':'Основной','/amount':0},{'/name':'Основной','/amount':0}],'exact')),
            ('preview',lambda:PreviewDialog({'configuration':state.compiled('Создай прибор57 и проверь его значение.').snapshot,
                'model':{'model_name':'Qwen3.5-4B','context_tokens':8192}}))]:
            app=App()
            async with app.run_test(size=size) as pilot:
                app.push_screen(factory());await pilot.pause()
                (root/(name+'-'+size_name+'.svg')).write_text(app.export_screenshot(),encoding='utf-8')
    (root/'scope.json').write_text(json.dumps({'scope':'Actual Textual render; public fixture; no model/HTTP/Run',
        'sizes':[[132,44],[74,28]],'source_hash':state.catalog.spec_hash},ensure_ascii=False,indent=2),encoding='utf-8')


if __name__=='__main__':asyncio.run(main())

"""Сохранить actual Textual render выбранного Run без модели/HTTP/replay."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
os.environ.pop('NO_COLOR',None)
os.environ['FORCE_COLOR']='1'
os.environ['COLORTERM']='truecolor'
from api_agent.desktop.app import AgentApp
from api_agent.desktop.store import DesktopStore
from api_agent.desktop.inspector import JsonInspector


async def capture(args):
    directory=Path(args.output);directory.mkdir(parents=True,exist_ok=True)
    with DesktopStore(args.state) as store:
        for label,size in [('wide',(132,44)),('narrow',(74,28))]:
            app=AgentApp(store)
            async with app.run_test(size=size) as pilot:
                app.open_session(args.project,args.session)
                app.inspect_message(args.run,'assistant');await pilot.pause()
                table=app.query_one('#exchanges')
                if table.row_count:
                    key=next(iter(table.rows)).value
                    app.show_exchange(key)
                    tree=app.query_one('#json-tree',JsonInspector)
                    tree.root.children[1].expand()
                    for node in tree.root.children[1].children:
                        if isinstance(node.data,dict) and node.data.get('path')=='/json':node.expand()
                await pilot.pause()
                (directory/(label+'.svg')).write_text(app.export_screenshot(),encoding='utf-8')
                (directory/(label+'.json')).write_text(json.dumps({'size':size,'project':app.project,'session':app.session,
                    'run':app.inspected_run,'model_prepared':app.controller.manager.current is not None,
                    'exchanges':table.row_count},ensure_ascii=False,indent=2),encoding='utf-8')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--state',required=True);parser.add_argument('--project',required=True)
    parser.add_argument('--session',required=True);parser.add_argument('--run',required=True);parser.add_argument('--output',required=True)
    asyncio.run(capture(parser.parse_args()))


if __name__=='__main__':main()

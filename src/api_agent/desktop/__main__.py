"""Самостоятельный запуск TUI; CLI выбранного code runtime сохранён."""
import argparse
from pathlib import Path


def main(argv=None):
    parser=argparse.ArgumentParser(description='API Testing Agent — Projects, Sessions, HTTP trace')
    parser.add_argument('--state',default='config/desktop/ui.sqlite3',help='Локальная база настроек и черновиков TUI')
    args=parser.parse_args(argv)
    from api_agent.desktop.app import AgentApp
    from api_agent.desktop.store import DesktopStore
    with DesktopStore(Path(args.state)) as store:
        app=AgentApp(store)
        try:app.run()
        finally:
            app.controller.shutdown()
    return 0


if __name__=='__main__':raise SystemExit(main())

"""Фактическое открытие SavedResults и инспектора; model preparation запрещена."""
import asyncio
from unittest.mock import patch

from textual.widgets import OptionList, TabbedContent, TextArea, DataTable
from api_agent.agent.model import LocalModel
from api_agent.desktop.application.app import AgentApp
from api_agent.desktop.state.models import ModelManager
from api_agent.desktop.state.store import DesktopStore
from api_agent.desktop.views.project_views import ResultsDialog, open_project_view


def counts(history):
    row = history.db.execute("SELECT COUNT(*),COALESCE(SUM(COALESCE(json_extract(payload,'$.model_calls'),0)),0),"
        "COALESCE(SUM(COALESCE(json_extract(payload,'$.requests'),0)),0) FROM runs").fetchone()
    return tuple(row)


async def open_tabs(store, project, fixture_run, output):
    app = AgentApp(store)
    try:
        async with app.run_test(size=(132, 46)) as pilot:
            app.open_session(project, fixture_run['session_id']); await pilot.pause()
            open_project_view(app, 'results'); await pilot.pause()
            if not isinstance(app.screen, ResultsDialog): raise ValueError('history_results_not_opened')
            rows = app.screen.rows
            index = next(i for i, row in enumerate(rows) if row['run_id'] == fixture_run['run_id'])
            options = app.screen.query_one('#result-list', OptionList); options.highlighted = index
            options.focus(); await pilot.press('enter'); await pilot.pause()
            if app.inspected_run != fixture_run['run_id']: raise ValueError('history_wrong_result_opened')
            tabs = []
            for name in ('config-tab', 'report-tab', 'trace-tab'):
                app.query_one(TabbedContent).active = name; await pilot.pause()
                (output / (name + '.svg')).write_text(app.export_screenshot(), encoding='utf-8'); tabs.append(name)
            if not app.query_one('#configuration', TextArea).text or not app.query_one('#exchanges', DataTable).row_count:
                raise ValueError('history_configuration_or_trace_missing')
            return tabs
    finally: app.controller.shutdown()


def run_history_action(history, project, fixture_run, configuration, output):
    before = counts(history); blocked = []
    def forbid(*args, **kwargs):
        blocked.append('model_or_prepare'); raise AssertionError('history_must_not_execute')
    with DesktopStore(output / 'history-ui.sqlite3') as store:
        store.add_project(project, 'STAND35 · Сохранённый результат', configuration)
        store.add_session(project, fixture_run['session_id']); store.set_setting('onboarded', True)
        with patch.object(ModelManager, 'prepare', forbid), patch.object(LocalModel, '__call__', forbid):
            tabs = asyncio.run(open_tabs(store, project, fixture_run, output))
    after = counts(history)
    return {'history_viewed': not blocked, 'selected_run_id': fixture_run['run_id'], 'tabs': tabs,
        'new_runs': after[0] - before[0], 'new_model_calls': after[1] - before[1] + len(blocked),
        'new_http': after[2] - before[2], 'guards_triggered': blocked}

"""Полная диагностика локального исполнения по явному действию пользователя."""
from textual import work
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Label
from api_agent.desktop.components.controls import Button, Select, Summary
from api_agent.desktop.components.widgets import PageDialog
from api_agent.setup.environment import inspect_environment


class EnvironmentDialog(PageDialog):
    def __init__(self, store, controller, *, project=None):
        super().__init__(); self.store = store; self.controller = controller
        self.project = project; self.report = None; self.checking = False

    def compose(self):
        with Vertical(classes='page-shell'):
            yield Label('Диагностика окружения', classes='page-title')
            with VerticalScroll(classes='page-body'):
                yield Summary('Проверяются зависимости, миграции SQLite на временной копии, '
                    'изолированный Docker-исполнитель и выбранная модель с tokenizer/template. '
                    'Агентный Run и обращения к тестируемому API не создаются.')
                yield Select([('Модель не выбрана', '')] + [(r['profile']['name'], r['id']) for r in self.store.models()],
                             value='', allow_blank=False, id='environment-model')
                yield Summary('', id='environment-report')
            with Horizontal(classes='page-actions'):
                yield Button('Проверить', id='environment-check', variant='primary')
                yield Button('Назад', id='environment-close')

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'environment-close': self.dismiss(None); return
        if event.button.id != 'environment-check' or self.checking: return
        if self.controller.busy or self.controller.manager.active:
            self.query_one('#environment-report', Summary).update('Дождитесь завершения активного Run.'); return
        self.checking = True; self.query_one('#environment-check', Button).disabled = True
        self.query_one('#environment-report', Summary).update('Проверка окружения…')
        key = self.query_one('#environment-model', Select).value
        image = self.store.setting('worker_image')
        history = self.store.path.parent / 'history.sqlite3'
        if self.project:
            config = self.store.project(self.project)['configuration']
            image = config.get('image') or image
            history = self.controller.history_path(self.project)
        self.check(image, history, self.store.model(key) if key else None)

    @work(thread=True, exclusive=True, group='environment-doctor')
    def check(self, image, history, record):
        report = inspect_environment(image=image, history_path=history, model_record=record,
                                     manager=self.controller.manager)
        self.app.call_from_thread(self.checked, report)

    def checked(self, report):
        if not self.is_mounted: return
        self.report = report; self.checking = False
        self.query_one('#environment-check', Button).disabled = False
        names = {'dependencies': 'Зависимости', 'storage': 'SQLite/миграции', 'sandbox': 'Docker-исполнитель', 'model': 'Модель'}
        states = {'ready': 'готово', 'unavailable': 'недоступно или несовместимо', 'not_configured': 'не настроено'}
        lines = ['Окружение готово.' if report['ready'] else 'Окружение не готово: устраните указанные препятствия.']
        lines += [names[c['name']] + ': ' + states[c['state']] for c in report['checks']]
        self.query_one('#environment-report', Summary).update('\n\n'.join(lines))

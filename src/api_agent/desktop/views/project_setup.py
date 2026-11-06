"""Проверяемая форма Project: doctor до сохранения, API availability advisory."""
from textual import work
from textual.widgets import Label

from api_agent.desktop.components.controls import Button, Input, Select, Summary
from api_agent.desktop.views.dialogs import FormDialog, PROJECT_FIELDS
from api_agent.setup.doctor import inspect_project, project_fingerprint


def report_text(report):
    lines = ['Спецификация: ' + ('поддерживается' if report['structural']['supported'] else 'не поддерживается')]
    states = {'not_checked': 'не проверено', 'reachable': 'доступно', 'unavailable': 'API недоступен'}
    lines.append('Соединение: ' + states[report['connection']['state']] +
                 '. Недоступность API не запрещает сохранить проект или выполнить цель.')
    lines.append('Поддержанные проверки: ' + ', '.join(report['supported_predicates']))
    if report.get('policy'):
        access = report['policy']['operations']
        counts = {kind: sum(op['access'] == kind for op in access) for kind in ['ALLOW', 'CONFIRM', 'DENY', 'HIDE']}
        lines.append('Права: ' + ', '.join(kind + ' ' + str(count) for kind, count in counts.items()))
    for item in report['issues'][:20]:
        level = {'block': 'Ошибка', 'warn': 'Предупреждение', 'recommend': 'Рекомендация'}[item['level']]
        lines.append(level + ': ' + item['pointer'] + '\n' + item['reason'] + ' ' + item['correction'])
    if len(report['issues']) > 20: lines.append('Дополнительные замечания доступны в JSON-диагностике.')
    return '\n\n'.join(lines)


class ProjectSetupDialog(FormDialog):
    def __init__(self, *, values=None, title='Добавить проект'):
        super().__init__(title, PROJECT_FIELDS, values=values)
        self.report = None; self.checking = False; self.probe_names = ()
        self.connection = None

    async def on_mount(self):
        super().on_mount()
        await self.query_one('.page-actions').mount(Button('Проверить', id='project-check'), before=0)
        await self.query_one('.page-actions').mount(Button('Настроить вход', id='connection-settings'), before=0)
        body = self.query_one('.page-body')
        await body.mount(Label('Проверка соединения: только выбранное публичное чтение'),
            Select([('Не обращаться к API', '')], value='', allow_blank=False, id='project-probe'),
            Summary('', id='project-diagnostics'))

    def current_values(self):
        return {key: self.query_one('#field-' + key).value for key, *_ in self.fields}

    def on_button_pressed(self, event):
        event.prevent_default()
        if event.button.id == 'connection-settings':
            event.stop()
            from api_agent.openapi import load_source
            from api_agent.desktop.views.connection_setup import ConnectionSetupDialog
            from api_agent.desktop.presentation.diagnostic_text import error_text
            values = self.current_values()
            try: catalog = load_source(values['spec_path'], target=values['target'])
            except (ValueError, OSError) as error:
                self.query_one('#project-diagnostics', Summary).update(error_text(error)); return
            def selected(result):
                if result is not None:
                    self.query_one('#field-mappings_path', Input).value = ''
                    self.connection = (catalog.spec_hash, values['target'], result)
                    self.report = None
                    self.query_one('#project-diagnostics', Summary).update(
                        'После сохранения будет использован вход из формы; импорт файла отключён.' if result else
                        'После сохранения вход и импорт файла будут отключены. Файлы на диске сохранятся.')
            self.app.push_screen(ConnectionSetupDialog(catalog), selected)
            return
        if event.button.id not in {'project-check', 'form-save'}:
            return super().on_button_pressed(event)
        event.stop()
        if self.checking: return
        values = self.current_values(); save = event.button.id == 'form-save'
        if save and self.report and self.report['fingerprint'] == project_fingerprint(values):
            if self.report['can_save']: super().on_button_pressed(event)
            return
        self.checking = True
        self.query_one('#project-diagnostics', Summary).update('Проверка спецификации и конфигурации…')
        self.query_one('#form-save', Button).disabled = True
        self.query_one('#project-check', Button).disabled = True
        self.perform_check(values, self.query_one('#project-probe', Select).value, save)

    @work(thread=True, exclusive=True, group='project-doctor')
    def perform_check(self, values, probe, save):
        report = inspect_project(values, network=bool(probe), probe_operation=probe or None)
        self.app.call_from_thread(self.checked, values, report, save)

    def checked(self, values, report, save):
        if not self.is_mounted: return
        self.checking = False
        self.query_one('#form-save', Button).disabled = False
        self.query_one('#project-check', Button).disabled = False
        if values != self.current_values() or report['fingerprint'] != project_fingerprint(values):
            self.report = None
            self.query_one('#project-diagnostics', Summary).update('Настройки изменились во время проверки. Нажмите «Проверить» ещё раз.')
            return
        self.report = report
        names = tuple(report['probe_operations'])
        if names != self.probe_names:
            self.probe_names = names
            self.query_one('#project-probe', Select).set_options([('Не обращаться к API', '')] + [(name, name) for name in names])
            self.query_one('#project-probe', Select).value = ''
        self.query_one('#project-diagnostics', Summary).update(report_text(report))
        if save and report['can_save']:
            # Повторная обычная валидация/acceptance не обходится результатом doctor.
            from textual.widgets import Button as BaseButton
            super().on_button_pressed(BaseButton.Pressed(self.query_one('#form-save', Button)))

    def dismiss(self, result=None):
        if isinstance(result, dict) and self.connection is not None and not result.get('mappings_path'):
            from api_agent.openapi import load_source
            digest, target, mapping = self.connection
            catalog = load_source(result['spec_path'], target=result['target'])
            if digest != catalog.spec_hash or target != result['target']:
                self.query_one('#project-diagnostics', Summary).update('Спецификация/адрес изменились. Настройте вход ещё раз.')
                return
            result = {**result, '_connection': mapping}
        return super().dismiss(result)

"""Явный выбор login и credential schema paths; сетевых вызовов здесь нет."""
import json

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Label
from api_agent.desktop.components.controls import Button, Input, Select, Summary
from api_agent.desktop.components.widgets import PageDialog
from api_agent.desktop.presentation.diagnostic_text import error_text
from api_agent.openapi.model import pointer_part
from api_agent.setup.credentials import compile_connection


def login_operations(catalog):
    return {name: op for name, op in catalog.operations.items()
        if op['method'] == 'POST'
        and (not op['security'] or any(not a for a in op['security']))
        and not any(p.get('required') for p in op['parameters'])
        and op.get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema', {}).get('type') == 'object'}


def string_paths(schema, prefix=''):
    if schema.get('type') == 'string' and prefix: return [prefix]
    if schema.get('type') != 'object': return []
    return [path for name, field in schema.get('properties', {}).items()
            for path in string_paths(field, prefix + '/' + pointer_part(name))]


class ConnectionSetupDialog(PageDialog):
    def __init__(self, catalog):
        super().__init__(); self.catalog = catalog; self.operations = login_operations(catalog)
        self.fields = {}; self.field_ids = {}; self.selected = None

    def compose(self):
        with Vertical(classes='page-shell'):
            yield Label('Настройка входа', classes='page-title')
            with VerticalScroll(classes='page-body'):
                yield Summary('Выберите операцию входа и поле токена из спецификации. Вход сейчас не выполняется. '
                              'Данные сохраняются локально в зашифрованном виде для текущего пользователя Windows.')
                yield Select([(name + ' · ' + op['path'], name) for name, op in self.operations.items()],
                             prompt='Операция входа', id='connection-operation')
                yield Label('Поле токена в успешном JSON-ответе')
                yield Select([], prompt='Выберите путь', id='connection-token')
                yield Vertical(id='connection-fields')
                yield Summary('', id='connection-error')
            with Horizontal(classes='page-actions'):
                yield Button('Сохранить', id='connection-save', variant='primary')
                yield Button('Без настроенного входа', id='connection-none')
                yield Button('Отмена', id='connection-cancel')

    async def on_select_changed(self, event):
        if event.select.id != 'connection-operation' or event.value not in self.operations: return
        self.selected = event.value; op = self.operations[event.value]
        schema = op['requestBody']['content']['application/json']['schema']
        self.fields = schema.get('properties', {})
        statuses = sorted(int(s) for s in op['responses'] if s.isdigit() and 200 <= int(s) < 300)
        response = op['responses'][str(statuses[0])].get('content', {}).get('application/json', {}).get('schema', {}) if statuses else {}
        self.query_one('#connection-token', Select).set_options([(path, path) for path in string_paths(response)])
        area = self.query_one('#connection-fields'); await area.remove_children(); self.field_ids = {}
        for index, (name, field) in enumerate(self.fields.items()):
            if field.get('type') not in {'string', 'integer', 'number', 'boolean'}: continue
            key = 'credential-' + name if name.replace('-', '').isalnum() and name[0].isalpha() else 'credential-field-' + str(index)
            self.field_ids[name] = key
            await area.mount(Label(name + (' *' if name in schema.get('required', []) else '')),
                             Input(password=field['type'] == 'string', id=key))

    def on_button_pressed(self, event):
        event.stop()
        if event.button.id == 'connection-cancel': self.dismiss(None); return
        if event.button.id == 'connection-none': self.dismiss({}); return
        if event.button.id != 'connection-save': return
        try:
            if self.selected not in self.operations: raise ValueError('connection_login_operation_invalid')
            body = {}
            for name, key in self.field_ids.items():
                value = self.query_one('#' + key, Input).value
                if not value and name not in self.operations[self.selected]['requestBody']['content']['application/json']['schema'].get('required', []): continue
                body[name] = value if self.fields[name]['type'] == 'string' else json.loads(value)
            result = compile_connection(self.catalog, self.selected,
                                        self.query_one('#connection-token', Select).value, body)
        except (ValueError, TypeError):
            self.query_one('#connection-error', Summary).update('Выберите операцию/строковое поле токена и заполните поля по их типам.'); return
        for key in self.field_ids.values(): self.query_one('#' + key, Input).value = ''
        self.dismiss(result)

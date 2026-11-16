"""Явный выбор login и credential schema paths; сетевых вызовов здесь нет."""
import json

from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Label
from api_agent.desktop.components.controls import Button, Input, Select, Summary
from api_agent.desktop.components.widgets import PageDialog
from api_agent.desktop.presentation.diagnostic_text import error_text
from api_agent.setup.credentials import compile_connection
from api_agent.setup.login_profile import login_cookies, login_fields, login_operations, token_paths, validate_login_schema


class ConnectionSetupDialog(PageDialog):
    def __init__(self, catalog, *, schema_only=False):
        super().__init__(); self.catalog = catalog; self.operations = login_operations(catalog)
        self.fields = {}; self.field_ids = {}; self.selected = None; self.schema_only = schema_only

    def compose(self):
        with Vertical(classes='page-shell'):
            yield Label('Настройка входа', classes='page-title')
            with VerticalScroll(classes='page-body'):
                if self.schema_only:
                    yield Summary('Выберите операцию входа и поля из OpenAPI. Учётные данные задаются в сессии.')
                else:
                    yield Summary('Данные прежней настройки входа сохраняются через Windows DPAPI.')
                yield Select([(name + ' · ' + op['path'], name) for name, op in self.operations.items()],
                             prompt='Операция входа', id='connection-operation')
                yield Label('Токен в JSON или имя cookie' if self.schema_only else 'Поле токена в успешном JSON-ответе')
                yield Select([], prompt='Выберите значение', id='connection-token')
                if self.schema_only:
                    yield Select([], prompt='Поле логина в запросе', id='connection-login-field')
                    yield Select([], prompt='Поле пароля в запросе', id='connection-password-field')
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
        credentials = [(path, path) for path in token_paths(op)]
        if self.schema_only:
            credentials.extend(('Cookie: ' + name, 'cookie:' + name) for name in sorted(login_cookies(self.catalog)))
        self.query_one('#connection-token', Select).set_options(credentials)
        if self.schema_only:
            options = [(name, name) for name in login_fields(op)]
            for key in ('login', 'password'):
                self.query_one('#connection-' + key + '-field', Select).set_options(options)
            return
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
            if self.schema_only:
                credential = self.query_one('#connection-token', Select).value
                schema = {'operation': self.selected}
                if isinstance(credential, str) and credential.startswith('cookie:'):
                    schema['cookie_name'] = credential.removeprefix('cookie:')
                else:
                    schema['token_path'] = credential
                for key in ('login', 'password'):
                    schema[key + '_field'] = self.query_one('#connection-' + key + '-field', Select).value
                self.dismiss(validate_login_schema(self.catalog, schema)); return
            if self.selected not in self.operations: raise ValueError('connection_login_operation_invalid')
            body = {}
            for name, key in self.field_ids.items():
                value = self.query_one('#' + key, Input).value
                if not value and name not in self.operations[self.selected]['requestBody']['content']['application/json']['schema'].get('required', []): continue
                body[name] = value if self.fields[name]['type'] == 'string' else json.loads(value)
            result = compile_connection(self.catalog, self.selected,
                                        self.query_one('#connection-token', Select).value, body)
        except (ValueError, TypeError):
            message = ('Выберите операцию, поля логина и пароля и токен или cookie.' if self.schema_only else
                       'Выберите операцию и поле токена и заполните поля по их типам.')
            self.query_one('#connection-error', Summary).update(message); return
        for key in self.field_ids.values(): self.query_one('#' + key, Input).value = ''
        self.dismiss(result)

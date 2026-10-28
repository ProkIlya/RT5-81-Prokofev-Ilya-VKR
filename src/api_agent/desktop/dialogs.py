"""Textual диалоги: явное принятие, private input и типизированные настройки."""
import json
import uuid
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Collapsible, Input, Label, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option


class FormDialog(ModalScreen):
    DEFAULT_CSS = '''
    FormDialog {align: center middle; background: $background 70%;}
    FormDialog > VerticalScroll {width: 80; max-width: 96%; height: 90%; border: round $accent; padding: 1 2; background: $surface;}
    FormDialog Input, FormDialog Select {margin-bottom: 1;}
    FormDialog Horizontal {height: auto; margin-top: 1;}
    FormDialog Button {margin-right: 1;}
    FormDialog .form-error {color: $error; height: auto;}
    '''
    BINDINGS = [('escape','back','Отмена')]

    def __init__(self, title, fields, *, values=None, id=None):
        super().__init__(id=id); self.dialog_title=title;self.fields=fields;self.values=values or {}

    def compose(self) -> ComposeResult:
        with VerticalScroll():
            yield Label(self.dialog_title)
            for key,label,default,options in self.fields:
                yield Label(label)
                value=self.values.get(key,default)
                if options:yield Select(options,value=value,allow_blank=False,id='field-'+key)
                else:yield Input(str(value) if value is not None else '',id='field-'+key)
            yield Static('',id='form-error',classes='form-error')
            with Horizontal():
                yield Button('Сохранить',id='form-save',variant='primary')
                yield Button('Отмена',id='form-cancel')

    def action_back(self): self.dismiss(None)

    def on_button_pressed(self,event):
        if event.button.id=='form-cancel':self.dismiss(None)
        elif event.button.id=='form-save':
            self.dismiss({key:self.query_one('#field-'+key).value for key,*_ in self.fields})


MODEL_FIELDS=[('name','Название конфигурации','',None),
    ('kind','Источник','server',[('Работающий локальный сервер','server'),('Файл GGUF','gguf')]),
    ('endpoint','Адрес локального сервера','http://127.0.0.1:18081',None),
    ('model_name','Идентификатор сервера (пусто — определить)','',None),
    ('gguf','Файл GGUF для управляемого запуска','',None),
    ('executable','Поддержанная сборка llama-server.exe','',None),
    ('port','Порт управляемого сервера',18085,None),
    ('context_tokens','Окно контекста',8192,None),('output_tokens','Максимум ответа',1024,None),
    ('gpu_layers','Слоёв на GPU (0 — CPU)',0,None)]

PROJECT_FIELDS=[('label','Название проекта','',None),('target','Адрес разрешённого API','http://127.0.0.1:18080',None),
    ('spec_path','Локальный файл OpenAPI JSON','',None),
    ('policy_path','Файл конфигурации прав (пусто — только чтение)','',None),
    ('mappings_path','Файл auth/extraction mappings (необязательно)','',None),
    ('image','Docker image с неизменным sha256','',None),
    ('history_path','Файл истории (необязательно)','',None)]


class ModelSettings(ModalScreen):
    DEFAULT_CSS = '''
    ModelSettings {align: center middle; background: $background 70%;}
    ModelSettings > VerticalScroll {width: 85; max-width: 96%; height: auto; max-height: 90%; padding: 1 2; border: round $accent; background: $surface;}
    ModelSettings OptionList {height: 9; margin: 1 0;}
    ModelSettings Horizontal {height: auto;}
    ModelSettings Button {margin-right: 1;}
    '''
    BINDINGS=[('escape','back','Назад')]
    def __init__(self,store):super().__init__(id='model-settings');self.store=store
    def compose(self):
        with VerticalScroll():
            yield Label('Модели приложения')
            yield Static('Выберите конфигурацию для проверки или добавьте новую. Загрузка выполняется отдельно от Run.')
            yield OptionList(*(Option(r['profile']['name']+' · '+r['profile']['kind'],id=r['id']) for r in self.store.models()),id='model-list')
            with Horizontal():
                yield Button('Добавить',id='model-add',variant='primary')
                yield Button('Изменить',id='model-edit')
                yield Button('Загрузить / проверить',id='model-check')
            yield Button('Назад',id='model-back')
    def action_back(self):self.dismiss(None)
    def on_button_pressed(self,event):
        event.stop();key=event.button.id
        if key=='model-back':self.dismiss(None)
        elif key=='model-add':self.dismiss(('edit',uuid.uuid4().hex,None))
        elif key in {'model-edit','model-check'}:
            options=self.query_one('#model-list',OptionList)
            if options.highlighted is None:return
            record=self.store.model(options.get_option_at_index(options.highlighted).id)
            self.dismiss(('check' if key=='model-check' else 'edit',record['id'],record['profile']))


class ChoiceDialog(ModalScreen):
    DEFAULT_CSS='''
    ChoiceDialog {align: center middle; background: $background 70%;}
    ChoiceDialog > VerticalScroll {width: 85; max-width: 96%; height: auto; max-height: 90%; border: round $accent; padding: 1 2; background: $surface;}
    ChoiceDialog OptionList {height: 9;}
    '''
    BINDINGS=[('escape','back','Назад')]
    def __init__(self,title,options,*,id=None):super().__init__(id=id);self.heading=title;self.options=options
    def compose(self):
        with VerticalScroll():
            yield Label(self.heading)
            yield OptionList(*(Option(label,id=str(key)) for key,label in self.options),id='choices')
            yield Button('Назад',id='choice-back')
    def on_option_list_option_selected(self,event):self.dismiss(event.option.id)
    def on_button_pressed(self,event):self.dismiss(None)
    def action_back(self):self.dismiss(None)


class PreviewDialog(ModalScreen):
    DEFAULT_CSS='''
    PreviewDialog {align: center middle; background: $background 70%;}
    PreviewDialog > Vertical {width: 100; max-width: 96%; height: 90%; border: round $accent; padding: 1; background: $surface;}
    PreviewDialog VerticalScroll {height: 1fr;}
    PreviewDialog Static {height: auto;}
    PreviewDialog TextArea {height: 20; min-height: 8;}
    PreviewDialog Horizontal {height: 3;}
    '''
    BINDINGS=[('escape','back','Отмена')]
    def __init__(self,value,*,title='Проверьте конфигурацию',accept='Запустить'):
        super().__init__();self.value=value;self.heading=title;self.accept=accept
    def compose(self):
        with Vertical():
            yield Label(self.heading)
            with VerticalScroll():
                configuration=self.value.get('configuration',{})
                lines=[configuration.get('mode','Продолжение Run')+' · '+configuration.get('goal','')]
                model=self.value.get('model',{})
                if model:lines.append('Модель: '+model.get('profile',{}).get('profile',{}).get('name',model.get('model_name',''))
                    +' · context '+str(model.get('context_tokens','')))
                from api_agent.desktop.constructor import card_summary
                for card in configuration.get('cards',[]):
                    if card.get('enabled',True):lines.append(card_summary(card))
                lines.append('Покрытие: выбранные условия; PASS не доказывает выполнение всей свободной цели.')
                yield Static('\n\n'.join(lines),id='preview-summary',markup=False)
                with Collapsible(title='Полный неизменный снимок: модель, условия, ограничения и бюджеты'):
                    yield TextArea(json.dumps(self.value,ensure_ascii=False,indent=2),read_only=True,soft_wrap=True)
            with Horizontal():
                yield Button(self.accept,id='preview-accept',variant='primary')
                yield Button('Отмена',id='preview-cancel')
    def on_button_pressed(self,event):self.dismiss(event.button.id=='preview-accept')
    def action_back(self):self.dismiss(False)


class HumanDialog(ModalScreen):
    DEFAULT_CSS='''
    HumanDialog {align: center middle; background: $background 70%;}
    HumanDialog > VerticalScroll {width: 100; max-width: 96%; height: 90%; border: round $accent; padding: 1; background: $surface;}
    HumanDialog TextArea {height: 1fr; min-height: 8;}
    HumanDialog Horizontal {height: auto;}
    '''
    BINDINGS=[('escape','back','Отказ')]
    def __init__(self,request):super().__init__();self.request=request
    def compose(self):
        kind=self.request['kind'];payload=self.request['payload']
        with VerticalScroll():
            yield Label({'ask':'Уточнение','private_input':'Приватный ввод','approval':'Подтвердите точное действие','acceptance':'Подтверждение'}.get(kind,kind))
            yield TextArea(json.dumps(payload,ensure_ascii=False,indent=2),read_only=True)
            if kind in {'ask','private_input'}:
                yield Input(password=kind=='private_input',id='human-value')
            with Horizontal():
                yield Button('Ответить' if kind in {'ask','private_input'} else 'Разрешить',id='human-yes',variant='primary')
                yield Button('Отказать',id='human-no')
    def on_button_pressed(self,event):
        if event.button.id=='human-no':self.action_back();return
        kind=self.request['kind']
        value=self.query_one('#human-value',Input).value if kind in {'ask','private_input'} else 'yes'
        if kind=='private_input':self.query_one('#human-value',Input).value=''
        if kind=='ask':
            try:value=json.loads(value)
            except ValueError:pass
        self.dismiss(value)
    def action_back(self):
        if self.request['kind']=='private_input':self.query_one('#human-value',Input).value=''
        self.dismiss('no' if self.request['kind'] in {'approval','acceptance'} else None)

"""Русские формы настроек и компактные диалоги действий."""
import json
import uuid
from pathlib import Path
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Collapsible, Input, Label, OptionList, Select, Static, TextArea
from textual.widgets.option_list import Option
from api_agent.desktop.widgets import PageDialog, JsonCode
from api_agent.desktop.presentation import approval_summary, conditions_summary, error_text

EXAMPLES={'spec_path':r'C:\api\openapi.json','policy_path':r'C:\api\policy.json',
 'mappings_path':r'C:\api\mappings.json','history_path':r'C:\api\history.sqlite3',
 'gguf':r'C:\models\model.gguf','executable':r'C:\llama\llama-server.exe',
 'target':'http://127.0.0.1:18080','endpoint':'http://127.0.0.1:18081','name':'Моя локальная модель','label':'Название вашего API'}
HELP={'spec_path':'Схема API: OpenAPI 3 JSON. Swagger 2.0 требует преобразования.',
 'policy_path':'Пусто — чтение разрешено, запись по подтверждению, удаление запрещено.',
 'mappings_path':'Настройки входа и извлечения значений. Для публичного API можно оставить пустым.',
 'history_path':'Пусто — база истории рядом с настройками приложения.',
 'gguf':'Файл весов модели. Приложение загрузит её в фоне.',
 'executable':'Исполняемый файл поддерживаемой сборки llama-server.',
 'context_tokens':'Ответ использует часть этого окна. Значение должно поддерживаться сервером.'}


class FormDialog(PageDialog):
    def __init__(self,title,fields,*,values=None,id=None):
        super().__init__(id=id);self.dialog_title=title;self.fields=fields;self.values=values or {}
    def compose(self)->ComposeResult:
        with Vertical(classes='page-shell'):
            yield Label(self.dialog_title,classes='page-title')
            with VerticalScroll(classes='page-body'):
                for key,label,default,options in self.fields:
                    yield Label(label,id='label-'+key)
                    value=self.values.get(key,default)
                    if options:yield Select(options,value=value,allow_blank=False,id='field-'+key)
                    else:yield Input(str(value) if value is not None else '',placeholder=EXAMPLES.get(key,''),id='field-'+key)
                    yield Static(HELP.get(key,''),id='help-'+key,classes='field-help')
                    yield Static('',id='error-'+key,classes='form-error')
                yield Static('',id='form-error',classes='form-error')
            with Horizontal(classes='page-actions'):
                yield Button('Сохранить',id='form-save',variant='primary');yield Button('Отмена',id='form-cancel')
    def on_mount(self):self.refresh_kind()
    def on_select_changed(self,event):
        if self.is_mounted:self.refresh_kind()
    def refresh_kind(self):
        choice=self.query('#field-kind')
        if not choice:return
        gguf=choice.first().value=='gguf'
        for key in ['endpoint','model_name','gguf','executable','port','gpu_layers']:
            visible=gguf if key in {'gguf','executable','port','gpu_layers'} else not gguf
            for prefix in ['field-','label-','help-','error-']:
                for widget in self.query('#'+prefix+key):widget.display=visible
    def on_button_pressed(self,event):
        event.stop()
        if event.button.id=='form-cancel':self.dismiss(None)
        elif event.button.id=='form-save':
            values={key:self.query_one('#field-'+key).value for key,*_ in self.fields};errors={}
            for key in ['label','name','spec_path']:
                if key in values and not str(values[key]).strip():errors[key]='Заполните это поле'
            for key in ['target','endpoint']:
                if key in values and (key!='endpoint' or values.get('kind')!='gguf'):
                    try:
                        from api_agent.transport.http import loopback_origin
                        loopback_origin(values[key])
                    except ValueError:errors[key]='Укажите локальный HTTP-адрес с портом, например http://127.0.0.1:18080'
            for key in ['spec_path','policy_path','mappings_path']:
                if values.get(key) and not Path(values[key]).is_file():errors[key]='Файл не найден. Укажите полный локальный путь.'
            if values.get('spec_path') and 'spec_path' not in errors:
                try:
                    from api_agent.openapi import load_source
                    catalog=load_source(values['spec_path'],target=values.get('target'))
                except (ValueError,OSError) as error:errors['spec_path']=error_text(error)
                else:
                    if values.get('policy_path') and 'policy_path' not in errors:
                        try:
                            from api_agent.policy.config import ProjectConfig
                            policy=ProjectConfig.load(values['policy_path'],catalog)
                            if 'target' not in errors and policy.snapshot['target']!=loopback_origin(values['target'])[0]:raise ValueError('Адрес API отличается от адреса в файле прав')
                        except (ValueError,OSError) as error:errors['policy_path']=error_text(error)
            if values.get('mappings_path') and 'mappings_path' not in errors:
                try:
                    mappings=json.loads(Path(values['mappings_path']).read_text(encoding='utf-8-sig'))
                    if not isinstance(mappings,dict):raise ValueError('Ожидается JSON-объект настроек входа')
                except (ValueError,OSError) as error:errors['mappings_path']=error_text(error)
            if values.get('kind')=='gguf':
                for key in ['gguf','executable']:
                    if not Path(values.get(key,'')).is_file():errors[key]='Укажите существующий файл'
            for key,minimum,maximum in [('context_tokens',256,1048576),('output_tokens',1,1048575),('port',1,65535),('gpu_layers',0,999)]:
                if key in values:
                    try:
                        number=int(values[key])
                        if not minimum<=number<=maximum:raise ValueError()
                    except (ValueError,TypeError):errors[key]=f'Введите целое число от {minimum} до {maximum}'
            if 'context_tokens' in values and 'output_tokens' in values and not errors:
                if int(values['output_tokens'])>=int(values['context_tokens']):errors['output_tokens']='Ответ должен быть меньше окна контекста'
            for key,*_ in self.fields:self.query_one('#error-'+key,Static).update(errors.get(key,''))
            if errors:self.query_one('#field-'+next(iter(errors))).focus();return
            self.dismiss(values)


MODEL_FIELDS=[('name','Название конфигурации','',None),
 ('kind','Источник','server',[('Локальный сервер','server'),('Файл GGUF','gguf')]),
 ('endpoint','Адрес сервера','http://127.0.0.1:18081',None),('model_name','Имя модели (пусто — определить)','',None),
 ('gguf','Файл GGUF','',None),('executable','Сборка llama-server','',None),('port','Порт управляемого сервера',18085,None),
 ('context_tokens','Контекст, токенов',8192,None),('output_tokens','Максимум ответа, токенов',1024,None),('gpu_layers','Слоёв на GPU (0 — CPU)',0,None)]
PROJECT_FIELDS=[('label','Название проекта','',None),('target','Адрес API','http://127.0.0.1:18080',None),
 ('spec_path','Файл OpenAPI 3 JSON','',None),('policy_path','Файл прав операций (необязательно)','',None),
 ('mappings_path','Настройки входа и извлечения значений (необязательно)','',None),('history_path','Файл истории (необязательно)','',None)]


class ModelSettings(PageDialog):
    def __init__(self,store):super().__init__(id='model-settings');self.store=store
    def compose(self):
        with Vertical(classes='page-shell'):
            yield Label('Модели приложения',classes='page-title')
            yield Static('Добавьте конфигурацию и загрузите модель. Выбор модели для новой цели — рядом с запуском.')
            with VerticalScroll(classes='page-body'):
                yield OptionList(*(Option(r['profile']['name']+' · '+('GGUF' if r['profile']['kind']=='gguf' else 'сервер'),id=r['id']) for r in self.store.models()),id='model-list')
            with Horizontal(classes='page-actions'):
                yield Button('Добавить',id='model-add',variant='primary');yield Button('Изменить',id='model-edit')
                yield Button('Загрузить / проверить',id='model-check');yield Button('Назад',id='model-back')
    def on_button_pressed(self,event):
        event.stop();key=event.button.id
        if key=='model-back':self.dismiss(None)
        elif key=='model-add':self.dismiss(('edit',uuid.uuid4().hex,None))
        elif key in {'model-edit','model-check'}:
            options=self.query_one('#model-list',OptionList)
            if options.highlighted is None:return
            record=self.store.model(options.get_option_at_index(options.highlighted).id)
            self.dismiss(('check' if key=='model-check' else 'edit',record['id'],record['profile']))


class ChoiceDialog(PageDialog):
    DEFAULT_CSS='''ChoiceDialog {align: center middle; background: $background 75%;}
    ChoiceDialog > .page-shell {width: 65; max-width: 98%; height: auto; max-height: 95%; border: solid $accent;}
    ChoiceDialog OptionList {height: auto; max-height: 20;}'''
    def __init__(self,title,options,*,id=None):super().__init__(id=id);self.heading=title;self.options=options
    def compose(self):
        with Vertical(classes='page-shell'):
            yield Label(self.heading,classes='page-title')
            yield OptionList(*(Option(label,id=str(key) if key else str(i)) for i,(key,label) in enumerate(self.options)),id='choices')
            yield Button('Назад',id='choice-back')
    def on_option_list_option_selected(self,event):event.stop();self.dismiss(event.option.id)
    def on_button_pressed(self,event):event.stop();self.dismiss(None)


class PreviewDialog(PageDialog):
    def __init__(self,value,*,title='Сводка запуска',accept='Запустить'):
        super().__init__();self.value=value;self.heading=title;self.accept=accept
    def compose(self):
        manifest=self.value.get('configuration',{})
        with Vertical(classes='page-shell'):
            yield Label(self.heading,classes='page-title')
            with VerticalScroll(classes='page-body'):
                yield Static('Цель: '+manifest.get('goal','Продолжение исходной цели'),markup=False)
                yield Static(conditions_summary(manifest),id='preview-summary',markup=False)
            with Horizontal(classes='page-actions'):
                yield Button(self.accept,id='preview-accept',variant='primary');yield Button('Отмена',id='preview-cancel')
    def on_button_pressed(self,event):event.stop();self.dismiss(event.button.id=='preview-accept')
    def action_back(self):self.dismiss(False)


class HumanDialog(PageDialog):
    DEFAULT_CSS='''HumanDialog {align: center middle; background: $background 70%;}
    HumanDialog > .page-shell {width: 100; max-width: 100%; height: auto; max-height: 100%; border: solid $accent;}
    HumanDialog .page-body {height: auto; max-height: 65%;}'''
    def __init__(self,request):super().__init__();self.request=request
    def compose(self):
        kind=self.request['kind'];payload=self.request['payload']
        with Vertical(classes='page-shell'):
            yield Label({'ask':'Уточнение','private_input':'Ввод учётных данных','approval':'Разрешить действие?','acceptance':'Подтверждение'}.get(kind,'Подтверждение'),classes='page-title')
            with VerticalScroll(classes='page-body'):
                if kind=='approval':
                    yield Static(approval_summary(payload),markup=False)
                    preview=payload.get('preview',{})
                    if preview.get('parameters'):
                        yield Label('Параметры запроса');yield JsonCode(preview['parameters'])
                    if 'json' in preview:
                        yield Label('Тело запроса');yield JsonCode(preview['json'])
                    if preview.get('credential_body_omitted'):yield Static('Используются настроенные учётные данные API.')
                    with Collapsible(title='Подробнее о разрешении',collapsed=True):
                        yield Static('Операция: '+str(payload.get('operation_id',''))+'\nВерсия политики: '+str(payload.get('config_version',''))+'\nРазрешение относится только к показанному действию.',markup=False)
                else:yield Static(str(payload.get('question') or payload.get('message') or 'Уточните значение для продолжения'),markup=False)
                if kind in {'ask','private_input'}:yield Input(password=kind=='private_input',id='human-value')
            with Horizontal(classes='page-actions'):
                yield Button('Отказать',id='human-no')
                yield Button('Ответить' if kind in {'ask','private_input'} else 'Разрешить',id='human-yes',variant='primary')
    def on_mount(self):self.query_one('#human-no').focus()
    def on_button_pressed(self,event):
        event.stop()
        if event.button.id=='human-no':self.action_back();return
        kind=self.request['kind'];value=self.query_one('#human-value',Input).value if kind in {'ask','private_input'} else 'yes'
        if kind=='private_input':self.query_one('#human-value',Input).value=''
        if kind=='ask':
            try:value=json.loads(value)
            except ValueError:pass
        self.dismiss(value)
    def action_back(self):
        if self.request['kind']=='private_input':self.query_one('#human-value',Input).value=''
        self.dismiss('no' if self.request['kind'] in {'approval','acceptance'} else None)

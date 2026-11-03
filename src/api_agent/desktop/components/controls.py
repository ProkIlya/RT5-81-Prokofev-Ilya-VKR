"""Компактные controls и единые цвета действий, без изменения их команд."""
import re
from rich.text import Text
from textual.widgets import Button as BaseButton, Input as BaseInput, Select as BaseSelect, Static

COLORS={'ALLOW':'green','CONFIRM':'yellow','DENY':'red','HIDE':'magenta',
        'PASS':'green','FAIL':'red','UNKNOWN':'yellow','N/A':'cyan'}


def highlighted(value):
    text=Text(str(value))
    text.highlight_regex(r'(?m)^([^\n:]{1,65}:)',style='bold')
    for word,color in {**COLORS,'GET':'green','HEAD':'green','POST':'bright_cyan',
            'PUT':'yellow','PATCH':'yellow','DELETE':'red',
            'Разрешено':'green','Запрещено':'red','Подтверждение':'yellow','Скрыто':'magenta',
            'Удалить':'red','Добавить':'green','Изменить':'yellow','Переименовать':'yellow'}.items():
        text.highlight_regex(r'(?i)\b'+re.escape(word)+r'\b',style=color)
    text.highlight_regex(r'(?<!\w)/[\w/{}/~.\-]+',style='cyan')
    text.highlight_regex(r'https?://[^\s]+',style='underline cyan')
    text.highlight_regex(r'"[^"\n]*"',style='green')
    text.highlight_regex(r'\b(?:true|false|null)\b',style='magenta')
    for prefix,color in [('2','green'),('4','yellow'),('5','red')]:
        text.highlight_regex(r'\b'+prefix+r'\d\d\b',style='bold '+color)
    text.highlight_regex(r'(?m)^(Цель|Условия|Результаты|Ограничения|Модель|Источники).*$',style='bold cyan')
    return text


class Summary(Static):
    """Безопасное Rich-представление: пользовательский текст не разбирается как markup."""
    def __init__(self,content='',**kwargs):
        kwargs['markup']=False
        super().__init__(highlighted(content) if isinstance(content,str) else content,**kwargs)
        if isinstance(content,str) and any(self.has_class(name) for name in ['field-help','form-error','editor-error']):
            self.display=bool(content)
    def update(self,content=''):
        if isinstance(content,str):
            if any(self.has_class(name) for name in ['field-help','form-error','editor-error']):
                self.display=bool(content)
            content=highlighted(content)
        return super().update(content)


class Input(BaseInput):
    def __init__(self,*args,**kwargs):
        kwargs.setdefault('compact',True);super().__init__(*args,**kwargs)


class Select(BaseSelect):
    @staticmethod
    def labels(options):
        return [(highlighted(label) if isinstance(label,str) else label,value) for label,value in options]
    def __init__(self,options,*args,**kwargs):
        kwargs.setdefault('compact',True);super().__init__(self.labels(options),*args,**kwargs)
    def set_options(self,options):return super().set_options(self.labels(options))


class Button(BaseButton):
    def __init__(self,label=None,*args,**kwargs):
        title=str(label or '');key=kwargs.get('id','') or ''
        icon={'Добавить':'+','Изменить':'✎','Удалить':'×','Назад':'←',
            'Изменить выбранную':'✎','Удалить выбранную':'×',
            '⧉ Ответ':'⧉','⧉ JSON-трейс':'{ }','⧉ Условия':'☑ JSON',
            '⧉ Условия в JSON':'⧉ JSON'}.get(title,title)
        kwargs.setdefault('tooltip',title);kwargs.setdefault('compact',True);kwargs.setdefault('flat',True)
        super().__init__(icon,*args,**kwargs)
        self.add_class('action-delete' if 'delete' in key or 'remove' in key or key=='human-no'
            else 'action-edit' if 'edit' in key or 'toggle' in key else 'action-add' if 'add' in key else 'action-neutral')

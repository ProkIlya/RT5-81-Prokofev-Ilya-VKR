"""Пользовательский текст ошибки отделён от неизменного технического кода."""
import json
import re
from api_agent.desktop.presentation.diagnostic_catalog import MESSAGES

CODE=re.compile(r'\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b')
FALLBACK='Не удалось выполнить действие. Проверьте введённые данные и настройки. Если ошибка повторяется, сообщите о ней разработчику.'


def error_text(value):
    # Тексты исключений ОС могут содержать пути/сетевые данные; не копируем их.
    for kind in (FileNotFoundError,PermissionError,ConnectionError,TimeoutError,OSError,json.JSONDecodeError):
        if isinstance(value,kind):return MESSAGES[kind.__name__]
    text=str(value).strip() if value is not None else ''
    code=getattr(value,'code',None)
    if isinstance(code,str) and code in MESSAGES:return MESSAGES[code]
    if text in MESSAGES:return MESSAGES[text]
    if isinstance(value,KeyError):return MESSAGES['KeyError']
    if not text:return 'Причина не указана.'
    if any('А'<=ch<='я' or ch in 'Ёё' for ch in text):
        # Повторный вызов formatter для готового сообщения не возвращает код.
        return CODE.sub(lambda m:MESSAGES.get(m.group(),m.group()),text)
    # Обёртки форм могут добавлять английский префикс. Показываем только
    # объяснение известного кода, без префикса и остального exception payload.
    for match in CODE.finditer(text):
        if match.group() in MESSAGES:return MESSAGES[match.group()]
    # Для ещё не зарегистрированного кода не показываем raw exception payload.
    return FALLBACK

"""Русское представление сохранённых данных; ни ожиданий, ни evidence не меняет."""
import json
from rich.text import Text

METHOD_COLORS={'GET':'green','HEAD':'green','POST':'bright_cyan','PUT':'yellow','PATCH':'yellow','DELETE':'red'}
PREDICATES={'equals':'равно','not_equals':'не равно','exists':'существует','schema':'соответствует схеме',
    'number_rounded':'число с округлением','number_within':'число с допуском','collection':'коллекция'}
SELECTORS={'first':'первый подходящий','last':'последний подходящий','all':'все подходящие'}
AUTH={'any':'без ограничения авторизации','current':'текущая учётная запись','anonymous':'без авторизации'}
LIMITS={'max_requests':'HTTP-запросы','max_steps':'Шаги агента','max_seconds':'Активное время, с',
    'max_tokens_total':'Всего токенов','max_context_tokens':'Контекст, токенов','max_output_tokens':'Ответ, токенов',
    'malformed_retries':'Повторы некорректного ответа','max_prompt_bytes':'Размер входа, байт',
    'max_empty_steps':'Пустые шаги','max_repeats':'Повторы действий'}
from api_agent.desktop.presentation.diagnostic_catalog import MESSAGES as ERRORS
from api_agent.desktop.presentation.diagnostic_text import error_text


def duration(value):
    if value is None:return 'не сохранено'
    return (f'{value:.3f} с').replace('.',',')


def method_text(method):return Text(method,style=METHOD_COLORS.get(method,''))


def literal(value):
    return json.dumps(value,ensure_ascii=False)


def assertion_summary(item):
    kind=item['predicate'];path=item.get('path','') or 'всё JSON-тело'
    label=path+' — '+PREDICATES.get(kind,kind)
    if 'value' in item:label+=' '+literal(item['value'])
    if 'expected' in item:label+=' '+str(item['expected'])
    if 'tolerance' in item:label+=' ± '+str(item['tolerance'])
    if kind=='collection':label+=' ('+('содержит' if item.get('mode')=='includes' else 'точное совпадение')+'): '+literal(item.get('rows',[]))
    return label


def historical_sources(value,attachments):
    return [str(field.get('path',''))+' · Run '+str(field.get('source',{}).get('run_id',attachment.get('origin',{}).get('run_id','')))[:8]
        for attachment in attachments or [] for field in attachment.get('fields',[])
        if type(field.get('value')) is type(value) and field.get('value')==value]


def sources_summary(card,attachments=()):
    sources=[]
    for fields in card.get('where',{}).values():
        for value in fields.values():
            if isinstance(value,dict) and 'response' in value:
                ref=value['response'];sources.append(ref['operation_id']+' → '+ref['path'])
            else:sources.extend(historical_sources(value,attachments))
    return '; '.join(dict.fromkeys(sources)) or 'Указанные значения'


def scope_summary(where,attachments=()):
    lines=[]
    for location,fields in where.items():
        for key,value in fields.items():
            if isinstance(value,dict) and 'response' in value:
                src=value['response'];text='из ответа '+src['operation_id']+', поле '+src['path']
                text+=', '+('первый' if src.get('occurrence')=='first' else 'последний')+' ответ этого запуска'
                if src.get('where'):text+='; '+scope_summary(src['where'])
            else:
                text=literal(value)
                origins=historical_sources(value,attachments)
                text+=' · совпадает с прикреплённым полем '+', '.join(origins) if origins else ' · указанное значение'
            lines.append(('Параметр ' if location=='parameters' else 'Поле запроса ')+key+' = '+text)
    return '; '.join(lines) or 'Дополнительный отбор запросов не задан'


def card_summary(card,attachments=()):
    lines=['Карточка: '+(card.get('name') or card['operation_id']).strip(),
        'Ожидается HTTP '+', '.join(map(str,card['statuses']))+'; '+SELECTORS.get(card.get('selector','last'),'последний подходящий'),
        AUTH.get(card.get('auth','any'),'без ограничения авторизации'),scope_summary(card.get('where',{}),attachments)]
    lines.extend('  • '+assertion_summary(x) for x in card.get('assertions',[]))
    if card.get('negative'):
        n=card['negative'];lines.append('Намеренно неверный запрос: '+n['path']+' — '+{'required':'пропустить обязательное поле','schema_type':'неверный тип','enum':'значение вне списка'}.get(n['rule'],n['rule']))
    return '\n'.join(lines)


def conditions_summary(manifest):
    cards=[c for c in manifest.get('cards',[]) if c.get('enabled',True)]
    return '\n\n'.join(card_summary(c,manifest.get('attachments',[])) for c in cards) if cards else 'Пользовательские проверки не заданы.'


def approval_summary(payload):
    preview=payload.get('preview',{})
    return '\n'.join([preview.get('method',payload.get('method',''))+' '+preview.get('path',payload.get('path','')),
        'API: '+str(payload.get('target','')),
        'Учётная запись: '+('текущая' if payload.get('request_principal',{}).get('mode')=='authenticated' else 'без авторизации')])

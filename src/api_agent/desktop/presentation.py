"""Русское представление сохранённых данных; ни ожиданий, ни evidence не меняет."""
import json
from rich.text import Text

METHOD_COLORS={'GET':'green','HEAD':'green','POST':'dodger_blue2','PUT':'yellow','PATCH':'yellow','DELETE':'red'}
PREDICATES={'equals':'равно','not_equals':'не равно','exists':'существует','schema':'соответствует схеме',
    'number_rounded':'число с округлением','number_within':'число с допуском','collection':'коллекция'}
SELECTORS={'first':'первый подходящий','last':'последний подходящий','all':'все подходящие'}
AUTH={'any':'без ограничения авторизации','current':'текущая учётная запись','anonymous':'без авторизации'}
LIMITS={'max_requests':'HTTP-запросы','max_steps':'Шаги агента','max_seconds':'Активное время, с',
    'max_tokens_total':'Всего токенов','max_context_tokens':'Контекст, токенов','max_output_tokens':'Ответ, токенов',
    'malformed_retries':'Повторы некорректного ответа','max_prompt_bytes':'Размер входа, байт',
    'max_empty_steps':'Пустые шаги','max_repeats':'Повторы действий'}
ERRORS={'model_identity_unavailable':'Сервер модели недоступен или не удалось проверить модель. Загрузите модель в настройках.',
    'model_selection_required':'Выберите модель рядом с кнопкой запуска.',
    'model_runtime_unavailable':'Сервер модели недоступен. Проверьте его запуск.',
    'model_identity_changed':'Модель изменилась. Для продолжения требуется исходная модель запуска.',
    'selected_empty_testing':'Включите хотя бы одну карточку проверки.',
    'selected_invalid_goal':'Введите непустую цель.', 'selected_negative_status':'Для намеренно неверного запроса укажите ожидаемый статус 4xx.',
    'selected_submission_changed':'Черновик изменился. Отправьте цель заново.',
    'selected_policy_denied':'Операция запрещена политикой проекта.',
    'desktop_run_busy':'Дождитесь завершения текущего запуска.',
    'approval_rejected':'Действие отменено: вы отклонили разрешение.',
    'finished':'Запуск завершён','cancelled':'Запуск остановлен пользователем',
    'uncertain_write':'Результат записи неизвестен. Повтор без проверки запрещён.',
    'constructor_import_size':'Файл условий слишком большой (не более 64 КиБ).',
    'constructor_invalid_json':'Некорректный JSON. Проверьте кавычки, запятые и скобки.',
    'constructor_unsupported_version':'Неподдерживаемая версия файла условий.',
    'constructor_stale_spec':'Условия относятся к другой версии OpenAPI.',
    'attachment_unfinished_run':'Прикреплять можно только данные завершённого запуска.',
    'report_snapshot_changed':'Целостность отчёта нарушена. Просмотр остановлен.',
    'model_in_use':'Модель занята текущим запуском.',
    'model_context_unavailable':'У сервера недостаточное окно контекста.',
    'model_process_failed':'Не удалось запустить модель. Проверьте GGUF и сборку llama-server.',
    'model_port_in_use':'Порт модели уже занят другим процессом.',
    'model_identity_file_missing':'Файл модели или llama-server не найден.',
    'model_identity_not_gguf':'Выбранный файл не является GGUF-моделью.',
    'model_loading_timeout':'Истекло время загрузки модели.',
    'service_unavailable':'API недоступен или не прошёл проверку готовности. Проверьте подключение и настройки health-check.',
    'health_configuration_required':'Для продолжения настройте health-check API в файле подключений проекта.',
    'continuation_stale_policy':'Политика изменилась. Продолжение со старым снимком недоступно; создайте новую цель.',
    'continuation_model_identity_unproven':'У запуска нет подтверждённого снимка модели. Создайте новую цель.',
    'continuation_budget_exhausted':'Общий бюджет цепочки запусков исчерпан. Продолжение недоступно.',
    'selected_invalid_statuses':'Укажите HTTP-статусы числами от 100 до 599.',
    'selected_invalid_value':'Значение не соответствует типу поля.',
    'selected_duplicate_card':'Внутренние идентификаторы карточек должны различаться.',
    'selected_invalid_collection':'Проверьте JSON коллекции и режим сопоставления.',
    'selected_invalid_row_value':'Значение элемента коллекции не соответствует схеме.',
    'selected_unsupported_negative':'Это нарушение схемы запроса не поддерживается.',
    'config_scope_changed':'Политика изменилась во время запуска. Действие остановлено.'}


def error_text(value):
    text=str(value)
    if text in ERRORS:return ERRORS[text]
    if any('А'<=ch<='я' or ch in 'Ёё' for ch in text):return text
    return 'Не удалось выполнить действие. Диагностический код: '+text[:180]


def duration(value):
    if value is None:return 'не сохранено'
    return (f'{value:.3f} с').replace('.',',')


def method_text(method):return Text(method,style=METHOD_COLORS.get(method,'cyan'))


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


def scope_summary(where):
    lines=[]
    for location,fields in where.items():
        for key,value in fields.items():
            if isinstance(value,dict) and 'response' in value:
                src=value['response'];text='из ответа '+src['operation_id']+', поле '+src['path']
                text+=', '+('первый' if src.get('occurrence')=='first' else 'последний')+' ответ этого запуска'
                if src.get('where'):text+='; '+scope_summary(src['where'])
            else:text=literal(value)
            lines.append(('Параметр ' if location=='parameters' else 'Поле запроса ')+key+' = '+text)
    return '; '.join(lines) or 'Дополнительный отбор запросов не задан'


def card_summary(card):
    lines=[(card.get('name') or card['operation_id']).strip(),
        'Ожидается HTTP '+', '.join(map(str,card['statuses']))+'; '+SELECTORS.get(card.get('selector','last'),'последний подходящий'),
        AUTH.get(card.get('auth','any'),'без ограничения авторизации'),scope_summary(card.get('where',{}))]
    lines.extend('  • '+assertion_summary(x) for x in card.get('assertions',[]))
    if card.get('negative'):
        n=card['negative'];lines.append('Намеренно неверный запрос: '+n['path']+' — '+{'required':'пропустить обязательное поле','schema_type':'неверный тип','enum':'значение вне списка'}.get(n['rule'],n['rule']))
    return '\n'.join(lines)


def conditions_summary(manifest):
    cards=[c for c in manifest.get('cards',[]) if c.get('enabled',True)]
    return '\n\n'.join(card_summary(c) for c in cards) if cards else 'Пользовательские проверки не заданы.'


def approval_summary(payload):
    preview=payload.get('preview',{})
    return '\n'.join([preview.get('method',payload.get('method',''))+' '+preview.get('path',payload.get('path','')),
        'API: '+str(payload.get('target','')),
        'Учётная запись: '+('текущая' if payload.get('request_principal',{}).get('mode')=='authenticated' else 'без авторизации')])

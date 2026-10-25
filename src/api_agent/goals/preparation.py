"""Ограниченные исправления структуры до принятия цели и первого HTTP."""
from copy import deepcopy
import json


# Ошибки authority/source/identity, вопросы человеку и acceptance сюда не входят.
REPAIRABLE_ERRORS = frozenset({
    'goal_invalid_dependency', 'goal_unsupported_path', 'goal_unsupported_response',
    'goal_invalid_parameters', 'goal_missing_parameter', 'goal_invalid_parameter_type',
    'goal_invalid_body', 'goal_invalid_collection', 'goal_collection_scope_required',
    'goal_invalid_status', 'goal_invalid_decimal_type', 'goal_invalid_decimal_expectation',
})
MAX_REPAIRS = 2


def repair_payload(original, generated_message, code, remaining, *, details=None):
    """Стабильный префикс и только последнее предложение, без reasoning history."""
    payload = deepcopy(original)
    feedback = json.dumps({'compiler_error': code, 'details': details, 'repairs_remaining': remaining,
        'instruction': 'Исправь полный план по исходной цели и operations. HTTP ещё не выполнялся. '
                       'Не меняй цель, ожидаемые значения или основания. Верни полный исправленный JSON.'},
        ensure_ascii=False)
    if 'response_format' in payload:
        payload['messages'] += [{'role': 'assistant', 'content': generated_message['content']},
                                {'role': 'user', 'content': feedback}]
    else:
        calls = deepcopy(generated_message['tool_calls'])
        payload['messages'] += [{'role': 'assistant', 'tool_calls': calls},
            {'role': 'tool', 'tool_call_id': calls[0]['id'], 'content': feedback}]
    return payload

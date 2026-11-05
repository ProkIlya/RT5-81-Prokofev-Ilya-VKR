"""Измеряемая проекция одного Run; полная история и evidence остаются у host."""
from copy import deepcopy
import json

from api_agent.agent.response_views import response_page
from api_agent.agent.resilience import BudgetViolation


def _groups(messages):
    """Не разрывать native call/results; неизвестную форму не чинить молча."""
    groups = []
    index = 0
    while index < len(messages):
        message = messages[index]
        if message.get('tool_calls'):
            calls = message['tool_calls']
            ids = [call['id'] for call in calls]
            results = messages[index + 1:index + 1 + len(ids)]
            if (len(ids) != len(set(ids)) or len(results) != len(ids)
                    or any(m.get('role') != 'tool' for m in results)
                    or {m.get('tool_call_id') for m in results} != set(ids)):
                return None
            groups.append(messages[index:index + 1 + len(ids)])
            index += 1 + len(ids)
        elif message.get('role') == 'tool':
            return None
        else:
            groups.append([message]); index += 1
    return groups


def _count(counter, payload):
    try: value = counter(deepcopy(payload))
    except Exception: raise BudgetViolation('token_preflight_error') from None
    if type(value) is not int or value < 1: raise BudgetViolation('token_preflight_error')
    return value


def _project_dynamic_state(prefix, memory):
    """История вызовов уже присутствует в полной рабочей проекции один раз."""
    changed = False
    for message in prefix:
        if message.get('role') != 'user': continue
        try: task = json.loads(message.get('content', ''))
        except (ValueError, TypeError): continue
        if not isinstance(task, dict) or 'goal' not in task: continue
        state = task.get('state')
        if (not isinstance(state, dict) or not isinstance(state.get('api_calls'), list)
                or not isinstance(memory.get('api_calls'), list)
                or len(state['api_calls']) != len(memory['api_calls'])): continue
        state.pop('api_calls')
        state['api_call_history'] = {'source':'working_memory.api_calls','complete':True}
        message['content'] = json.dumps(task, ensure_ascii=False, separators=(',', ':'))
        changed = True
    return changed


def _project_results(groups, saved_memory):
    projected = set()
    for group in groups:
        for message in group[1:]:
            content = message.get('content', '')
            if len(content.encode('utf-8')) <= 2048:
                continue
            if _project_result(message, content, saved_memory):
                projected.add(id(message))
    return projected


def _project_result(message, content, saved_memory):
    try:
        value = json.loads(content)
    except (ValueError, TypeError):
        value = content
    replaced = []
    if (isinstance(value, dict) and {'check', 'responses'} <= value.keys()
            and {'check', 'auth_session', 'api_calls'} <= saved_memory.keys()):
        replaced = [key for key in ('check', 'state', 'responses') if key in value]
        value = {key: item for key, item in value.items() if key not in replaced}
    shortened = json.dumps({'context_truncated': True,
        'original_bytes': len(content.encode('utf-8')), 'projection': response_page(value, max_bytes=1024),
        'sections_in_working_memory': replaced,
        'hint': 'Inspect complete responses in assigned Python variables using execute_code; '
            'do not repeat completed writes to recover output.'},
        ensure_ascii=False, separators=(',', ':'))
    if len(shortened.encode('utf-8')) >= len(content.encode('utf-8')):
        return False
    message['content'] = shortened
    return True


def compact_payload(payload, *, prefix_count, memory, counter, context_tokens):
    """По измеренному порогу сократить ответы и затем убрать старые группы."""
    if counter is None:
        return payload, None
    groups = _groups(payload['messages'][prefix_count:])
    if groups is None:
        return payload, None
    before = _count(counter, payload)
    output_reserve = payload['max_tokens']
    threshold = max(1, (context_tokens - output_reserve) * 3 // 4)
    if before < threshold:
        return payload, None

    info = {'trigger': 'input_threshold', 'input_tokens_before': before,
        'input_tokens_after': before, 'threshold_tokens': threshold,
        'output_reserve': output_reserve, 'removed_message_groups': 0, 'removed_messages': 0,
        'removed_tool_groups': 0, 'tool_results_truncated': 0,
        'above_soft_limit': True, 'full_evidence_retained': True}
    last_tool = next((i for i in range(len(groups) - 1, -1, -1) if groups[i][0].get('tool_calls')), None)
    if last_tool is None:
        return payload, info

    groups = deepcopy(groups)
    saved_memory = memory()
    projected = _project_results(groups, saved_memory)
    prefix = deepcopy(payload['messages'][:prefix_count])
    dynamic_projected = _project_dynamic_state(prefix, saved_memory)
    evidence = [call['exchange_id'] for call in saved_memory.get('api_calls', []) if 'exchange_id' in call]
    removed_tools = removed_messages = 0
    # Последний обмен и подсказки после него принадлежат текущему действию.
    best, best_info = payload, info
    for removed in range(last_tool + 1):
        if removed:
            removed_tools += bool(groups[removed - 1][0].get('tool_calls'))
            removed_messages += len(groups[removed - 1])
        retained = [message for group in groups[removed:] for message in group]
        projected_count = sum(id(message) in projected for message in retained)
        history = {'removed_tool_groups': removed_tools, 'tool_results_truncated': projected_count,
            'dynamic_api_history_projected': dynamic_projected, 'full_evidence_retained': True}
        memory_message = {'role': 'user', 'content': json.dumps({'working_memory': saved_memory,
            'history_projection': history}, ensure_ascii=False, separators=(',', ':'))}
        candidate = {**payload, 'messages': [*prefix, memory_message, *retained]}
        after = _count(counter, candidate)
        if after < best_info['input_tokens_after']:
            best = candidate
            best_info = {**info, 'input_tokens_after': after, 'removed_message_groups': removed,
                'removed_messages': removed_messages, 'removed_tool_groups': removed_tools,
                'tool_results_truncated': projected_count, 'above_soft_limit': after > threshold,
                'exchange_ids': evidence}
        if after <= threshold:
            break
    return best, best_info

"""Измеряемая проекция одного Run; полная история и evidence остаются у host."""
from copy import deepcopy
import json

from ..agent.response_views import response_page
from ..agent.resilience import BudgetViolation


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


def compact_payload(payload, *, prefix_count, memory, counter, context_tokens):
    """Сократить старые complete groups только после измеренного порога.

    System/goal/accepted checks/API metadata не сокращаются. Дублированная
    динамическая история task.state представлена один раз в working_memory.
    Последняя complete группа и последующие application hints сохраняются.
    Огромные tool results получают явную bounded projection; чтение оригинала
    хранится у host; полные присвоенные ответы остаются в Python variables.
    Это не LLM summary и не новый proof.
    Если выигрыша нет, обычный budget guard останавливает Run при переполнении.
    """
    if counter is None: return payload, None
    groups = _groups(payload['messages'][prefix_count:])
    if not groups or not any(g[0].get('tool_calls') for g in groups): return payload, None
    before = _count(counter, payload)
    threshold = max(1, (context_tokens - payload['max_tokens'] - 32) * 3 // 4)
    if before <= threshold: return payload, None
    last = max(i for i, group in enumerate(groups) if group[0].get('tool_calls'))
    removed = sum(bool(g[0].get('tool_calls')) for g in groups[:last])
    retained = deepcopy([m for group in groups[last:] for m in group])
    saved_memory = memory()
    projections = 0
    for message in retained:
        content = message.get('content', '')
        if message.get('role') != 'tool' or len(content.encode('utf-8')) <= 2048: continue
        try: value = json.loads(content)
        except (ValueError, TypeError): value = content
        replaced = []
        if (isinstance(value, dict) and {'check','responses'} <= value.keys()
                and {'check','auth_session','api_calls'} <= saved_memory.keys()):
            replaced = [k for k in ('check','state','responses') if k in value]
            value = {k:v for k,v in value.items() if k not in replaced}
        view = response_page(value, max_bytes=1024)
        message['content'] = json.dumps({'context_truncated': True,
            'original_bytes': len(content.encode('utf-8')), 'projection': view,
            'sections_in_working_memory': replaced,
            'hint': 'Inspect complete responses in assigned Python variables using execute_code; '
                'do not repeat completed writes to recover output.'},
            ensure_ascii=False, separators=(',', ':'))
        projections += 1
    result = deepcopy(payload)
    prefix = deepcopy(payload['messages'][:prefix_count])
    dynamic_projected = _project_dynamic_state(prefix, saved_memory)
    result['messages'] = prefix + [
        {'role':'user','content':json.dumps({'working_memory':saved_memory,
         'history_projection':{'removed_tool_groups':removed,'tool_results_truncated':projections,
          'dynamic_api_history_projected':dynamic_projected,
          'full_evidence_retained':True}},ensure_ascii=False,separators=(',', ':'))}] + retained
    after = _count(counter, result)
    if after >= before: return payload, None
    return result, {'input_tokens_before': before, 'input_tokens_after': after,
        'threshold_tokens': threshold, 'removed_tool_groups':removed,
        'tool_results_truncated':projections, 'full_evidence_retained':True}

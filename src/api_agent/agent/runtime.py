"""S09d3: собственный code loop; права, evidence и HTTP принадлежат broker."""
from copy import deepcopy
from dataclasses import dataclass, field
import ast
import json
import re
import tempfile
import time
from pathlib import Path

from api_agent.agent.model import ModelError, ModelTimeout, configured_goal_model, goal_sampling
from api_agent.agent.resilience import BudgetViolation, EmptyDecision, TokenBudget, GENERATION_REPAIR_HINT
from api_agent.bounded_json import validate_tree, numeric_metadata
from api_agent.openapi.parser import canonical
from api_agent.agent.response_views import response_page
from api_agent.workspace.context.context_window import compact_payload
from api_agent.workspace.context.run_memory import memory_snapshot
from api_agent.validation import fingerprint, report, schema_issues, display_value
from api_agent.workspace.execution.broker import WorkspaceBroker, WorkspaceError
from api_agent.workspace.execution.sandbox import WorkspaceSandbox
from api_agent.workspace.sdk import SdkArtifact
from api_agent.workspace.context.prompt import FINISH_SCHEMA, initial_messages, payload
from api_agent.workspace.execution.diagnostics import HINTS
from api_agent.workspace.context.state_view import execution_state


@dataclass(frozen=True)
class WorkspaceOptions:
    """Готовый Project artifact и pinned image, без сборки SDK на каждый Run."""
    artifact: SdkArtifact
    image: str
    model_name: str = field(default_factory=configured_goal_model)

    def validate(self, catalog, config):
        if not isinstance(self.artifact, SdkArtifact) or not re.fullmatch(r'sha256:[a-f0-9]{64}', self.image):
            raise ValueError('goal_workspace_configuration')
        if (self.artifact.manifest['spec_hash'] != catalog.spec_hash
            or self.artifact.manifest['config_hash'] != fingerprint(config.snapshot)):
            raise ValueError('goal_workspace_scope_mismatch')
        if not isinstance(self.model_name, str) or not 1 <= len(self.model_name.strip()) <= 200:
            raise ValueError('goal_workspace_model_identity')


def _decision(response):
    """Форма runtime не даёт модели второй интерфейс или право выполнить Python на host."""
    choices = response['choices']
    if len(choices) != 1: raise ValueError('invalid_code_call')
    message = choices[0]['message']
    calls = message.get('tool_calls') or []
    if not calls and not message.get('content'): raise EmptyDecision()
    if len(calls) != 1: raise ValueError('invalid_code_call')
    call = calls[0]
    if (set(call) != {'id', 'type', 'function'} or call['type'] != 'function'
        or type(call['id']) is not str or not 1 <= len(call['id']) <= 200
        or set(call['function']) != {'name', 'arguments'}
        or call['function']['name'] not in {'execute_code', 'check_goal', 'finish_run'}):
        raise ValueError('invalid_code_call')
    args = json.loads(call['function']['arguments'])
    name = call['function']['name']
    if type(args) is not dict:
        raise ValueError('invalid_code_call')
    if name == 'execute_code' and (set(args) != {'code'} or type(args['code']) is not str
        or not args['code'].strip() or len(args['code'].encode('utf-8')) > 16384):
        raise ValueError('invalid_code_call')
    if name == 'check_goal' and args: raise ValueError('invalid_code_call')
    if name == 'finish_run' and schema_issues(args, FINISH_SCHEMA): raise ValueError('invalid_code_call')
    return call, name, args


def _action_key(name, args, exchange_count):
    """Ключ повтора живёт только в RAM; форматирование Python не меняет действие."""
    if name == 'execute_code':
        try:
            action = ast.dump(ast.parse(args['code']), include_attributes=False)
        except SyntaxError:
            action = args['code'].strip()
    else:
        action = args
    # Новое evidence делает повторную детерминированную проверку содержательной.
    return fingerprint([name, action, exchange_count if name == 'check_goal' else None])


def _completed_writes(broker):
    """Сохранённые эффекты для коррекции, без private inputs и успешного replay."""
    from api_agent.goals.checks.compiler import PRIVATE_NAMES
    def public(value):
        if isinstance(value, dict):
            return {k: public(v) for k, v in value.items() if k.lower() not in PRIVATE_NAMES}
        if isinstance(value, list): return [public(v) for v in value]
        return value
    result = []
    for exchange in broker.run['exchanges']:
        name = exchange['operation_id']
        status = (exchange.get('response') or {}).get('status')
        if (broker.catalog.operations[name]['method'] in {'GET', 'HEAD', 'OPTIONS'}
            or exchange.get('outcome') not in {'received', 'invalid_json', 'invalid_content_type'}
            or exchange.get('truncated') or type(status) is not int or not 200 <= status < 300):
            continue
        item = {'operation_id': name, 'status': status,
            'parameters': display_value(public(exchange.get('request_parameters', {})))}
        body = (exchange.get('request') or {}).get('json')
        if body is not None: item['body'] = display_value(public(body))
        result.append(item)
    return broker.safe(result)


def _response_feedback(broker, exchange):
    """Preview сохраняет тип JSON; полное evidence и Python response не меняются."""
    response = exchange.get('response') or {}
    value = broker.safe(response.get('json'))
    result = {'operation_id': exchange['operation_id'], 'status': response.get('status'), 'body': value}
    if len(canonical(value)) > 1024:
        view = response_page(value, limit=16, max_bytes=2048)
        result['body'] = view.pop('value')
        result['body_view'] = view
    return result


def _drive(broker, worker, model, budget, options):
    messages = initial_messages(broker.workflow, options.artifact)
    base_count = len(messages)
    context_index = base_count - 1
    seen_actions = set()
    workspace_variables = []
    variable_types = {}
    feedback_checks = None
    initial = json.loads(messages[context_index]['content'])
    if 'checks' in initial:
        # До первого feedback технические IDs не нужны для выбора действий.
        # Сохраняем полные descriptors для привязки последующих verdicts.
        feedback_checks = deepcopy(initial['checks'])
        for check in initial['checks']: check.pop('condition_id', None)
    initial['state'] = execution_state(broker, [], {}, False)
    initial['state'].pop('requests_complete', None)
    continuation = broker.run.get('continuation')
    if continuation:
        initial['recovery'] = deepcopy(continuation['memory'])
        initial['recovery']['spent'] = continuation['prior_ledger']
        initial['recovery']['remaining_steps'] = broker.limits.max_steps - continuation['prior_ledger']['steps']
    messages[context_index]['content'] = json.dumps(initial, ensure_ascii=False)
    malformed = empty = 0
    try:
        for index in range(broker.limits.max_steps - (continuation or {}).get('prior_ledger', {}).get('steps', 0)):
            broker.guard()
            if worker.no_progress >= worker.limits.max_no_progress_blocks:
                broker.stop('code_correction_limit'); return
            broker.reserve_active('preflight', broker.limits.max_seconds)
            try:
                broker.event('model_preflight_started')
                if hasattr(model, 'set_deadline'): model.set_deadline(broker.session.deadline)
                request = payload(messages, options.model_name, broker.limits)
                if hasattr(model, 'prepare_payload'): request = model.prepare_payload(request)
                request, measured = compact_payload(request, prefix_count=base_count,
                    memory=lambda:memory_snapshot(broker.workflow, broker.run, broker.session, broker.safe),
                    counter=budget.counter, context_tokens=broker.limits.max_context_tokens)
                if measured:
                    broker.run.setdefault('context_projections', []).append({**measured,'model_call':index + 1})
                    broker.event('context_projected', measured)
                validate_tree(request, max_depth=64, max_bytes=broker.limits.max_prompt_bytes)
                if broker.safe(request) != request: raise WorkspaceError('private_model_context')
                input_tokens, reserved = budget.reserve(request)
            finally:
                broker.settle_active('preflight')
            run = broker.run
            run['model_calls'] += 1
            prefix = {'system': messages[0], 'tools': request['tools']}
            if context_index > 1: prefix['api_reference'] = messages[1:context_index]
            step = {'index': index + 1, 'action': None, 'observation': None,
                'offered_tools': [t['function']['name'] for t in request['tools']],
                'prefix_hash': fingerprint(prefix),
                'input_tokens_preflight': input_tokens, 'reserved_tokens': reserved}
            run['steps'].append(step)
            run['resources'] = budget.snapshot()
            bounded = hasattr(model, 'set_deadline')
            broker.reserve_active('model', getattr(model, 'timeout', broker.limits.max_seconds) if bounded else broker.limits.max_seconds)
            try:
                try:
                    broker.event('model_requested', {'step': index + 1, 'interface': 'execute_code'})
                    broker.guard()
                    if bounded:
                        model.set_deadline(min(broker.session.deadline, time.monotonic() + broker.remaining_active('model')))
                    response = model(deepcopy(request))
                finally: broker.settle_active('model')
            except (ModelError, ModelTimeout) as error:
                if type(error.timing) is dict:
                    try:
                        fields = ('ttft_seconds', 'wall_seconds')
                        step['timing'] = numeric_metadata({k: v for k, v in error.timing.items() if k in fields}, fields)
                    except ValueError: pass
                if not isinstance(error, ModelError) or error.code != 'model_generation_limit': raise
                budget.settle({}, request['max_tokens'])
                malformed += 1
                step.update(decision_kind='truncated', finish_reason='length')
                run['resources'] = budget.snapshot()
                broker.event('code_decision_rejected', {'kind': 'truncated'})
                broker.guard()
                if malformed > broker.limits.malformed_retries:
                    broker.stop('model_generation_limit'); return
                messages.append({'role': 'user', 'content': GENERATION_REPAIR_HINT})
                continue
            validate_tree(response)
            usage = numeric_metadata(response.get('usage', {}), ('prompt_tokens', 'completion_tokens', 'total_tokens'))
            budget.settle(usage, request['max_tokens'])
            step['usage'] = usage or None
            step['timing'] = numeric_metadata(response.get('_timing', {}), ('ttft_seconds', 'wall_seconds'))
            run['resources'] = budget.snapshot()
            broker.event('model_received', {'step': index + 1})
            broker.guard()
            try: call, name, args = _decision(response)
            except (ValueError, TypeError, KeyError, IndexError, EmptyDecision) as error:
                is_empty = isinstance(error, EmptyDecision)
                empty = empty + 1 if is_empty else 0
                malformed += int(not is_empty)
                step['decision_kind'] = 'empty' if is_empty else 'malformed'
                broker.event('code_decision_rejected', {'kind': step['decision_kind']})
                if empty >= broker.limits.max_empty_steps or malformed > broker.limits.malformed_retries:
                    broker.stop('empty_limit' if is_empty else 'malformed_limit'); return
                messages.append({'role': 'user', 'content': 'Return exactly one valid call to execute_code, check_goal or finish_run. No API operation tool is available.'})
                continue
            empty = 0
            step['action'] = {'name': name, 'arguments': {'code': {'stored_in': 'code_executions'}} if name == 'execute_code' else broker.safe(args)}
            before = len(run['exchanges'])
            action_key = _action_key(name, args, before)
            repeated = action_key in seen_actions
            seen_actions.add(action_key)
            event_offset = len(run['events'])
            # Native call_id остаётся только в RAM; durable связь назначается приложением.
            if name == 'execute_code':
                result = worker.execute(args['code'], call_id='model-step-' + str(index + 1))
            else:
                if name == 'finish_run' and not {'claims', 'answer_fields'} <= args.keys():
                    saved = broker.workflow.handle_tool(broker.session, 'check_goal', {})
                    missing = sorted({'claims', 'answer_fields'} - args.keys())
                    args.setdefault('claims', [{k: c[k] for k in ('condition_id', 'verdict')}
                                               for c in saved['conditions']])
                    args.setdefault('answer_fields', deepcopy(saved['answer_fields']))
                    broker.event('finish_defaults_applied', {'fields': missing,
                        'basis': 'accepted_goal_saved_evidence'})
                try:
                    result = broker.dispatch({'kind': 'finish' if name == 'finish_run' else name,
                        'artifact_hash': options.artifact.hash, **args})
                except WorkspaceError as error:
                    if broker.stopped: raise
                    result = {'error': str(error)}
            execution_id = result.get('execution_id')
            denied = [e['details']['reason'] for e in run['events'][event_offset:] if e['kind'] == 'call_rejected']
            if denied:
                result['error'] = denied[0]
            checklist = broker.workflow.handle_tool(broker.session, 'check_goal', {})
            if result.get('error') == 'NameError':
                result['hint'] = 'Define variables before use. Inside Python call the listed api functions; execute_code is an outer tool, not a Python function.'
            if result.get('error') in {'ModuleNotFoundError', 'ImportError'}:
                result['hint'] = 'Remove the failing import. The api object is already available; call its listed functions directly. Do not install packages.'
            if result.get('error') == 'duplicate_semantic_action':
                result['hint'] = ('Do not repeat this API call: the same semantic action already occurred and was not sent again. '
                    'Remove the rejected call. Keep existing response variables and continue only missing work. '
                    'Successful writes are listed in state.completed_writes; earlier failures remain evidence.')
            if result.get('error') == 'AttributeError' and result.get('error_object_type'):
                result['hint'] = 'Attribute access failed on a ' + result['error_object_type'] + '. API responses are dicts: response["body"], not response.body. JSON objects use ["field"]; JSON lists use numeric indexes.'
            if result.get('error_detail'):
                result['hint'] = HINTS[result['error_detail']['reason']]
            if name == 'execute_code':
                if 'variables' in result: workspace_variables = result['variables']
                if 'variable_types' in result: variable_types = result['variable_types']
                result['requests_executed'] = len(run['exchanges']) - before
                result['output_untrusted'] = True
                if not result['requests_executed'] and not result.get('error') and checklist['verdict'] != 'PASS':
                    result['hint'] = ('No HTTP request was executed by this block. Python stdout is untrusted '
                        'and cannot establish API results. Use the listed api functions for missing work.')
            result = broker.safe({**{k: result[k] for k in ('output', 'error', 'error_line', 'error_detail', 'hint', 'truncated', 'expected_request', 'preflight') if k in result},
                **{k: result[k] for k in ('requests_executed', 'output_untrusted') if k in result},
                'check': {'verdict': checklist['verdict'], 'conditions': [
                    {k: c[k] for k in (('condition_id', 'verdict') if c['verdict'] == 'PASS'
                        else ('condition_id', 'verdict', 'expected', 'actual'))} for c in checklist['conditions']],
                    'answer_fields': checklist['answer_fields']},
                'responses': [_response_feedback(broker, e) for e in run['exchanges'][before:]],
                'state': ({**execution_state(broker, workspace_variables, variable_types, checklist['verdict'] == 'PASS'),
                     'completed_writes': _completed_writes(broker)})})
            if repeated:
                result['warning'] = 'You repeated the same action. Consider its previous result before repeating it again.'
            result['state'].pop('requests_complete', None)
            result['state']['selected_checks_verdict'] = checklist['verdict']
            result['check']['coverage'] = broker.workflow.accepted.snapshot['coverage']
            result['check']['goal_verdict'] = 'UNKNOWN'
            step['observation'] = result
            broker.event('model_observed', {'step': index + 1, 'tool': name,
                'execution_id': execution_id})
            if broker.finished or broker.stopped: return
            context_call = deepcopy(call)
            discarded = len(messages) - base_count
            task_context = json.loads(messages[context_index]['content'])
            task_context['state'] = result['state']
            if feedback_checks is not None: task_context['checks'] = deepcopy(feedback_checks)
            messages[context_index]['content'] = json.dumps(task_context, ensure_ascii=False)
            # Полная история остаётся в Run; следующий контекст — закреплённая
            # цель плюс последний код, текущие checks/ответы и следующий запрос.
            model_observation = deepcopy(result)
            model_observation.pop('state', None)
            messages[base_count:] = [{'role': 'assistant', 'content': '', 'tool_calls': [context_call]},
                {'role': 'tool', 'tool_call_id': call['id'], 'content': json.dumps(model_observation, ensure_ascii=False)}]
            if discarded:
                broker.event('code_context_refreshed', {'discarded_messages': discarded,
                    'projection': 'accepted_goal_current_evidence'})
            diagnostic = result.get('preflight') or {}
            if diagnostic.get('hint'):
                # Инструкция приложения, не текст API/worker; тот же correction budget.
                messages.append({'role': 'user', 'content': 'Correct the rejected code at Python line '
                    + str(diagnostic.get('line')) + '. '
                    + diagnostic['hint'] + ' Return the corrected execute_code call.'})
            if result.get('error') and result.get('hint'):
                messages.append({'role': 'user', 'content': 'Correct the Python error at line '
                    + str(result.get('error_line')) + '. ' + result['hint']
                    + ' Keep earlier completed requests; continue only missing work.'})
            messages.append({'role': 'user', 'content': 'Selected checks assess only chosen aspects; '
                'the whole goal is not assessed. Compare the original goal with completed API calls. '
                'PASS/FAIL alone neither completes the goal nor blocks unrelated actions. '
                'If feasible actions remain, execute the next code block now, even after PASS/FAIL. '
                'A finish_run summary cannot schedule further actions. Keep expectations and failed evidence. '
                'If all goal actions are done or further work is infeasible, call finish_run now '
                'with the final user-facing answer in summary. A Python print is only local feedback: '
                'it does not answer the user or finish the Run. Do not print the same result again.'})
        broker.stop('step_limit')
    finally:
        # Raw code/context не являются durable history, включая private encoded literals.
        messages.clear()
        seen_actions.clear()


def run_workspace(workflow, history, options, *, session_id, message_id, model, limits,
                  token_counter=None, parent_run_id=None, trace_sink=None, event_sink=None):
    if not getattr(workflow,'selected_checks',False):raise ValueError('manual_workspace_required')
    options.validate(workflow.catalog, workflow.config)
    human = workflow.human
    clock = human.active_clock
    started = clock() - workflow.preparation_active
    budget = workflow.preparation_budget or TokenBudget(limits, token_counter or getattr(model, 'count_tokens', None))
    if getattr(workflow, 'continuation', None):
        budget.upper = workflow.continuation['prior_ledger']['tokens']
        budget.unknown = bool(budget.upper)
    broker = WorkspaceBroker(workflow, history, artifact=options.artifact, session_id=session_id,
        message_id=message_id, limits=limits, cancel=human.cancel, parent_run_id=parent_run_id,
        model_metadata={'name': options.model_name, 'interface': 'execute_code', 'mode': 'llm_code',
            'sampling': {**goal_sampling(options.model_name), 'max_tokens': limits.max_output_tokens}},
        trace_sink=trace_sink, event_sink=event_sink, environment='default')
    try:
        with broker:
            try:
                with tempfile.TemporaryDirectory(prefix='api-agent-code-') as directory:
                    with WorkspaceSandbox(broker, image=options.image, root=Path(directory) / 'work') as worker:
                        _drive(broker, worker, model, budget, options)
                if human.cancel.is_set(): broker.stopped = broker.stopped or 'cancelled'
                broker.run.update(status='completed' if broker.finished and not broker.stopped else 'partial',
                    stop_reason=broker.stopped or ('finished' if broker.finished else 'interrupted'))
                broker.run['report'] = broker.safe(report(broker.run))
            except Exception as error:
                if isinstance(error, BudgetViolation): reason = error.code
                elif isinstance(error, WorkspaceError): reason = str(error)
                elif isinstance(error, ModelTimeout): reason = 'model_timeout'
                elif isinstance(error, ModelError): reason = error.code
                else: reason = 'workspace_runtime_error'
                broker.stopped = broker.stopped or reason
            finally:
                if budget.pending: budget.unknown, budget.pending = True, False
                broker.run['resources'] = budget.snapshot()
                broker.run.update(wall_seconds=clock() - started, human_wait_seconds=human.waited_seconds,
                    human_events=human.public_events(), total_model_calls=budget.calls, limits=vars(limits))
    except Exception:
        if not broker.registered: raise
        # Сбой сохранения после эффектов не превращается в «HTTP не отправлен».
        broker.run.update(status='partial', stop_reason=broker.stopped or 'workspace_storage_error')
        broker.run['report'] = broker.safe(report(broker.run))
    return broker.safe_run()

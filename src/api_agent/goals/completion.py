"""Версионированный итог цели отдельно от сохранённых промежуточных ошибок."""
from copy import deepcopy

FINAL_STATE = 'final-state-v1'
STRICT_EXECUTION = 'strict-execution-v1'


def final_state_policy(run):
    contract = (run.get('admission_manifest') or {}).get('goal_contract') or {}
    return contract.get('success_criterion') == FINAL_STATE


def ready_goal(workflow, session):
    """Только принятый checklist, без новых HTTP, генерации или изменения ожиданий."""
    if not final_state_policy(session.run): return None
    if session.uncertain or session.cancel.is_set(): return None
    check = workflow.handle_tool(session, 'check_goal', {})
    return check if check['verdict'] == 'PASS' else None


def finish_catalog_if_ready(engine, run, session, guard, stop, emit):
    """Те же guard/session.finish, что у native finish, без лишнего вызова модели."""
    if not engine.workflow or not final_state_policy(run): return False
    if not guard(): return True
    check = ready_goal(engine.workflow, session)
    if check is None: return False
    final = session.finish({'summary': 'Принятые условия цели подтверждены сохранёнными ответами.',
        'evidence_ids': [e['id'] for e in session.exchanges]})
    run['chain_verdict'] = 'pass'
    stop('finished', 'completed')
    run['final'] = engine.safe({**final,
        'claims': [{'condition_id': c['condition_id'], 'verdict': c['verdict']} for c in check['conditions']],
        'answer_fields': deepcopy(check['answer_fields'])})
    emit('goal_completed_from_evidence', {'criterion': FINAL_STATE, 'model_call_required': False})
    return True

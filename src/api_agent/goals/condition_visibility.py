"""Экспериментальная model projection; полный checklist остаётся у проверяющего."""
from copy import deepcopy


def visible(workflow):
    return getattr(workflow, 'model_conditions', 'visible') == 'visible'


def _summary(check):
    conditions = check.get('conditions', [])
    return {'verdict': check['verdict'], 'condition_counts': {
        verdict: sum(c.get('verdict') == verdict for c in conditions)
        for verdict in ('PASS', 'FAIL', 'UNKNOWN')},
        'answer_fields_observed': len(check.get('answer_fields', {}))}


def model_observation(workflow, name, observation):
    """Менять только application checklist, не одноимённые поля HTTP body."""
    result = deepcopy(observation)
    if visible(workflow): return result
    if isinstance(result.get('check'), dict): result['check'] = _summary(result['check'])
    if name == 'check_goal' and isinstance(result.get('data'), dict):
        result['data'] = _summary(result['data'])
    if name == 'execute_operation' and isinstance(result.get('data'), dict):
        validation = result['data'].get('validation')
        if isinstance(validation, dict): validation.pop('expected_status_codes', None)
    return result

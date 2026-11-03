"""Заявления об успехе сверяются со свидетельствами до завершения Run."""

GOAL_PROGRESS_HINT=('Checks cover selected aspects, not the whole goal. Compare the original goal with real API state. '
    'Complete remaining feasible actions without replaying writes; preserve expectations and failed evidence. '
    'When all goal actions are done or work is blocked, call finish_run with the final Russian summary. '
    'Printing is local feedback, not a user answer or completion.')


def unsupported_passes(claims, checklist):
    actual={c['condition_id']:c['verdict'] for c in checklist['conditions']}
    return [c.get('condition_id') for c in claims if isinstance(c,dict)
        and c.get('verdict')=='PASS' and actual.get(c.get('condition_id'))!='PASS']


def correction(workflow, missing):
    auth=workflow.auth or {}
    login=auth.get('login_operation')
    entry=''
    if login and login in workflow.allowed:
        entry=' If login is needed, call the listed api function for '+login+'; the application supplies its credentials.'
    return ('The application rejected unsupported PASS claims. No invented Python dict or printed value proves an HTTP result. '
        'Continue with actual api.* function calls from the API reference, then check the real responses. '
        'Do not simulate, mock or fabricate responses. Preserve completed writes; do not blindly repeat them. '
        'If the task cannot be completed, finish honestly with UNKNOWN/FAIL claims instead of PASS.'+entry)

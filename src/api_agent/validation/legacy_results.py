"""Политика и чистая проекция сохранённых результатов S09; без LLM-ролей."""
from copy import deepcopy
from api_agent.openapi.parser import canonical, pointer_get
from api_agent.validation.common import fingerprint


def _bounded(value):
    """Большое значение не выдаётся за полный фрагмент evidence."""
    data = canonical(value)
    return deepcopy(value) if len(data) <= 1024 else {'omitted': True, 'bytes': len(data), 'sha256': fingerprint(value)}


def build_feedback(run):
    """Проекция только принятых условий и относящегося к ним evidence."""
    manifest = run['admission_manifest']['goal_contract']['manifest']
    checklist = run['report']['goal_checklist']
    descriptors = {c['id']: c for c in manifest['conditions']}
    exchanges = {e['id']: e for e in run['exchanges']}
    rows = []
    for check in checklist['conditions']:
        descriptor = descriptors[check['condition_id']]
        exchange = exchanges.get(check['exchange_id'])
        present = exchange is not None
        path = descriptor['parameters'].get('path')
        if present and path is not None:
            try: pointer_get(exchange['response']['json'], path)
            except (KeyError, IndexError, TypeError): present = False
        rows.append({'condition_id': check['condition_id'], 'requirement_id': descriptor['requirement_id'],
            'verdict': check['verdict'], 'expected': _bounded(check['expected']), 'actual': _bounded(check['actual']),
            'actual_present': present, 'source': descriptor['source'], 'basis': deepcopy(descriptor['basis']),
            'evidence_ids': [check['exchange_id']] if exchange else []})
    fields = {key: _bounded(value) for key, value in checklist['answer_fields'].items()}
    return {'goal_verdict': run['report']['goal_verdict'], 'execution_status': run['status'],
        'contract_verdict': run['report']['contract_verdict'], 'conditions': rows, 'answer_fields': fields,
        'unanswered_fields': [f['id'] for f in manifest['answer_fields'] if f['id'] not in fields]}


FINAL_STATE="final-state-v1"
STRICT_EXECUTION="strict-execution-v1"


def final_state_policy(run):
    contract = (run.get('admission_manifest') or {}).get('goal_contract') or {}
    return contract.get('success_criterion') == FINAL_STATE

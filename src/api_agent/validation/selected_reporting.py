"""Versioned отчёт S10 из сохранённых условий/exchanges, без LLM и сети."""
from copy import deepcopy
import uuid

from api_agent.openapi.parser import Catalog, canonical
from api_agent.validation.common import fingerprint


def selected_report(run):
    from api_agent.goals.checks.compiler import AcceptedGoal, check_goal
    contract=run['admission_manifest']['goal_contract']
    accepted=AcceptedGoal(canonical(contract['manifest']))
    if accepted.hash!=contract['manifest_hash']:raise ValueError('goal_manifest_changed')
    manifest=accepted.snapshot
    checklist=check_goal(accepted,Catalog(canonical(run['contract'])),run)
    events={e['id']:e for e in run.get('exchanges',[])}
    validations=run.get('validations',[])
    if any(v.get('exchange_id') not in events or v.get('run_id')!=run['run_id'] for v in validations):
        raise ValueError('report_foreign_validation')
    diagnostics=[]
    for validation in validations:
        diagnostics.append({k:deepcopy(validation.get(k)) for k in ('exchange_id','operation_id',
            'contract_verdict','issues_total','confirmed_contract_violations','complete_response')})
        diagnostics[-1]['issues']=deepcopy(validation.get('issues',[])[:16])
    verdicts={v.get('contract_verdict') for v in validations}
    contract_verdict='mismatch' if 'mismatch' in verdicts else 'pass' if verdicts=={'pass'} else 'not_checked'
    reason=run.get('stop_reason')
    limitations=['PASS относится только к выбранным условиям; полнота естественной цели не сертифицирована.',
        'Свободный ответ модели не является машинным verdict.']
    if manifest['mode']=='research':limitations.append('Пользовательские проверки не заданы.')
    if run.get('uncertain_write') or run.get('pending_mutation'):limitations.append('Эффект записи неизвестен; blind retry запрещён.')
    if any(e.get('truncated') for e in events.values()):limitations.append('Есть неполные/усечённые ответы.')
    if run['status']!='completed':limitations.append('Выполнение остановлено; часть поручения могла остаться невыполненной.')
    label={'PASS':'Выбранные условия подтверждены.','FAIL':'Обнаружено несоответствие выбранным условиям.',
        'UNKNOWN':'Для выбранных условий недостаточно свидетельств.','N/A':'Пользовательские проверки не заданы.'}
    summary=('Запуск завершён. ' if reason=='finished' else 'Запуск остановлен. ')+label[checklist['verdict']]
    final=run.get('final') or {}
    return {'schema_version':'run-report-v1','report_id':str(uuid.uuid5(uuid.NAMESPACE_URL,'api-agent-report:'+run['run_id'])),
        'project_id':run['project_id'],'session_id':run['session_id'],'message_id':run['message_id'],
        'response_message_id':run.get('response_message_id'),'run_id':run['run_id'],'parent_run_id':run.get('parent_run_id'),
        'goal':run['goal'],'goal_hash':run['goal_hash'],'mode':manifest['mode'],'target':run['target'],
        'summary':summary,'execution_status':run['status'],'stop_reason':reason,
        'checks_verdict':checklist['verdict'],'goal_verdict':'UNKNOWN','goal_coverage':manifest['coverage'],
        'goal_checklist':checklist,'conditions':deepcopy(checklist['conditions']),
        'contract_verdict':contract_verdict,'contract_diagnostics':diagnostics,
        'model_answer':{'text':final.get('summary'),'verification':'free_text_unverified',
            'claims_draft':deepcopy(final.get('claims',[])),'answer_fields_draft':deepcopy(final.get('answer_fields',{}))},
        'versions':{'spec_hash':run['spec_hash'],'config_hash':manifest['config_hash'],
            'config_version':manifest['config_version'],'operation_hashes':manifest['operation_hashes'],
            'checker':manifest['checker_version'],'manifest_contract':manifest['contract'],
            'manifest_version':manifest['version'],'manifest_hash':accepted.hash,'sdk_hash':run.get('sdk_hash')},
        'actor_context':list({fingerprint(e.get('request_principal')):deepcopy(e.get('request_principal')) for e in events.values()}.values()),
        'acceptance':deepcopy(manifest['acceptance']),
        'source':manifest['source'],'evidence_ids':list(events),
        'evidence_hashes':{key:fingerprint(value) for key,value in events.items()},
        'uncertain_write':bool(run.get('uncertain_write') or run.get('pending_mutation')),
        'resources':deepcopy(run.get('resources',{})),
        'time':{'started_at':run.get('started_at'),'wall_seconds':run.get('wall_seconds'),
            'human_wait_seconds':run.get('human_wait_seconds')},'limitations':limitations}

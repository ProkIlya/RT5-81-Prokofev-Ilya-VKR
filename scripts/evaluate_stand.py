"""Полная матрица стенда с тремя попытками, явными blockers и независимым oracle.

Owner/control исполняются только коллектором. Агент получает исходную цель,
обычный Project и заранее заданные условия, без ответного ключа/плана HTTP.
"""

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]


def plan_attempts(suite, supported):
    """Неисполняемые варианты остаются в матрице, а не исчезают из знаменателя."""
    return [dict(case_id=c['id'], rendered_goal=c['goal'], variant=variant, attempt=attempt,
                 required_capabilities=c['required_capabilities'],
                 status='planned' if c['id'] in supported else 'blocked_capability',
                 reason=None if c['id'] in supported else 'Нет доверенного полного oracle/fixture adapter этой бизнес-цели; будущие capabilities не обходятся.')
            for c in suite['cases'] for variant in c.get('variants', ['good']) for attempt in range(1, 4)]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(argv=None):
    parser = argparse.ArgumentParser(description='Evaluate original stand goals, three repetitions per variant')
    parser.add_argument('--output', required=True)
    parser.add_argument('--baseline', action='store_true')
    parser.add_argument('--total-tokens', type=int, default=8192)
    parser.add_argument('--output-tokens', type=int, default=512)
    parser.add_argument('--model-name', default='MiniCPM5-2B-Q4_K_M')
    args = parser.parse_args(argv)
    from api_agent.agent import StartAgentRun
    from api_agent.agent.model import LocalModel
    from api_agent.agent.resilience import JsonlJournal
    from api_agent.openapi import load_source
    from api_agent.policy import ProjectConfig
    from api_agent.storage import SQLiteHistory
    if args.baseline:
        workflow_module = load_module('api_agent.agent.catalog_baseline', ROOT/'evidence/S08/model-comparison/baseline/catalog_source.py')
    else:
        from api_agent.agent import catalog as workflow_module
    owner = load_module('stand_eval_owner', 'C:/RT5-51-Prokofiev-Ilya-RIP-Frontend/scripts/stand.py')
    suite_path = ROOT/'docs/evals/stand-tasks.ru.json'
    suite = json.loads(suite_path.read_text(encoding='utf-8-sig'))
    rows = plan_attempts(suite, {'STAND-37'})
    directory = Path(args.output).resolve()/uuid.uuid4().hex
    directory.mkdir(parents=True)
    code_hashes = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted((ROOT/'src').rglob('*.py'))}
    config = {'baseline':args.baseline, 'model':args.model_name,
              'max_tokens_total':args.total_tokens, 'max_output_tokens':args.output_tokens,
              'max_context_tokens':8192, 'max_steps':8, 'max_requests':3,
              'max_seconds':180, 'repetitions':3,
              'source_hashes_before':code_hashes,
              'suite_sha256':hashlib.sha256(suite_path.read_bytes()).hexdigest()}
    (directory/'configuration.json').write_text(json.dumps(config,ensure_ascii=False,indent=2),encoding='utf-8')
    try:
        for row in rows:
            if row['status'] != 'planned':
                continue
            attempt = directory/(row['case_id']+'-'+row['variant']+'-'+str(row['attempt']))
            attempt.mkdir()
            started = time.monotonic()
            try:
                before = owner.reset()
                owner.control('POST','/mode',{'mode':row['variant']})
                catalog = load_source('C:/RT5-51-Prokofiev-Ilya-RIP-Frontend/backend/docs/openapi.json')
                operation = 'get_api_users_profile'
                project = 'stand-'+uuid.uuid4().hex
                cfg = ProjectConfig.create(project,'http://127.0.0.1:18080',catalog,mode='READ_ONLY',resettable=True,operations={operation:'A'})
                workflow = workflow_module.CatalogWorkflow(catalog,cfg,goal=row['rendered_goal'],conditions={operation:[
                    {'predicate':'status_in','parameters':{'values':[401]},'intent':'negative','preconditions':'verified'}]},anonymous_operations=[operation])
                def sink(run):
                    text = json.dumps(run,ensure_ascii=False,indent=2)
                    if owner.initialize() in text or '18084' in json.dumps(run['history']):
                        raise ValueError('owner_key_or_control_leak')
                    (attempt/'run.json').write_text(text,encoding='utf-8')
                limits = workflow_module.CatalogLimits(max_tokens_total=args.total_tokens,max_output_tokens=args.output_tokens)
                with SQLiteHistory(attempt/'history.sqlite3') as history:
                    options = {} if args.model_name == 'MiniCPM5-2B-Q4_K_M' else {'model_name':args.model_name}
                    run = StartAgentRun('http://127.0.0.1:18080',catalog,workflow=workflow,limits=limits,
                                        history_store=history,trace_sink=sink,event_sink=JsonlJournal(attempt),**options).start(
                        project,'s',uuid.uuid4().hex,row['rendered_goal'],LocalModel('http://127.0.0.1:18081'))
                    sink(run)
                    historical = history.run_for_message(project,'s',run['response_message_id'])
                    assert historical['run_id'] == run['run_id']
                status = [e['response']['status'] for e in run['exchanges'] if e.get('response')]
                body = (run['exchanges'][0].get('response') or {}).get('json') if run['exchanges'] else None
                valid = status == ([401] if row['variant']=='good' else [200]) and run['requests']==1
                valid = valid and run['report']['intent_verdict'] == ('pass' if row['variant']=='good' else 'fail') and run['status']=='completed'
                if row['variant']=='good':
                    valid = valid and isinstance(body,dict) and 'login' not in body and 'id' not in body
                else:
                    valid = valid and isinstance(body,dict) and 'login' in body
                row.update(status='executed',run_id=run['run_id'],execution_status=run['status'],stop_reason=run['stop_reason'],
                           actual_http_statuses=status,machine_verdict=run['report']['intent_verdict'],
                           task_oracle='pass' if valid else 'fail',resources=run['resources'],
                           prompt_version=workflow.prompt_version,
                           model_summaries=[(s.get('action') or {}).get('arguments',{}).get('summary') for s in run['steps'] if (s.get('action') or {}).get('name')=='finish_run'],
                           model_summary_review='pending_explicit_review',
                           server_journal=owner.control('GET','/journal'),owner_reset_before=before,
                           llm_timings=[s.get('timing') for s in run['steps']],evidence=str(attempt))
            except Exception as error:
                row.update(status='blocked_fixture',error_type=type(error).__name__)
            finally:
                row['wall_seconds'] = time.monotonic()-started
                # Ошибка очистки не должна уничтожать уже полученный результат.
                (attempt/'record.json').write_text(json.dumps(row,ensure_ascii=False,indent=2),encoding='utf-8')
                try:
                    row['owner_reset_after'] = owner.reset()
                except Exception as error:
                    row['cleanup_error_type'] = type(error).__name__
                (directory/'matrix.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
                print(json.dumps({k:row.get(k) for k in ['case_id','variant','attempt','status','task_oracle','stop_reason']},ensure_ascii=False),flush=True)
    finally:
        try:
            after = owner.reset()
        except Exception as error:
            after = {'ready':False,'error_type':type(error).__name__}
        unchanged = all(hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h for p,h in code_hashes.items())
        aggregate = {'case_count':len(suite['cases']),'planned_attempt_count':len(rows),
                     'executed':sum(r['status']=='executed' for r in rows),
                     'blocked':sum(r['status'].startswith('blocked') for r in rows),
                     'task_oracle_pass':sum(r.get('task_oracle')=='pass' for r in rows),
                     'source_unchanged':unchanged,'owner_reset_after':after,
                     'collector_exit_code':0 if after.get('ready') and unchanged else 1, 'quality_acceptance':False,
                     'note':'Collection exit0 is not acceptance of all37 goals. Model explanations require separate review.'}
        (directory/'summary.json').write_text(json.dumps(aggregate,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'evidence':str(directory),**aggregate},ensure_ascii=False),flush=True)
    return aggregate['collector_exit_code']


if __name__ == '__main__':
    raise SystemExit(main())

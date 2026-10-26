"""Исполняемые S10 gates: good0/bad2, подготовка3; отдельное evidence каждого запуска."""
from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unittest
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests'),str(ROOT)]


def negative_controls(stage):
    from selected_support import catalog_config,card,configuration,exchange
    from api_agent.goals.checks.selected_compile import compile_selected
    from api_agent.goals.checks.compiler import check_goal
    catalog,config=catalog_config()
    detected=[]
    def reject(name, action):
        try:action()
        except ValueError:detected.append(name);return
        raise AssertionError('negative control missed: '+name)
    if stage in {'S10a','S10'}:
        reject('empty_testing',lambda:compile_selected(catalog,config,configuration([])))
        reject('forged_acceptance',lambda:compile_selected(catalog,config,{**configuration(),'accepted':True}))
        reject('private_path',lambda:compile_selected(catalog,config,configuration([card(assertions=[{'predicate':'exists','path':'/token'}])])))
        reject('unsupported_selector',lambda:compile_selected(catalog,config,configuration([card(selector='last_success')])))
        reject('multiple_negative_rules',lambda:compile_selected(catalog,config,configuration([card(negative=[{'location':'body','path':'/value','rule':'schema_type'}]*2)])))
        accepted=compile_selected(catalog,config,configuration())
        reject('foreign_evidence',lambda:check_goal(accepted,catalog,{'run_id':'r','exchanges':[exchange(1,run_id='foreign')]}))
        if check_goal(accepted,catalog,{'run_id':'r','exchanges':[exchange(1),exchange(2,outcome='transport_error')]})['verdict']!='UNKNOWN':raise AssertionError('timeout hidden')
        detected.append('timeout_hides_old_pass')
        rounded=compile_selected(catalog,config,configuration([card(assertions=[
            {'predicate':'number_rounded','path':'/value','expected':'80.000000000000000000'}])]))
        if check_goal(rounded,catalog,{'run_id':'r','exchanges':[exchange(1,value=(1<<4096)-1)]})['verdict']!='FAIL':
            raise AssertionError('precision mismatch hidden')
        detected.append('high_precision_mismatch')
        reject('input_imported_value',lambda:compile_selected(catalog,config,{**configuration(),
            'input_slots':[{'id':'device','type':'integer','value':42}]}))
        reject('private_binding_outside_project',lambda:compile_selected(catalog,config,{**configuration(),
            'input_slots':[{'id':'secret','type':'string','private':True,'binding':{'operation_id':'foreign','body_field':'password'}}]}))
        import tempfile
        from selected_support import DeviceServer
        from api_agent.policy import ProjectConfig
        from api_agent.storage import SQLiteHistory
        from api_agent.goals.entry.manual_application import ManualApplication
        from api_agent.goals.checks.selected_workflow import SelectedWorkflow
        from api_agent.goals.checks.compiler import AcceptedGoal
        from api_agent.openapi.parser import canonical
        from api_agent.agent.runtime import WorkspaceOptions
        from api_agent.workspace.execution.broker import WorkspaceBroker
        from api_agent.workspace.sdk import build_sdk
        with tempfile.TemporaryDirectory() as temp,DeviceServer() as server,SQLiteHistory(Path(temp)/'history.sqlite3') as history:
            read_catalog,_=catalog_config(server.target)
            read_config=ProjectConfig.create('p',server.target,read_catalog,mode='READ_ONLY',resettable=True,
                operations={'readDevice':'A','createDevice':'C','editDevice':'C'})
            artifact=build_sdk(read_catalog,read_config)
            app=ManualApplication(read_catalog,read_config,history=history,
                workspace=WorkspaceOptions(artifact,'sha256:'+'0'*64))
            accepted=AcceptedGoal(canonical(app.preview(configuration([card(assertions=[])],
                goal='Прочитай прибор42. Ничего не изменяй.'))['configuration']))
            workflow=SelectedWorkflow(read_catalog,read_config,accepted,human=app.human,preparation={})
            with WorkspaceBroker(workflow,history,artifact=artifact,session_id='s',message_id='readonly',environment='default') as broker:
                api=artifact.bind(broker.dispatch);api.readDevice(path__id=42)
                reject('unselected_write_remains_denied',lambda:api.editDevice(path__id=42,body={'value':80}))
            if server.journal!=[('GET','/devices/42',None)]:raise AssertionError('readonly_policy_expanded')
    if stage in {'S10b','S10'}:
        import tempfile
        from selected_support import DeviceServer,saved_run
        from api_agent.storage import SQLiteHistory
        with tempfile.TemporaryDirectory() as temp,DeviceServer() as server,SQLiteHistory(Path(temp)/'history.sqlite3') as history:
            run=saved_run(history,server)
            reject('foreign_report',lambda:history.read_report('foreign','s',run['run_id']))
            forged=deepcopy(run);forged['report']['checks_verdict']='FAIL'
            reject('model_assigned_report_verdict',lambda:history.finalize('p','s','m',forged))
            corrupted=deepcopy(run);corrupted['exchanges'][0]['response']['json']['value']=999
            history.db.execute('UPDATE runs SET payload=? WHERE run_id=?',(json.dumps(corrupted),run['run_id']))
            reject('changed_report_evidence',lambda:history.read_report('p','s',run['run_id']))
            reject('changed_run_read_bypass',lambda:history.read_run('p','s',run['run_id']))
            reject('changed_run_lookup_bypass',lambda:history.lookup('p','s','m'))
            history.db.execute('PRAGMA user_version=3')
            reject('schema_downgraded_report_read',lambda:history.read_report('p','s',run['run_id']))
    if stage in {'S10c','S10'}:
        import tempfile
        from selected_support import DeviceServer,saved_run
        from api_agent.storage import SQLiteHistory
        with tempfile.TemporaryDirectory() as temp,DeviceServer() as server,SQLiteHistory(Path(temp)/'history.sqlite3') as history:
            server.devices[42]['label']='[REDACTED]'
            run=saved_run(history,server)
            descriptor=history.select_attachment('p','s',run['run_id'],run['exchanges'][0]['id'],fields=['/id','/value'])
            reject('foreign_attachment',lambda:history.resolve_attachment('foreign',descriptor))
            reject('forged_attachment_values',lambda:history.resolve_attachment('p',{**descriptor,'fields_values':{'id':999}}))
            reject('missing_attachment_source_hash',lambda:history.resolve_attachment('p',{k:v for k,v in descriptor.items() if k!='evidence_hash'}))
            reject('redacted_attachment',lambda:history.select_attachment('p','s',run['run_id'],run['exchanges'][0]['id'],fields=['/label']))
            corrupted=deepcopy(run);corrupted['exchanges'][0]['response']['json']['value']=999
            history.db.execute('UPDATE runs SET payload=? WHERE run_id=?',(json.dumps(corrupted),run['run_id']))
            history.db.execute('PRAGMA user_version=3')
            reject('schema_downgraded_attachment',lambda:history.select_attachment('p','s',run['run_id'],run['exchanges'][0]['id'],fields=['/id','/value']))
    return detected


def main(stage,mode):
    if stage not in {'S10a','S10b','S10c','S10'} or mode not in {'good','bad'}:return 3
    root=Path(os.environ.get('S10_EVIDENCE_DIR',ROOT/'evidence/S10/checks'))
    directory=root/(stage+'-'+mode+'-'+uuid.uuid4().hex[:8]);directory.mkdir(parents=True)
    identity={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'src').rglob('*') if p.is_file() and p.suffix in {'.py','.sql'}}
    started=time.monotonic();record={'stage':stage,'mode':mode,'source_hashes':identity,'checks':'actual backend tests, not independent ACCEPT'}
    result=None
    try:
        if mode=='bad':
            record['detected']=negative_controls(stage)
            if not record['detected']:raise ValueError('stage_not_implemented')
            code=2
        else:
            modules={'S10a':['test_selected_checks','test_manual_application','test_manual_cli','test_selected_numeric_repair',
                    'test_selected_inputs_repair','test_manual_runtime_retirement','test_live_feedback_order',
                    'test_selected_feedback_scope','test_runtime_cleanup','test_recheck_policy_repair','test_recheck_completion_repair'],
                'S10b':['test_selected_history','test_report_integrity_repair','test_recheck_storage_repair'],'S10c':['test_result_attachments']}
            names=sum(modules.values(),[]) if stage=='S10' else modules[stage]
            if stage in {'S10a','S10c','S10'}:
                if os.environ.get('WORKSPACE_DOCKER_TESTS')!='1' or not os.environ.get('WORKSPACE_IMAGE'):raise ValueError('docker_test_configuration_required')
            if stage in {'S10a','S10'}:
                live=Path(os.environ.get('S10_LIVE_EVIDENCE',root/'live-complete/summary.json'))
                summary=json.loads(live.read_text(encoding='utf-8'))
                if {r['case'] for r in summary['records']}!={'partial_pass','independent_fail','covered','research','attachment'} or not summary['technical_controls_passing']:raise ValueError('live_controls_not_passing')
                for entry in summary['records']:
                    if entry['case'] in {'partial_pass','independent_fail'} and not (entry.get('feedback_order') or {}).get('confirmed'):
                        raise ValueError('live_feedback_order_not_confirmed')
                if summary['source_hashes']!=identity:raise ValueError('live_source_changed')
                record['live_evidence']=str(live)
                record['live_business_successes']=summary['business_successes']
                record['live_task_failures']=[r['case'] for r in summary['records'] if not r['successful']]
            with (directory/'tests.log').open('x',encoding='utf-8') as stream,redirect_stdout(stream),redirect_stderr(stream):
                suite=unittest.defaultTestLoader.loadTestsFromNames(names)
                def ids(items):
                    for item in items:
                        if isinstance(item,unittest.TestSuite):yield from ids(item)
                        else:yield item.id()
                record['test_ids']=list(ids(suite))
                result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
            record.update(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),skips=len(result.skipped))
            code=0 if result.wasSuccessful() and not result.skipped else 1
    except (ValueError,OSError,KeyError) as error:
        code=3;record['preparation_error']=type(error).__name__
    except Exception as error:
        code=1;record['error']=type(error).__name__
    record.update(exit_code=code,seconds=time.monotonic()-started)
    (directory/'result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in record.items() if k not in {'source_hashes','test_ids'}},ensure_ascii=False))
    return code


if __name__=='__main__':raise SystemExit(main(*sys.argv[1:]))

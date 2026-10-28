"""Исполняемые gates S12; good0/bad2, preparation3, сохранённый source/test manifest."""
from contextlib import redirect_stderr,redirect_stdout
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import unittest
import uuid
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/name) for name in ('src','tests','')]
MODULES={'S12a':['test_desktop_store','test_desktop_models','test_desktop_model_contract',
    'test_desktop_controller','test_desktop_ui','test_desktop_ui_execution','test_desktop_project_views','test_desktop_launcher',
    'test_desktop_preview','test_desktop_startup','test_desktop_publication',
    'test_desktop_submission','test_desktop_composer','test_desktop_http','test_http_header_alias','test_module_size'],
    'S12b':['test_desktop_constructor','test_desktop_constructor_ui','test_desktop_constructor_controls',
        'test_desktop_testing_ui','test_selected_checks','test_selected_numeric_repair','test_module_size']}
MODULES['S12']=list(dict.fromkeys(MODULES['S12a']+MODULES['S12b']))


def identities():
    return {p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
        for folder in ['src','tests','scripts'] for p in sorted((ROOT/folder).rglob('*'))
        if p.is_file() and p.suffix in {'.py','.sql','.json','.tcss'}}


def run(names,stream):
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(unittest.defaultTestLoader.loadTestsFromNames(names))
    return {'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),
        'skips':len(result.skipped),'passing':result.wasSuccessful() and not result.skipped}


def negative_specs(stage):
    from api_agent.desktop.models import ModelManager
    from api_agent.desktop.store import DesktopStore
    from api_agent.desktop.controller import DesktopController
    from api_agent.storage.checkpoints import scope_hash
    prepare,acquire,draft,read=ModelManager.prepare,ModelManager.acquire,DesktopStore.draft,DesktopController.read_run
    def unchecked(self,record,*,expected=None,**kwargs):return prepare(self,record,expected=None,**kwargs)
    def ignore_expected(self,*,expected=None):return acquire(self)
    def cross_draft(self,project,session):return draft(self,'p','s')
    def cross_history(self,project,session,run_id):return read(self,project,'s',run_id)
    def weak_scope(value):
        value=dict(value);value.pop('model',None);return scope_hash(value)
    from api_agent.desktop.submission import SubmissionMixin
    from api_agent.desktop.composer import ComposerMixin
    from api_agent.execution import transport_session
    classify=transport_session.classified_headers
    def raw_headers(pairs,**kwargs):
        pairs=list(pairs);value=classify(pairs,**kwargs)
        for item in value['items']:
            if item['classification']!='public':item['value']=next(v for n,v in pairs if n==item['name'])
        return value
    def raw_alias(pairs,**kwargs):
        pairs=list(pairs);value=classify(pairs,**kwargs)
        if len(pairs)==1 and pairs[0][0].lower()=='content-type':value['items'][0]['value']=pairs[0][1]
        return value
    controls=[
        (ModelManager,'prepare',unchecked,'test_desktop_models.DesktopModelTests.test_same_alias_different_gguf_cannot_continue'),
        (ModelManager,'acquire',ignore_expected,'test_desktop_models.DesktopModelTests.test_acquire_rejects_another_prepared_identity'),
        (DesktopStore,'draft',cross_draft,'test_desktop_store.DesktopStoreTests.test_draft_survives_restart_and_never_crosses_session_or_project'),
        (DesktopController,'read_run',cross_history,'test_desktop_controller.DesktopControllerTests.test_reading_saved_report_never_prepares_a_model_or_replays_http'),
        ('api_agent.storage.checkpoints','scope_hash',weak_scope,'test_desktop_model_contract.ModelContractTests.test_checkpoint_scope_binds_model_identity_not_just_goal'),
        (SubmissionMixin,'submission_current',lambda self,token:True,'test_desktop_submission.SubmissionTests.test_goal_edit_during_prepare_does_not_send_old_research'),
        (ComposerMixin,'refresh_composer',lambda self:None,'test_desktop_composer.ComposerTests.test_restored_research_exposes_attachment_provenance_and_limits_and_can_remove'),
        (transport_session,'classified_headers',raw_headers,'test_desktop_http.HttpMetadataTests.test_classified_headers_survive_restart_without_authority_and_with_bounds'),
        (transport_session,'classified_headers',raw_alias,'test_http_header_alias.HeaderAliasTests.test_bearer_basic_secret_headers_are_closed_in_every_persisted_projection')]
    if stage=='S12a':return controls
    from api_agent.desktop.constructor_state import ConstructorState
    from api_agent.desktop.constructor import ConstructorDialog
    from api_agent.goals.checks import selected_check
    compile_original,init_original,checker=ConstructorState.compiled,ConstructorDialog.__init__,selected_check.check_selected
    def ignore_spec(self,text):self.replace(json.loads(text)['cards'])
    def change_selector(self,goal,*,cards=None):
        from copy import deepcopy
        values=deepcopy(self.cards if cards is None else cards)
        for card in values:card['selector']='first'
        return compile_original(self,goal,cards=values)
    def no_evidence_pass(accepted,catalog,run):
        result=checker(accepted,catalog,run)
        if not run.get('exchanges'):result['verdict']='PASS'
        return result
    def editable_old(self,state,*,readonly=False):init_original(self,state,readonly=False)
    constructor_controls=[
        (ConstructorState,'import_text',ignore_spec,'test_desktop_constructor.ConstructorTests.test_import_is_atomic_and_rejects_stale_spec_unknown_fields_and_private_data'),
        (ConstructorState,'compiled',change_selector,'test_desktop_constructor.ConstructorTests.test_collection_duplicates_selectors_and_common_checker_parity'),
        (selected_check,'check_selected',no_evidence_pass,'test_desktop_constructor.ConstructorTests.test_collection_duplicates_selectors_and_common_checker_parity'),
        (ConstructorDialog,'__init__',editable_old,'test_desktop_constructor_ui.ConstructorUITests.test_old_constructor_is_readonly_and_does_not_change_current_draft')]
    return constructor_controls if stage=='S12b' else controls+constructor_controls


def main(stage,kind):
    if stage not in MODULES or kind not in {'good','bad'}:return 3
    if os.environ.get('WORKSPACE_DOCKER_TESTS')!='1' or not os.environ.get('WORKSPACE_IMAGE'):
        print('Docker tests and pinned WORKSPACE_IMAGE required',file=sys.stderr);return 3
    directory=ROOT/'evidence/S12'/('gate-'+stage+'-'+kind+'-'+uuid.uuid4().hex[:10]);directory.mkdir(parents=True)
    before=identities();record={'stage':stage,'kind':kind,'identity':before};started=time.monotonic();code=1
    try:
        with (directory/'tests.log').open('x',encoding='utf-8') as stream,redirect_stdout(stream),redirect_stderr(stream):
            record['baseline']=run(MODULES[stage],stream)
            if not record['baseline']['passing']:raise ValueError('baseline_failed')
            if kind=='good':code=0
            else:
                outcomes=[]
                for owner,attribute,replacement,name in negative_specs(stage):
                    if isinstance(owner,str):
                        import importlib
                        owner=importlib.import_module(owner)
                    with patch.object(owner,attribute,replacement):result=run([name],stream)
                    outcomes.append({'test':name,'detected':result['failures']>0 and result['errors']==0,
                                     'result':result})
                record['negative_controls']=outcomes
                code=2 if all(r['detected'] for r in outcomes) else 1
    except Exception as error:record['error']=str(error) if isinstance(error,ValueError) else type(error).__name__
    record.update(exit_code=code,seconds=time.monotonic()-started,source_tests_unchanged=before==identities())
    if not record['source_tests_unchanged']:code=1;record['exit_code']=code
    (directory/'result.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({**{k:v for k,v in record.items() if k!='identity'},'evidence':str(directory)},ensure_ascii=False))
    return code


if __name__=='__main__':raise SystemExit(main(*sys.argv[1:]))

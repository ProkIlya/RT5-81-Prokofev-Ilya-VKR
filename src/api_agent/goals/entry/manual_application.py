"""Preview и явное принятие ручной конфигурации до любых model/API calls."""
from copy import deepcopy

from api_agent.execution.contracts import CatalogLimits
from api_agent.bounded_json import validate_tree
from api_agent.openapi.parser import canonical
from api_agent.validation import fingerprint
from api_agent.goals.entry.application_context import ApplicationContext
from api_agent.goals.checks.compiler import AcceptedGoal
from api_agent.goals.checks.selected_compile import compile_selected
from api_agent.goals.checks.selected_workflow import SelectedWorkflow
from api_agent.goals.entry.continuation import ContinuationApplication


class ManualApplication(ContinuationApplication, ApplicationContext):
    def __init__(self, catalog, config, *, history, workspace, human=None, limits=None,
                 mappings=None, token_counter=None, current_config=None, trace_sink=None,
                 event_sink=None, model_test_passwords=None, source='user_import'):
        self.configuration_source = source
        super().__init__(catalog, config, history=history,
            human=human, limits=limits or CatalogLimits(), token_counter=token_counter,
            mappings=mappings, current_config=current_config, trace_sink=trace_sink, event_sink=event_sink,
            workspace=workspace, model_test_passwords=model_test_passwords)

    def attachment_status(self, descriptor):
        """Применимость отдельно от неизменного исторического отчёта."""
        from api_agent.storage.reuse import attachment_status, actor_identity
        return attachment_status(self.history, self.config.snapshot['project_id'],
                                 descriptor, self.catalog, self.config, actor_identity(self.mappings))

    def _configuration(self, document):
        accepted = compile_selected(self.catalog, self.config, document, source=self.configuration_source)
        if self._safe(document) != document: raise ValueError('selected_private_configuration')
        snapshot=accepted.snapshot
        attachments=[]
        for item in snapshot['attachments']:
            reuse = self.attachment_status(item)
            if reuse['status'] != 'applicable': raise ValueError('attachment_' + reuse['status'])
            resolved = self.history.resolve_attachment(self.config.snapshot['project_id'], item)
            resolved['reuse'] = reuse
            attachments.append(resolved)
        validate_tree(attachments,max_depth=16,max_bytes=16384)
        for attachment in attachments:
            operation=attachment['origin']['operation_id']
            private=[path for path,rule in self.mappings.get('extraction',{}).get(operation,{}).items() if rule.get('secret')]
            if any(f['source']['path']==s or f['source']['path'].startswith(s+'/') for f in attachment['fields'] for s in private):
                raise ValueError('selected_private_attachment')
        if self._safe(attachments)!=attachments:raise ValueError('selected_private_attachment')
        snapshot['attachments']=attachments
        return AcceptedGoal(canonical(snapshot))

    def preview(self, document, *, parent_run_id=None):
        accepted = self._configuration(document)
        parents={a['origin']['run_id'] for a in accepted.snapshot['attachments']}
        if len(parents)>1 or parents and parent_run_id and parent_run_id not in parents:
            raise ValueError('selected_ambiguous_parent')
        parent_run_id=parent_run_id or next(iter(parents),None)
        if parent_run_id:
            parent=self.history.goal_parent(self.config.snapshot['project_id'],parent_run_id)
            snapshot=accepted.snapshot
            snapshot['version']=((parent.get('admission_manifest') or {}).get('goal_contract') or {}).get('manifest',{}).get('version',0)+1
            accepted=AcceptedGoal(canonical(snapshot))
        result = {'configuration': accepted.snapshot, 'limits': vars(self.limits),
            'policy': deepcopy(self.config.snapshot), 'sdk_hash': self.workspace.artifact.hash,
            'runtime_mapping_hash': fingerprint(self.mappings),'parent_run_id':parent_run_id}
        if self.workspace.snapshot is not None: result['model'] = self.workspace.snapshot
        # Feasibility/configuration validation выполняется до принятия и HTTP.
        self._workflow(accepted, {})
        return {**result, 'preview_hash': fingerprint(result)}

    def _workflow(self, accepted, preparation):
        workflow = SelectedWorkflow(self.catalog, self.config, accepted, human=self.human,
            preparation=preparation, model_name=self.model_name, current_config=self.current_config,
            **{k: deepcopy(v) for k, v in self.mappings.items() if k in {'auth','extraction','binding_rules','private_bodies','public_response_headers'}})
        workflow.runtime_mapping_hash = fingerprint({k: v for k, v in self.mappings.items() if k != 'health_check'})
        from api_agent.storage.reuse import actor_identity
        workflow.actor_identity_hash = actor_identity(self.mappings)
        return workflow

    def start(self, project, session, message, document, model, *, accepted_hash=None, parent_run_id=None):
        if project != self.config.snapshot['project_id']: raise ValueError('selected_foreign_project')
        with self._lock:
            preview = self.preview(document,parent_run_id=parent_run_id)
            parent_run_id=preview['parent_run_id']
            command_hash = fingerprint({'document':document,'parent_run_id':parent_run_id}) if parent_run_id else fingerprint(document)
            goal_hash = fingerprint(document['goal'])
            previous = self.history.lookup(project, session, message)
            if previous is not None:
                if previous.get('preparation', {}).get('command_hash') != command_hash:
                    raise ValueError('selected_message_changed')
                return previous
            if accepted_hash is not None and accepted_hash != preview['preview_hash']:
                raise ValueError('selected_preview_changed')
            old = self.history.goal_preparation(project, session, message, goal_hash)
            if old is not None:
                if old.get('command_hash') != command_hash: raise ValueError('selected_message_changed')
                return old.get('result', {'status':'blocked','run_id':None,'stop_reason':'preparation_incomplete'})
            preparation = {'goal_hash': goal_hash, 'command_hash': command_hash,
                'preview_hash': preview['preview_hash'], 'status': 'awaiting_acceptance', 'generator_calls': 0}
            claimed = self.history.begin_goal_preparation(project, session, message, preparation)
            if claimed is not None: raise ValueError('selected_message_busy')
            confirmed = accepted_hash is not None
            if not confirmed:
                confirmed = self.human.accept(preview['preview_hash'], preview)['approved']
            if not confirmed or self.human.cancel.is_set():
                result = {'status':'blocked','run_id':None,'stop_reason':'selected_acceptance_required'}
                preparation.update(status='blocked', result=result)
                self._save(project, session, message, preparation)
                return result
            if self.preview(document,parent_run_id=parent_run_id)['preview_hash'] != preview['preview_hash']:
                raise ValueError('selected_preview_changed')
            snapshot = deepcopy(preview['configuration'])
            snapshot['acceptance'] = {'status': 'accepted', 'preview_hash': preview['preview_hash']}
            accepted = AcceptedGoal(canonical(snapshot))
            preparation.update(status='accepted', human_events=self.human.public_events())
            self._save(project, session, message, preparation)
            workflow = self._workflow(accepted, preparation)
            workflow._snapshot_metadata = self.history.goal_project_metadata(self.config, workflow.storage_metadata())
            from api_agent.agent.runtime import run_workspace
            result = run_workspace(workflow, self.history, self.workspace, session_id=session,
                message_id=message, model=model, limits=self.limits, token_counter=self.token_counter,
                parent_run_id=parent_run_id, trace_sink=self.trace_sink, event_sink=self.event_sink)
            preparation.update(status='completed', result=self._safe(result))
            self._save(project, session, message, preparation)
            return result

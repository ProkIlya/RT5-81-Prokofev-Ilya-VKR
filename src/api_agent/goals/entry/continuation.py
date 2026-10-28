"""Явное linked продолжение: immutable intent, health и остатки исходных budgets."""
from copy import deepcopy

from api_agent.goals.checks.compiler import AcceptedGoal
from api_agent.openapi.model import canonical
from api_agent.storage.checkpoints import recovery_memory, validate_checkpoint
from api_agent.validation import fingerprint
from api_agent.goals.entry.readiness import check_service


class ContinuationApplication:
    def preview_continuation(self, project, session, run_id):
        """Локальный preview без сети и без изменения прерванного Run."""
        if project != self.config.snapshot['project_id']: raise ValueError('continuation_foreign_project')
        run = self.history.read_run(project, session, run_id)
        if run['status'] != 'partial': raise ValueError('continuation_not_interrupted')
        checkpoint = validate_checkpoint(self.history, run)
        original_model = (run.get('model') or {}).get('snapshot')
        selected_model = self.workspace.snapshot
        if original_model is not None or selected_model is not None:
            if original_model is None or selected_model is None or original_model != selected_model:
                raise ValueError('continuation_model_changed')
        if checkpoint['limits'] != vars(self.limits): raise ValueError('continuation_limits_changed')
        if run['target'] != self.target or run['spec_hash'] != self.catalog.spec_hash:
            raise ValueError('continuation_stale_configuration')
        if run.get('policy_config') != self.config.snapshot: raise ValueError('continuation_stale_policy')
        if run.get('runtime_mapping_hash') != fingerprint({k: v for k, v in self.mappings.items() if k != 'health_check'}):
            raise ValueError('continuation_actor_or_mappings_changed')
        self.history.assert_continuation_available(project, run_id)
        if run.get('reset_epoch') != self.history._fact_policy(project)['reset_epoch']:
            raise ValueError('continuation_known_reset')
        manifest = run['admission_manifest']['goal_contract']['manifest']
        ledger = checkpoint['ledger']
        remaining = {name: maximum - ledger[name] for name, maximum in (
            ('steps', self.limits.max_steps), ('http', self.limits.max_requests),
            ('tokens', self.limits.max_tokens_total), ('active_seconds', self.limits.max_seconds))}
        if any(v <= 0 for v in remaining.values()): raise ValueError('continuation_budget_exhausted')
        memory = recovery_memory(self.history, run, checkpoint)
        continuation = {'parent_run_id': run_id, 'root_run_id': (run.get('continuation') or {}).get('root_run_id', run_id),
            'parent_checkpoint_hash': checkpoint['hash'], 'prior_ledger': ledger, 'memory': memory,
            'completed_mutations': checkpoint['completed_mutations']}
        value = {'goal': run['goal'], 'mode': run['mode'], 'configuration': deepcopy(manifest),
            'parent_run_id': run_id, 'limits': vars(self.limits), 'spent': ledger, 'remaining': remaining,
            'active_time_accounting': checkpoint['active_usage'],
            'memory': memory, 'continuation': continuation,
            'health_configuration_hash': fingerprint(self.mappings.get('health_check')),
            'runtime_mapping_hash': fingerprint(self.mappings)}
        if original_model is not None: value['model'] = deepcopy(original_model)
        return {**value, 'preview_hash': fingerprint(value)}

    def continue_run(self, project, origin_session, run_id, *, session_id, message_id, model, accepted_hash=None):
        """Новый worker; нет аргумента для изменения цели или budgets."""
        with self._lock:
            previous = self.history.lookup(project, session_id, message_id)
            if previous is not None:
                if (previous.get('continuation') or {}).get('parent_run_id') != run_id:
                    raise ValueError('continuation_message_changed')
                return previous
            preview = self.preview_continuation(project, origin_session, run_id)
            if accepted_hash is not None and preview['preview_hash'] != accepted_hash:
                raise ValueError('continuation_preview_changed')
            if accepted_hash is None and not self.human.accept(preview['preview_hash'], preview)['approved']:
                return {'status': 'blocked', 'run_id': None, 'stop_reason': 'continuation_acceptance_required'}
            if self.human.cancel.is_set(): raise ValueError('continuation_cancelled')
            goal_hash = fingerprint(preview['goal'])
            prior = self.history.goal_preparation(project, session_id, message_id, goal_hash)
            if prior is not None:
                if prior.get('continuation_parent') != run_id: raise ValueError('continuation_message_changed')
                return prior.get('result', {'status': 'blocked', 'run_id': None, 'stop_reason': 'preparation_incomplete'})
            preparation = {'goal_hash': goal_hash, 'status': 'preparing', 'preview_hash': preview['preview_hash'],
                'continuation_parent': run_id}
            if self.history.begin_goal_preparation(project, session_id, message_id, preparation) is not None:
                raise ValueError('continuation_message_busy')
            try:
                health = check_service(self.target, self.mappings.get('health_check'))
            except ValueError as error:
                health = {'ready': False, 'reason': str(error), 'request_sent': False}
            preparation['health_check'] = health
            if not health['ready']:
                result = {'status': 'blocked', 'run_id': None, 'stop_reason': 'service_unavailable', 'health_check': health}
                preparation.update(status='blocked', result=result); self._save(project, session_id, message_id, preparation)
                return result
            if self.human.cancel.is_set(): raise ValueError('continuation_cancelled')
            if self.preview_continuation(project, origin_session, run_id)['preview_hash'] != preview['preview_hash']:
                raise ValueError('continuation_preview_changed')
            preparation['status'] = 'accepted'; self._save(project, session_id, message_id, preparation)
            workflow = self._workflow(AcceptedGoal(canonical(preview['configuration'])), preparation)
            workflow.continuation = preview['continuation']
            workflow.inputs.update(preview['memory']['inputs'])
            workflow._snapshot_metadata = self.history.goal_project_metadata(self.config, workflow.storage_metadata())
            from api_agent.agent.runtime import run_workspace
            result = run_workspace(workflow, self.history, self.workspace, session_id=session_id,
                message_id=message_id, model=model, limits=self.limits, token_counter=self.token_counter,
                parent_run_id=run_id, trace_sink=self.trace_sink, event_sink=self.event_sink)
            preparation.update(status='completed', result=result); self._save(project, session_id, message_id, preparation)
            return result

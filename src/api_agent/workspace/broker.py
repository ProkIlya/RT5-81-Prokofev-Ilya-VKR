"""S09d2: внешний broker; worker не владеет правами, evidence или credentials."""
from copy import deepcopy
import secrets
import threading
import time
import uuid

from ..agent.catalog import CatalogLimits
from ..bounded_json import validate_tree
from ..openapi.parser import canonical
from ..validation import fingerprint, validate_exchange, report
from .sdk import SdkArtifact, BUILDER_VERSION

ERROR_CODES = set(('artifact_denied protocol_denied operation_denied invalid_parameters ambiguous_parameter_name '
    'binding_source_denied auth_reference_denied occurrence_denied occurrence_inputs_changed unexpected_occurrence '
    'request_limit cancelled uncertain_write run_time_limit stale_approval policy_denied config_scope_changed '
    'stale_auth_epoch unresolved_uncertain_write approval_rejected approval_required workspace_finished '
    'clarification_unavailable goal_revision_required input_slot_locked invalid_human_input human_cancelled human_unanswered '
    'ask_limit invalid_question clarification_required precondition_not_verified target_not_applicable '
    'required_parameter_missing invalid_parameter_type unsafe_parameter_value unsafe_path_parameter '
    'required_body_missing invalid_request_body body_denied credential_body_is_private request_body_limit '
    'duplicate_semantic_action invalid_finish unfinished_chain foreign_finish_evidence '
    'fact_invalid_descriptor fact_unsupported_descriptor fact_source_mismatch fact_invalid_pointer fact_private_descriptor '
    'fact_snapshot_mismatch fact_stale_evidence fact_unknown_evidence fact_foreign_operation fact_incomplete_evidence '
    'fact_private_request fact_unknown_expectation fact_untrusted_condition fact_predicate_not_verified fact_runtime_value '
    'fact_unsupported_value fact_unsupported_predicate fact_identity_mismatch fact_invalid_create_read '
    'fact_invalid_request_parameter fact_untrusted_binding fact_content_unavailable fact_content_mismatch '
    'fact_private_value fact_proposal_limit unexpected_http_status storage_error trace_error').split())


class WorkspaceError(ValueError):
    """Закрытый код ошибки, без отражения входов или текста исключения worker."""


class WorkspaceBroker:
    """Один последовательный Run под SQLite lease, с повторной policy перед HTTP."""
    def __init__(self, workflow, history, *, artifact, session_id, message_id, limits=None, cancel=None,
                 parent_run_id=None, model_metadata=None, trace_sink=None, event_sink=None, environment='local'):
        self.workflow, self.history = workflow, history
        self.catalog, self.config = workflow.catalog, workflow.config
        self.limits, self.cancel = limits or CatalogLimits(), cancel or threading.Event()
        if not isinstance(artifact, SdkArtifact): raise WorkspaceError('artifact_denied')
        manifest, snapshot = artifact.manifest, self.config.snapshot
        if (manifest.get('config_hash') != fingerprint(snapshot) or manifest.get('spec_hash') != self.catalog.spec_hash
            or manifest.get('builder_version') != BUILDER_VERSION or set(manifest.get('operations', {})) != set(snapshot['operations'])
            or any(op.get('operation_hash') != self.catalog.operation_hashes.get(name) for name, op in manifest['operations'].items())):
            raise WorkspaceError('artifact_denied')
        self.artifact = artifact
        self.session_id, self.message_id = session_id, message_id
        self.parent_run_id, self.model_metadata = parent_run_id, deepcopy(model_metadata)
        self.trace_sink, self.event_sink = trace_sink, event_sink
        self.environment, self.registered = environment, False
        self.run = None
        self.context = {}
        self.stopped = None
        self.finished = False
        self.mutation_rejected = False
        self._lease = None
        self.audit_key = secrets.token_bytes(32)

    def safe(self, value, *, metadata=False):
        if hasattr(self, 'session'):
            value = (self.session.state.redact_public_metadata(value) if metadata else self.session.state.redact(value))
        else: value = deepcopy(value)
        private = sorted(self.workflow.secrets(), key=len, reverse=True)
        def walk(item):
            if isinstance(item, str):
                for secret in private:
                    if secret: item = item.replace(secret, '[REDACTED]')
                return item
            if isinstance(item, list): return [walk(v) for v in item]
            if isinstance(item, dict): return {walk(k): walk(v) for k, v in item.items()}
            return item
        return walk(value)

    def safe_run(self):
        """Только верхнеуровневые закреплённые descriptors являются metadata."""
        metadata = {'contract', 'admission_manifest', 'policy_config', 'run_catalog'}
        result = self.safe({k: v for k, v in self.run.items() if k not in metadata})
        for key in metadata & self.run.keys(): result[key] = self.safe(self.run[key], metadata=True)
        return result

    def __enter__(self):
        self._lease = self.history.execution()
        self._lease.__enter__()
        try:
            snap = self.config.snapshot
            if self.history.lookup(snap['project_id'], self.session_id, self.message_id) is not None:
                raise WorkspaceError('message_already_executed')
            if self.workflow.feasibility_stop: raise WorkspaceError(self.workflow.feasibility_stop)
            self.prepared = self.workflow.prepared(self.catalog, self.message_id, self.workflow.goal)
            self.run = {'run_id': str(uuid.uuid4()), 'project_id': snap['project_id'], 'session_id': self.session_id,
                'message_id': self.message_id, 'response_message_id': str(uuid.uuid4()), 'target': snap['target'],
                'contract': self.catalog.snapshot, 'spec_hash': self.catalog.spec_hash, 'goal': self.workflow.goal,
                'goal_hash': fingerprint(self.workflow.goal), 'goal_coverage': self.workflow.goal_coverage,
                'admission_manifest': self.workflow.runtime_manifest(), 'model': {'interface': 'execute_code', 'mode': 'controlled_code'},
                'status': 'partial', 'stop_reason': None, 'steps': [], 'exchanges': [], 'validations': [], 'validation': None,
                'events': [], 'requests': 0, 'model_calls': 0, 'expectations': [e for group in self.prepared.values() for e in group],
                'code_executions': [], 'sdk_hash': self.artifact.hash, 'resources': {}, 'final': None}
            if self.parent_run_id: self.run['parent_run_id'] = self.parent_run_id
            if self.model_metadata is not None: self.run['model'] = deepcopy(self.model_metadata)
            self.session = self.workflow.make_session(snap['target'], self.catalog, self.run, self.limits)
            self.session.cancel = self.cancel
            metadata = self.workflow.storage_metadata() if hasattr(self.workflow, 'storage_metadata') else self.workflow.runtime_manifest()
            if (self.safe_run() != self.run or self.safe(metadata, metadata=True) != metadata
                or self.safe(self.artifact.manifest, metadata=True) != self.artifact.manifest
                or self.safe(self.artifact.source, metadata=True) != self.artifact.source):
                raise WorkspaceError('contract_redaction_conflict')
            self.history.begin_run(snap['project_id'], self.session_id, self.message_id, self.run,
                environment=self.environment, configuration=snap, metadata=metadata)
            self.registered = True
            self.event('workspace_started', {'sdk_hash': self.artifact.hash})
            return self
        except BaseException:
            try:
                if self.registered:
                    self.stopped = self.stopped or 'workspace_initialization_error'
                    self.run.update(status='partial', stop_reason=self.stopped)
                    self.run['report'] = self.safe(report(self.run))
                    self.run = self.safe_run()
                    self.history.finalize(self.run['project_id'], self.session_id, self.message_id, self.run)
            finally:
                self._clear()
                self._lease.__exit__(None, None, None)
                self._lease = None
            raise

    def checkpoint(self):
        self.run['requests'] = self.session.requests
        snapshot = self.safe_run()
        self.history.save_snapshot(snapshot)
        if self.trace_sink:
            try: self.trace_sink(deepcopy(snapshot))
            except Exception:
                self.stopped = self.stopped or 'trace_error'
                raise WorkspaceError('trace_error') from None

    def event(self, kind, details=None):
        event = {'run_id': self.run['run_id'], 'sequence': len(self.run['events']) + 1,
            'kind': kind, 'details': self.safe({**self.context, **(details or {})})}
        self.run['events'].append(event)
        # Одна durable транзакция вместо двух commit; trace failure откатывает обе записи.
        try:
            with self.history.transaction():
                self.history.append_event(event)
                self.checkpoint()
        except Exception:
            self.stopped = self.stopped or 'storage_error'
            raise WorkspaceError(self.stopped) from None
        if self.event_sink:
            try: self.event_sink(deepcopy(event))
            except Exception:
                self.stopped = self.stopped or 'trace_error'
                raise WorkspaceError('trace_error') from None

    def stop(self, reason):
        self.stopped = self.stopped or reason
        self.run['stop_reason'] = self.stopped
        self.event('workspace_stopped', {'reason': self.stopped})

    def guard(self):
        if self.finished: raise WorkspaceError('workspace_finished')
        if self.cancel.is_set(): self.stopped = self.stopped or 'cancelled'
        if self.session.uncertain: self.stopped = 'uncertain_write'
        if not self.stopped:
            try: self.session.remaining()
            except ValueError: self.stopped = 'run_time_limit'
        if self.stopped: raise WorkspaceError(self.stopped)

    def dispatch(self, message):
        """Полный IPC request недоверенный; неизвестные поля не игнорируются."""
        try:
            self.guard()
            validate_tree(message, max_depth=32, max_bytes=32768)
            if not isinstance(message, dict) or message.get('artifact_hash') != self.artifact.hash:
                raise WorkspaceError('artifact_denied')
            kind = message.get('kind')
            fields = {'operation': ({'operation_id', 'parameters'}, {'body'}), 'check_goal': (set(), set()),
                'ask_user': ({'question', 'input_slot'}, set()), 'propose_fact': ({'descriptor'}, set()),
                'finish': ({'summary', 'claims', 'answer_fields'}, set())}
            if kind not in fields: raise WorkspaceError('protocol_denied')
            required, optional = fields[kind]
            keys = set(message) - {'kind', 'artifact_hash'}
            if not required <= keys or keys - required - optional: raise WorkspaceError('protocol_denied')
            if kind == 'operation': return self._operation(message)
            if kind == 'check_goal':
                if hasattr(self.workflow, 'handle_tool'): return self.safe(self.workflow.handle_tool(self.session, 'check_goal', {}))
                return {'chain_verdict': self.workflow.completion(self.session, self.run['validations']),
                    'evidence_ids': [e['id'] for e in self.run['exchanges']]}
            if kind == 'ask_user':
                if not hasattr(self.workflow, 'handle_tool'): raise WorkspaceError('clarification_unavailable')
                result = self.workflow.handle_tool(self.session, kind, {k: message[k] for k in required})
                self.event('human_input_received')
                return self.safe(result)
            if kind == 'propose_fact':
                self.checkpoint()
                if len(self.run.get('fact_proposals', [])) >= 16: raise WorkspaceError('fact_proposal_limit')
                result = self.history.preview_fact(self.run['project_id'], self.session_id, self.run['run_id'], message['descriptor'])
                self.run.setdefault('fact_proposals', []).append(deepcopy(message['descriptor']))
                self.event('fact_proposed')
                return self.safe(result)
            return self._finish(message)
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            reason = str(error) if isinstance(error, ValueError) else 'protocol_denied'
            # Только известные коды; данные и исключения сторонних callbacks не отражаются.
            if reason not in ERROR_CODES: reason = 'call_denied'
            if reason == 'approval_rejected': self.mutation_rejected = True
            if reason in {'request_limit', 'cancelled', 'uncertain_write', 'run_time_limit', 'stale_approval',
                          'policy_denied', 'config_scope_changed', 'stale_auth_epoch', 'unresolved_uncertain_write'}:
                self.stopped = self.stopped or reason
            self.event('call_rejected', {'reason': reason})
            raise WorkspaceError(reason) from None

    def _operation(self, message):
        name, parameters = message['operation_id'], message['parameters']
        if name not in self.artifact.manifest['operations'] or name not in self.workflow.allowed:
            raise WorkspaceError('operation_denied')
        if not isinstance(parameters, dict) or set(parameters) != {'path', 'query', 'header'} or any(type(v) is not dict for v in parameters.values()):
            raise WorkspaceError('invalid_parameters')
        op = self.catalog.operations[name]
        expected = {(p['in'], p['name']) for p in op['parameters']}
        if any((location, key) not in expected for location, values in parameters.items() for key in values):
            raise WorkspaceError('invalid_parameters')
        namespaced = getattr(self.workflow, 'parameter_namespaces', False)
        supplied = {(location + '__' + key if namespaced else key): value
            for location, values in parameters.items() for key, value in values.items()}
        if len(supplied) != sum(len(values) for values in parameters.values()): raise WorkspaceError('ambiguous_parameter_name')
        args = {'operation_id': name, 'parameters': supplied}
        if 'body' in message: args['body'] = deepcopy(message['body'])
        slot = self.workflow.next_occurrence(self.session.exchanges)
        if self.workflow.occurrences:
            if slot is None or slot['operation_id'] != name: raise WorkspaceError('occurrence_denied')
            args['occurrence_id'] = slot['id']
        for key, rule in self.workflow.bindings_for(name, args.get('occurrence_id')).items():
            refs = [r for r in self.session.state.public() if not r['secret'] and r['operation_id'] == rule['source_operation']
                and r['path'] == rule['source_path'] and ('source_occurrence' not in rule or r.get('occurrence_id') == rule['source_occurrence'])
                and key in supplied and canonical(r['value']) == canonical(supplied[key])]
            if len(refs) != 1: raise WorkspaceError('binding_source_denied')
            source = next((e for e in self.session.exchanges if e['id'] == refs[0]['exchange_id']), None)
            checked = next((v for v in self.run['validations'] if v['exchange_id'] == refs[0]['exchange_id']), {})
            principal = source.get('request_principal', {}) if source else {}
            from ..validation.selection_scope import applicable
            if (source is None or source['outcome'] != 'received' or checked.get('contract_verdict') != 'pass'
                or checked.get('intent_verdict') != 'pass' or not checked.get('task_postcondition') or not applicable(checked)
                or (principal.get('mode') == 'authenticated' and (principal.get('auth_epoch') != self.session.state.auth_epoch
                    or principal.get('actor') != self.session.state.principal))):
                raise WorkspaceError('binding_source_denied')
            args.setdefault('bindings', {})[key] = refs[0]['ref']
            del supplied[key]
        mode = self.workflow.request_mode(name, slot)
        if mode == 'authenticated':
            auth = self.workflow.auth
            refs = [r for r in self.session.state.public() if r['secret'] and r['operation_id'] == auth['login_operation']
                and r['path'] == auth['token_path'] and r.get('auth_epoch') == self.session.state.auth_epoch
                and r.get('principal') == self.session.state.principal]
            if len(refs) != 1: raise WorkspaceError('auth_reference_denied')
            args['credential_ref'] = refs[0]['ref']
        mutation = op['method'] not in {'GET', 'HEAD', 'OPTIONS'}
        if mutation and self.mutation_rejected: raise WorkspaceError('approval_rejected')
        if mutation and self.history.unresolved_mutations(self.run['project_id'], exclude_run_id=self.run['run_id']):
            raise WorkspaceError('unresolved_uncertain_write')
        self.session.admit(args)
        if mutation: self.run['pending_mutation'] = {'operation_id': name, 'request_index': self.session.requests + 1}
        self.event('http_prepared', {'operation_id': name, 'operation_hash': self.catalog.operation_hashes[name],
            'occurrence_id': args.get('occurrence_id'), 'request_index': self.session.requests + 1})
        self.guard()
        exchange = self.session.execute(args)
        if mutation and not self.session.uncertain: self.run.pop('pending_mutation', None)
        validation = validate_exchange(self.catalog, exchange, self.prepared[self.workflow.prepared_key(args)])
        self.run['exchanges'].append(self.safe(exchange))
        self.run['validations'].append(self.safe(validation))
        self.run['validation'] = self.safe(validation)
        self.workflow.observation(self.session, exchange, validation)
        self.event('http_completed', {'operation_id': name, 'exchange_id': exchange['id'],
            'request_index': self.session.requests, 'outcome': exchange['outcome'], 'validation_hash': fingerprint(self.safe(validation))})
        response = exchange.get('response') or {}
        accepted = getattr(self.workflow, 'accepted', None)
        if accepted is not None and accepted.snapshot.get('contract') == 'conditions-v2':
            expected = {status for condition in self.prepared[self.workflow.prepared_key(args)]
                if condition['predicate'] == 'status_in' for status in condition['parameters']['values']}
            # Evidence уже сохранено. Новый блок выбирает модель, HTTP не повторяется.
            if response.get('status') not in expected:
                raise WorkspaceError('unexpected_http_status')
        return self.safe({'status': response.get('status'), 'body': deepcopy(response.get('json')),
            'json_valid': response.get('json_valid', False), 'truncated': exchange['truncated'], 'outcome': exchange['outcome']})

    def _finish(self, message):
        if not isinstance(message['summary'], str) or not 1 <= len(message['summary']) <= 2000 or not isinstance(message['claims'], list) or not isinstance(message['answer_fields'], dict):
            raise WorkspaceError('invalid_finish')
        self.session.finish({'summary': '', 'evidence_ids': [e['id'] for e in self.session.exchanges]})
        verdict = self.workflow.completion(self.session, self.run['validations'])
        if verdict == 'not_checked': raise WorkspaceError('unfinished_chain')
        self.run['chain_verdict'] = verdict
        self.run['final'] = self.safe({'summary': message['summary'], 'claims': message['claims'], 'answer_fields': message['answer_fields'],
            'evidence_ids': [e['id'] for e in self.session.exchanges]})
        self.finished = True
        self.event('draft_finished', {'chain_verdict': verdict})
        return {'kind': 'draft_finished', 'chain_verdict': verdict}

    def __exit__(self, exc_type, exc, tb):
        try:
            self.run['stop_reason'] = self.stopped or ('finished' if self.finished and exc_type is None else 'interrupted')
            self.run['status'] = 'completed' if self.run['stop_reason'] == 'finished' else 'partial'
            if self.session.uncertain or self.run.get('pending_mutation'):
                self.run.update(uncertain_write=True, primary_safety_cause='uncertain_write', stop_reason='uncertain_write', status='partial')
            self.run['report'] = self.safe(report(self.run))
            try:
                self.checkpoint()
            except WorkspaceError:
                # Ошибка финального экспорта тоже сохраняется; HTTP не повторяется.
                self.run.update(status='partial', stop_reason=self.stopped or 'trace_error')
                self.run['report'] = self.safe(report(self.run))
            self.run = self.safe_run()
            self.session.run = self.run
            self.history.finalize(self.run['project_id'], self.session_id, self.message_id, self.run)
        finally:
            self._clear()
            if self._lease: self._lease.__exit__(exc_type, exc, tb)

    def _clear(self):
        """Ошибка enter также освобождает приватное состояние и временные ключи."""
        if hasattr(self, 'session'):
            self.session.state.close()
            self.session.actions.clear()
            self.session._headers.clear()
            self.session._action_key = b''
            self.session._approved.clear()
            self.session.exchanges.clear()
        self.audit_key = b''

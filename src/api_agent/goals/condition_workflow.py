"""Conditions-only адаптер существующего Catalog Core, без второго исполнителя."""
from copy import deepcopy
import json

from ..agent.catalog import CatalogWorkflow, CatalogSession, PhaseDecision
from ..agent.model import configured_goal_model, goal_sampling
from ..policy import PolicyStop
from ..validation import schema_issues
from ..state.sensitive import TestPassword
from ..openapi.documentation import documentation_preview
from .condition_contract import check_conditions
from .condition_generation import _public_literal_schema
from .workflow import GoalWorkflow
from .completion import FINAL_STATE, STRICT_EXECUTION, final_state_policy
from .condition_visibility import visible
from .condition_auth import scoped_operations, revoked_record, install_revoked_credential


class ConditionWorkflow(GoalWorkflow):
    def __init__(self, catalog, config, accepted, *, proposal, human, budget, preparation,
                 extraction=None, binding_rules=None, auth=None, private_bodies=None,
                 current_config=None, answer_diagnostic=False, model_name=None, success_criterion=STRICT_EXECUTION,
                 model_conditions='visible'):
        if success_criterion not in {FINAL_STATE, STRICT_EXECUTION}: raise ValueError('invalid_completion_policy')
        if model_conditions not in {'visible', 'hidden'}: raise ValueError('invalid_condition_visibility')
        self.model_conditions = model_conditions
        self.accepted, self.human, self.preparation_budget = accepted, human, budget
        self.model_name = model_name or configured_goal_model()
        self.sampling = goal_sampling(self.model_name)
        self.preparation = deepcopy(preparation)
        self.preparation_active = preparation.get('active_seconds', 0)
        self.private_bodies, self.inputs = deepcopy(private_bodies or {}), {}
        self.answer_diagnostic = answer_diagnostic
        self._project_extraction = deepcopy(extraction or {})
        self._project_bindings = deepcopy(binding_rules or {})
        self._occurrence_bindings = {}
        self._authenticated = False
        manifest = accepted.snapshot
        self._contract = {'manifest': manifest, 'manifest_hash': accepted.hash, 'proposal': deepcopy(proposal)}
        self._contract['success_criterion'] = success_criterion
        self._contract['model_conditions'] = model_conditions
        self.auth_scopes = scoped_operations(accepted)
        if self.auth_scopes:
            if not auth or any('revoked' in scopes for scopes in self.auth_scopes.values()) and not auth.get('logout_operations'):
                raise ValueError('goal_auth_scope_mapping_required')
            self._contract['auth_scope_mapping'] = {'logout_operations': list(auth.get('logout_operations', []))}
        observations = {o['id']: o for o in manifest['observations']}
        conditions, request_auth = {}, {}
        for condition in manifest['conditions']:
            observation = observations[condition['observation_id']]
            name = observation['operation_id']
            if condition['predicate'] == 'status_in':
                values = condition['parameters']['values']
                descriptor = {'predicate': 'status_in', 'parameters': {'values': values},
                    'intent': 'negative' if values == [401] else 'positive', 'preconditions': 'verified'}
                if descriptor not in conditions.setdefault(name, []): conditions[name].append(descriptor)
                if values == [401] and name not in self.auth_scopes: request_auth[name] = 'anonymous_negative'
        # Core проверяет transport/contract; scopes и полноту business checks
        # определяет принятый condition contract, а не first_request scope Core.
        CatalogWorkflow.__init__(self, catalog, config, goal=manifest['goal'], conditions=conditions,
            extraction=extraction, binding_rules=binding_rules, auth=auth, goal_coverage='partial',
            request_auth=request_auth, approval=self._approve, current_config=current_config)

    def prepared(self, catalog, message, goal):
        from ..validation import prepare_expectations
        result = CatalogWorkflow.prepared(self, catalog, message, goal)
        manifest = self.accepted.snapshot
        for name, scopes in self.auth_scopes.items():
            result.pop(name)
            for scope in sorted(scopes):
                observations = {o['id'] for o in manifest['observations']
                    if o['operation_id'] == name and o.get('auth_scope') == scope}
                descriptors = [{'source':'USER_DEFINED','basis':{'message_id':message},
                    'predicate':'status_in','parameters':deepcopy(c['parameters']),
                    'intent':'negative' if scope == 'revoked' else 'positive','preconditions':'verified'}
                    for c in manifest['conditions'] if c['observation_id'] in observations and c['predicate'] == 'status_in']
                group = prepare_expectations(catalog, descriptors, message, goal, operation_id=name)
                for item in group:
                    item['request_principal_scope'] = {'mode':'old_epoch' if scope == 'revoked' else 'authenticated'}
                    item['selection_scope'] = {'mode':'first_request'}
                result['auth:' + name + ':' + scope] = group
        return result

    def prepared_key(self, args):
        name = args['operation_id']
        if name in self.auth_scopes:
            return 'auth:' + name + ':' + ('revoked' if self.request_mode(name) == 'anonymous_negative' else 'current')
        return CatalogWorkflow.prepared_key(self, args)

    def old_epoch_allowed(self, session, args, principal):
        if 'revoked' not in self.auth_scopes.get(args['operation_id'], set()): return False
        try: record = revoked_record(session)
        except PolicyStop: return False
        return principal == {'mode':'old_epoch','auth_epoch':record['auth_epoch'],'actor':record['principal']}

    def prompt_checks(self, checks):
        return CatalogWorkflow.prompt_checks(self, checks) if visible(self) else []








    def inspect(self, catalog, op, section='inputs'):
        """Публичные входы без уже предоставленных private полей."""
        data = CatalogWorkflow.inspect(self, catalog, op, section)
        body = data['catalog_operation'].get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema')
        private = self.private_bodies.get(op, {})
        if body and private:
            body['properties'] = {k: v for k, v in body.get('properties', {}).items() if k not in private}
            body['required'] = [k for k in body.get('required', []) if k not in private]
        if self.auth:
            data['input_requirements']['authentication'] = 'application_supplied'
            data['input_requirements'].pop('credential_source', None)
        return data

    def handle_tool(self, session, name, args):
        if name == 'check_goal': return check_conditions(self.accepted, self.catalog, session.run)
        descriptor = next((v for v in self.accepted.snapshot.get('input_slots', []) if v['id'] == args['input_slot']), None)
        if descriptor is None or session.requests: raise PolicyStop('goal_revision_required')
        waited = self.human.waited_seconds
        try: value = self.human.ask(args['question'], phase='run')
        finally: session.deadline += self.human.waited_seconds - waited
        if schema_issues(value, {'type': descriptor['type']}): raise PolicyStop('invalid_human_input')
        self.inputs[descriptor['id']] = value
        session.run['goal_inputs'] = deepcopy(self.inputs)
        session.run['human_events'] = self.human.public_events()
        return {'input_slot': descriptor['id'], 'value': value}

    def completion(self, session, validations):
        result = check_conditions(self.accepted, self.catalog, session.run)
        if any(c['verdict'] == 'UNKNOWN' for c in result['conditions']): return 'not_checked'
        if len(result['answer_fields']) != len(self.accepted.snapshot['answer_fields']): return 'not_checked'
        if not final_state_policy(session.run) and any(v['contract_verdict'] == 'fail' for v in validations): return 'fail'
        return {'PASS': 'pass', 'FAIL': 'fail', 'UNKNOWN': 'not_checked'}[result['verdict']]

    def observation(self, session, exchange, validation):
        result = CatalogWorkflow.observation(self, session, exchange, validation)
        if exchange['operation_id'] in self.auth_scopes and visible(self):
            result['validation']['expected_status_codes'] = sorted({v for item in session.run['expectations']
                if item['operation_id'] == exchange['operation_id'] and item.get('request_principal_scope', {}).get('mode')
                == exchange['request_principal']['mode'] for v in item.get('parameters', {}).get('values', [])})
        self._authenticated = session.state.principal is not None
        return result

    def request_mode(self, name, slot=None):
        """Сессия меняется после login, принятый manifest остаётся неизменным."""
        mode = CatalogWorkflow.request_mode(self, name, slot)
        scopes = self.auth_scopes.get(name)
        if scopes:
            return 'authenticated' if 'current' in scopes and (self._authenticated or 'revoked' not in scopes) else 'anonymous_negative'
        if (self.auth and self._authenticated and name != self.auth['login_operation']
                and mode != 'anonymous_negative'):
            return 'authenticated'
        return mode

    def make_session(self, target, catalog, run, limits):
        run['preparation'] = deepcopy(self.preparation)
        run['human_events'] = self.human.public_events()
        session = ConditionSession(target, catalog, self, run, limits)
        session.deadline -= self.preparation_active
        return session


class ConditionSession(CatalogSession):
    def admit(self, args):
        if not {v['id'] for v in self.workflow.accepted.snapshot.get('input_slots', [])} <= self.workflow.inputs.keys():
            raise PolicyStop('clarification_required')
        args = deepcopy(args)
        name = args['operation_id']
        if self.workflow.request_mode(name, None) == 'authenticated' and 'credential_ref' not in args:
            auth = self.workflow.auth
            refs = [v['ref'] for v in self.state.public() if v['secret']
                and v['operation_id'] == auth['login_operation'] and v['path'] == auth['token_path']
                and v.get('auth_epoch') == self.state.auth_epoch and v.get('principal') == self.state.principal]
            if len(refs) != 1: raise PolicyStop('auth_reference_denied')
            args['credential_ref'] = refs[0]
        private = self.workflow.private_bodies.get(name)
        if private:
            body = args.get('body', {})
            if not isinstance(body, dict): raise PolicyStop('private_body_denied')
            for field in private.keys() & body.keys():
                if not isinstance(private[field], TestPassword) or body[field] != private[field]:
                    raise PolicyStop('private_body_denied')
            args = deepcopy(args)
            args['body'] = {**body, **private}
        return super().admit(args)

    def authorize(self, args, result):
        if (args['operation_id'] in self.workflow.auth_scopes
                and self.workflow.request_mode(args['operation_id']) == 'anonymous_negative'):
            install_revoked_credential(self)
        if result[1]['method'] in {'GET', 'HEAD', 'OPTIONS'}:
            # Свободный цикл может повторно читать изменившееся состояние.
            # Для mutations прежний semantic action key остаётся неизменным.
            result = (*result[:-1], (*result[-1], 'fresh_read', self.requests))
        waited = self.workflow.human.waited_seconds
        try: return super().authorize(args, result)
        finally: self.deadline += self.workflow.human.waited_seconds - waited

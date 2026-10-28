"""Ручные режимы поверх общего admission/auth/policy/HTTP, без второго loop."""
from copy import deepcopy
import time

from api_agent.execution.workflow import CatalogWorkflow
from api_agent.agent.model import configured_goal_model, goal_sampling
from api_agent.validation import schema_issues
from api_agent.goals.checks.compiler import check_goal
from api_agent.goals.entry.code_workflow import CodeWorkflow, CodeSession
from api_agent.goals.checks.condition_scope import subset
from api_agent.goals.checks.selected_inputs import ask_input


class SelectedWorkflow(CodeWorkflow):
    """Конфигурация выбирает проверки; каталог действий не становится планом."""
    selected_checks = True

    def __init__(self, catalog, config, accepted, *, human, preparation, model_name=None,
                 extraction=None, binding_rules=None, auth=None, private_bodies=None, current_config=None,
                 public_response_headers=None):
        self.accepted, self.human, self.preparation_budget = accepted, human, None
        self.model_name = model_name or configured_goal_model()
        self.sampling = goal_sampling(self.model_name)
        self.preparation = deepcopy(preparation); self.preparation_active = 0
        self.private_bodies = deepcopy(private_bodies or {}); self.inputs = {}
        self.answer_diagnostic = False; self.model_conditions = 'visible'
        self._project_extraction, self._project_bindings = deepcopy(extraction or {}), deepcopy(binding_rules or {})
        self._occurrence_bindings, self.auth_scopes, self._authenticated = {}, {}, False
        manifest = accepted.snapshot
        for item in manifest['input_slots']:
            if item['private']:
                binding=item['binding'];name,key=binding['operation_id'],binding['body_field']
                if key in self.private_bodies.get(name,{}):raise ValueError('selected_input_already_provided')
                self.private_bodies.setdefault(name,{})[key]=None
        self._contract = {'manifest': manifest, 'manifest_hash': accepted.hash,
            'success_criterion': 'selected-goal-finish-v1', 'model_conditions': 'visible'}
        request_auth = {}
        for name in config.snapshot['operations']:
            scopes = {o['auth'] for o in manifest['observations'] if o['operation_id'] == name}
            if 'anonymous' in scopes:
                if 'current' in scopes: raise ValueError('selected_mixed_auth_unsupported')
                request_auth[name] = 'anonymous'
        # Технические contract checks не подменяются ожиданиями отдельных карточек.
        required = {o['operation_id'] for o in manifest['observations']}
        required.update(value['response']['operation_id'] for observation in manifest['observations']
            for value in observation['where'].get('parameters', {}).values()
            if isinstance(value, dict) and set(value) == {'response'})
        CatalogWorkflow.__init__(self, catalog, config, goal=manifest['goal'],
            conditions={name: [] for name in config.snapshot['operations']}, extraction=extraction,
            binding_rules=binding_rules, auth=auth, request_auth=request_auth,
            approval=self._approve, current_config=current_config, required_operations=required,
            public_response_headers=public_response_headers)
        if not self.allowed: raise ValueError('selected_no_allowed_operations')
        if any(o['operation_id'] not in self.allowed for o in manifest['observations']):
            raise ValueError('selected_operation_not_allowed')
        for observation in manifest['observations']:
            if observation['auth'] == 'current' and not auth: raise ValueError('selected_auth_mapping_required')

    def handle_tool(self, session, name, args):
        if name == 'check_goal': return check_goal(self.accepted, self.catalog, session.run)
        return ask_input(self,session,args)

    def completion(self, session, validations):
        result = self.handle_tool(session, 'check_goal', {})
        return {'PASS': 'pass', 'FAIL': 'fail'}.get(result['verdict'], 'not_checked')

    def request_mode(self, name, slot=None):
        mode = CatalogWorkflow.request_mode(self, name, slot)
        if self.request_auth.get(name) == 'anonymous': return 'anonymous'
        if self.auth and self._authenticated and name != self.auth['login_operation']: return 'authenticated'
        return mode

    def prepared(self, catalog, message, goal):
        from api_agent.validation import prepare_expectations
        return {name: prepare_expectations(catalog, None, message, goal,
            operation_id=name, expected_status=self._status(name)) for name in self.allowed}

    def prepared_key(self, args): return args['operation_id']

    def make_session(self, target, catalog, run, limits):
        run['preparation'] = deepcopy(self.preparation)
        run['mode'] = self.accepted.snapshot['mode']
        run['started_at'] = time.time()
        run['human_events'] = self.human.public_events()
        return SelectedSession(target, catalog, self, run, limits)


class SelectedSession(CodeSession):
    """Ровно одно принятое исключение схемы; остальные guards у общего Core."""
    def admit(self, args):
        self._negative_allowed = None
        name = args.get('operation_id')
        operation = self.catalog.operations.get(name, {})
        schema = operation.get('requestBody', {}).get('content', {}).get('application/json', {}).get('schema')
        body = args.get('body')
        issues = schema_issues(body, schema) if schema is not None and body is not None else []
        if issues:
            eligible = []
            for card in self.workflow.accepted.snapshot['cards']:
                negative = card.get('negative')
                if not card['enabled'] or card['operation_id'] != name or not negative: continue
                if not subset(card['where'].get('parameters', {}), args.get('parameters', {})): continue
                if not subset(card['where'].get('body', {}), body): continue
                if len(issues) == 1 and (issues[0]['path'], issues[0]['code']) == (negative['path'], negative['rule']):
                    eligible.append((negative['path'], negative['rule']))
            if len(set(eligible)) == 1: self._negative_allowed = eligible[0]
        return super().admit(args)

    def request_schema_issues(self, value, schema, location):
        issues = schema_issues(value, schema)
        if location == 'body' and len(issues) == 1 and (issues[0]['path'], issues[0]['code']) == self._negative_allowed:
            return []
        return issues

    def finish(self, args):
        self.remaining()
        if args['evidence_ids'] != [e['id'] for e in self.exchanges]: raise ValueError('foreign_finish_evidence')
        return {**args, 'run_id': self.run_id}

"""Ручные режимы поверх общего admission/auth/policy/HTTP, без второго loop."""
from copy import deepcopy
import time

from api_agent.execution.workflow import CatalogWorkflow
from api_agent.agent.model import configured_goal_model, goal_sampling
from api_agent.validation import schema_issues
from api_agent.goals.checks.compiler import check_goal
from api_agent.goals.entry.code_workflow import CodeWorkflow, CodeSession
from api_agent.goals.checks.condition_scope import private_response_path, subset
from api_agent.goals.legacy.condition_auth import confirmed_revocation
from api_agent.goals.checks.selected_inputs import ask_input


class SelectedWorkflow(CodeWorkflow):
    """Конфигурация выбирает проверки; каталог действий не становится планом."""
    selected_checks = True
    project_metadata = None

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
        revoked = [o for o in manifest['observations'] if o['auth'] == 'revoked']
        if len(revoked) > 1 or revoked and (not (auth or {}).get('logout_operations') or 'cookie_name' in (auth or {})):
            raise ValueError('selected_revoked_mapping_required')
        self.revoked_probe = revoked[0] if revoked else None
        variants = [o for o in manifest['observations'] if 'login_variant' in o]
        self.rotation = None
        if variants:
            mapping = (auth or {}).get('rotation')
            if (not isinstance(mapping, dict) or set(mapping) != {'operation_id', 'login_field', 'change_field'}
                    or any(not isinstance(value, str) for value in mapping.values())
                    or not auth.get('logout_operations') or len(variants) != 2
                    or {o['login_variant'] for o in variants} != {'previous', 'replacement'}
                    or any(o['operation_id'] != auth['login_operation'] for o in variants)):
                raise ValueError('selected_rotation_mapping')
            previous = auth['body'].get(mapping['login_field'])
            replacement = self.private_bodies.get(mapping['operation_id'], {}).get(mapping['change_field'])
            if not isinstance(previous, str) or not isinstance(replacement, str) or previous == replacement:
                raise ValueError('selected_rotation_mapping')
            self.rotation = deepcopy(mapping)
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
                if scopes & {'current', 'revoked'}: raise ValueError('selected_mixed_auth_unsupported')
                request_auth[name] = 'anonymous'
        # Технические contract checks не подменяются ожиданиями отдельных карточек.
        required = {o['operation_id'] for o in manifest['observations']}
        required.update(value['response']['operation_id'] for observation in manifest['observations']
            for value in observation['where'].get('parameters', {}).values()
            if isinstance(value, dict) and set(value) == {'response'})
        observations = {item['id']: item for item in manifest['observations']}
        for condition in manifest['conditions']:
            if condition['predicate'] != 'value_equal':
                continue
            parameters = condition['parameters']
            source = parameters['source']
            name = observations[condition['observation_id']]['operation_id']
            if (private_response_path(self._project_extraction, name, parameters['path'])
                    or private_response_path(self._project_extraction, source['operation_id'], source['path'])):
                raise ValueError('selected_private_response_path')
            required.add(source['operation_id'])
        if self.rotation:
            required.add(self.rotation['operation_id'])
        CatalogWorkflow.__init__(self, catalog, config, goal=manifest['goal'],
            conditions={name: [] for name in config.snapshot['operations']}, extraction=extraction,
            binding_rules=binding_rules, auth=auth, request_auth=request_auth,
            approval=self._approve, current_config=current_config, required_operations=required,
            public_response_headers=public_response_headers)
        if not self.allowed: raise ValueError('selected_no_allowed_operations')
        if any(o['operation_id'] not in self.allowed for o in manifest['observations']):
            raise ValueError('selected_operation_not_allowed')
        if any(item.get('private') and item['binding']['operation_id'] not in self.allowed
                for item in manifest['input_slots']):
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
        if (self.auth or {}).get('cookie_name'):
            from api_agent.execution.session_cookie import cookie_schemes
            if self._authenticated and self.auth['cookie_name'] in cookie_schemes(self.catalog.operations[name]):
                return 'authenticated'
            return mode
        if self.auth and self._authenticated and name != self.auth['login_operation']: return 'authenticated'
        return mode

    def old_epoch_allowed(self, session, args, principal):
        credential = session.revoked_credential(args, session._request_parameters)
        return (credential is not None and credential['principal'] == principal['actor']
                and credential['auth_epoch'] == principal['auth_epoch'])

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
    def login_body(self):
        body, variant = super().login_body()
        mapping = self.workflow.rotation
        if mapping is None:
            return body, variant
        name = mapping['operation_id']
        replacement = self.workflow.private_bodies[name][mapping['change_field']]
        passed = {v['exchange_id'] for v in self.run['validations'] if v['contract_verdict'] == 'pass'}
        for index, exchange in reversed(list(enumerate(self.exchanges))):
            principal = exchange.get('request_principal', {})
            status = (exchange.get('response') or {}).get('status')
            if (exchange['operation_id'] == name and exchange['id'] in passed
                    and exchange['outcome'] == 'received' and not exchange.get('truncated')
                    and type(status) is int and 200 <= status < 300 and principal.get('mode') == 'authenticated'
                    and (exchange['request'].get('json') or {}).get(mapping['change_field']) == replacement):
                later = self.exchanges[index + 1:]
                if any(e.get('login_variant') == 'previous' for e in later):
                    body[mapping['login_field']] = replacement
                    return body, 'replacement'
                if (self.state.principal is not None or self.state.auth_epoch != principal['auth_epoch'] + 1
                        or not confirmed_revocation(self.catalog, later,
                            self.workflow.auth['logout_operations'], principal)):
                    raise ValueError('selected_rotation_logout_required')
                return body, 'previous'
        return body, variant

    def revoked_credential(self, args, supplied):
        probe = self.workflow.revoked_probe
        if (probe is None or args['operation_id'] != probe['operation_id'] or self.state.principal is not None
                or 'credential_ref' in args or not subset(probe['where'].get('parameters', {}), supplied)
                or not subset(probe['where'].get('body', {}), args.get('body', {}))):
            return None
        auth = self.workflow.auth
        epoch = self.state.auth_epoch - 1
        if any((e.get('request_principal') or {}).get('mode') == 'old_epoch'
                and e['request_principal'].get('auth_epoch') == epoch for e in self.exchanges):
            return None
        matches = [record for record in self.state.public() if record['secret']
            and record['operation_id'] == auth['login_operation'] and record['path'] == auth['token_path']
            and record.get('auth_epoch') == epoch and record.get('principal') and confirmed_revocation(
                self.catalog, self.exchanges, auth['logout_operations'],
                {'actor': record['principal'], 'auth_epoch': epoch})]
        return matches[0] if len(matches) == 1 else None

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

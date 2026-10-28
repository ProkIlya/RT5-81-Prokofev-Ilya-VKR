"""Исполнение поддержанного каталога через существующий ограниченный agent loop.

Идентификаторы операций являются данными контракта. Полномочия, auth mapping,
критерии цели и связи runtime refs поступают от доверенной команды приложения.
"""

from copy import deepcopy
from api_agent.execution.state import TestPassword
from api_agent.bounded_json import validate_tree
from api_agent.policy.config import decision
from api_agent.validation import prepare_expectations
from api_agent.execution.contracts import _validate_extraction

from api_agent.execution.contracts import CatalogLimits
from api_agent.execution.observations import CatalogObservations

class CatalogWorkflow(CatalogObservations):
    """Закрепить допустимый каталог и проверяемые условия до первой модели."""

    def __init__(
        self,
        catalog,
        config,
        *,
        goal,
        conditions,
        extraction=None,
        binding_rules=None,
        auth=None,
        approval=None,
        current_config=None,
        anonymous_operations=(),
        goal_coverage="partial",
        selection_scopes=None,
        request_auth=None,
        occurrences=None,
        admission_rules=None,
        parameter_namespaces=False,
        required_operations=None,
        public_response_headers=None,
    ):
        self.catalog, self.config = catalog, config
        from api_agent.execution.http_metadata import public_response_headers as classify
        self.public_response_headers=classify([] if public_response_headers is None else public_response_headers)
        self.parameter_namespaces = parameter_namespaces
        self.workflow = self
        self.approval = approval
        self.current_config = current_config or (lambda: config)
        self.goal = goal
        if goal_coverage not in {"partial", "complete"}:
            raise ValueError("invalid_goal_coverage")
        # Полноту требований объявляет доверенная команда, не вывод модели.
        self.goal_coverage = goal_coverage
        self.conditions = deepcopy(conditions)
        self.extraction = deepcopy(extraction or {})
        self.binding_rules = deepcopy(binding_rules or {})
        self.auth = deepcopy(auth)
        self.anonymous_operations = frozenset(anonymous_operations)
        self.request_auth = deepcopy(request_auth or {})
        self.occurrences = deepcopy(occurrences or [])
        validate_tree(self.occurrences, max_bytes=16384)
        if not isinstance(self.occurrences, list) or len(self.occurrences) > 32:
            raise ValueError('invalid_occurrences')
        ids = set()
        for slot in self.occurrences:
            if (not isinstance(slot, dict) or set(slot) - {'id', 'operation_id', 'body', 'parameters', 'auth_mode', 'conditions'}
                or not isinstance(slot.get('id'), str) or not slot['id'] or slot['id'] in ids
                or slot.get('operation_id') not in config.snapshot['operations']
                or slot.get('auth_mode', 'authenticated') not in {'authenticated', 'anonymous', 'anonymous_negative'}):
                raise ValueError('invalid_occurrence')
            if slot.get('auth_mode') == 'anonymous_negative' and not any(
                c.get('predicate') == 'status_in' and c.get('parameters') == {'values': [401]}
                and c.get('intent') == 'negative' and c.get('preconditions') == 'verified'
                for c in slot.get('conditions', [])):
                raise ValueError('anonymous_requires_explicit_negative_expectation')
            ids.add(slot['id'])
        if not isinstance(self.request_auth, dict) or any(
            n not in config.snapshot['operations'] or mode not in
            {'authenticated', 'anonymous', 'anonymous_negative'}
            for n, mode in self.request_auth.items()
        ):
            raise ValueError('invalid_request_auth')
        self.anonymous_operations |= frozenset(n for n, mode in self.request_auth.items()
                                               if mode == 'anonymous_negative')
        for name in self.anonymous_operations:
            declared = self.conditions.get(name, [])
            if not any(
                d.get("predicate") == "status_in"
                and d.get("parameters") == {"values": [401]}
                and d.get("intent") == "negative"
                and d.get("preconditions") == "verified"
                for d in declared
            ):
                raise ValueError("anonymous_requires_explicit_negative_expectation")
        validate_tree(
            {
                "conditions": conditions,
                "extraction": self.extraction,
                "bindings": self.binding_rules,
            },
            max_bytes=65536,
        )
        if (
            not isinstance(goal, str)
            or not goal.strip()
            or not isinstance(conditions, dict)
            or not conditions
        ):
            raise ValueError("explicit_goal_conditions_required")
        self.allowed, self.excluded = [], {}
        snap = config.snapshot
        if snap["spec_hash"] != catalog.spec_hash:
            raise ValueError("stale_project_config")
        for name, grant in sorted(snap["operations"].items()):
            op = catalog.operations.get(name)
            if op is None or grant["operation_hash"] != catalog.operation_hashes[name]:
                raise ValueError("stale_operation_hash")
            why = self.capability(op, name)
            if why or decision(snap, name, catalog.operation_hashes[name]) == "DENY":
                self.excluded[name] = why or "policy_denied"
            else:
                self.allowed.append(name)
        if not snap['operations'] or not set(conditions) <= set(snap['operations']):
            raise ValueError("empty_or_unsupported_run_catalog")
        required = set(conditions) if required_operations is None else set(required_operations)
        if not required <= set(conditions):raise ValueError('invalid_required_operations')
        unavailable = required - set(self.allowed)
        self.feasibility_stop = ('policy_denied' if all(self.excluded.get(n) == 'policy_denied' for n in unavailable)
                                 else 'unsupported_goal_capability') if unavailable else None
        declared = set(snap['operations']) if self.feasibility_stop else set(self.allowed)
        from api_agent.execution.admission import prepare_admission
        self.admission_rules = prepare_admission(admission_rules or {}, declared, ids)
        from api_agent.validation.selection_scope import prepare_scopes
        self.selection_scopes = prepare_scopes(catalog, conditions, {} if selection_scopes is None else selection_scopes)
        if goal_coverage == 'complete' and any(
            catalog.operations[name]['method']=='GET'
            and any(p['in']=='query' for p in catalog.operations[name]['parameters'])
            and name not in self.selection_scopes for name in conditions):
            raise ValueError('complete_goal_requires_selection_scope')
        self._contexts = {}
        self._redactors = {}
        self.create = (self.allowed or sorted(snap['operations']))[0]
        for name, paths in self.extraction.items():
            if name not in declared:
                raise ValueError("foreign_extraction_operation")
            status = self._status(name)
            for path, rule in paths.items():
                if set(rule) != {"type", "secret"} or type(rule["secret"]) is not bool:
                    raise ValueError("invalid_extraction_rule")
                _validate_extraction(
                    catalog, name, status, path, rule["type"], secret=rule["secret"]
                )
        for target, bindings in self.binding_rules.items():
            if target not in declared:
                raise ValueError("foreign_binding_target")
            for destination, rule in bindings.items():
                if rule.get("source_path") not in self.extraction.get(
                    rule.get("source_operation"), {}
                ):
                    raise ValueError("unsupported_binding_source")
                if destination not in {
                    p["name"] for p in catalog.operations[target]["parameters"]
                }:
                    raise ValueError("unsupported_binding_destination")
        if self.auth:
            required = {"login_operation", "token_path", "body"}
            if not required <= set(self.auth) or set(self.auth) - required - {"public_body_fields", "logout_operations"}:
                raise ValueError("invalid_auth_mapping")
            logout = self.auth.get('logout_operations', [])
            if not isinstance(logout, list) or any(n not in self.allowed for n in logout):
                raise ValueError('invalid_logout_operations')
            public = self.auth.get("public_body_fields", [])
            if not isinstance(public, list) or len(public) > 16 or any(not isinstance(k, str) or k not in self.auth["body"] for k in public) or len(set(public)) != len(public):
                raise ValueError("invalid_public_identity_mapping")
            login = self.auth["login_operation"]
            rule = self.extraction.get(login, {}).get(self.auth["token_path"])
            if rule != {"type": "string", "secret": True}:
                raise ValueError("secret_auth_extraction_required")

    def capability(self, op, name):
        """Отклонять неподдержанную сериализацию явно, не угадывать её по names."""
        parameters = op["parameters"]
        names = [p["name"] for p in parameters]
        if not self.parameter_namespaces and len(set(names)) != len(names):
            return "ambiguous_parameter_name"
        if any(
            p["in"] not in ({"path", "query", "header"} if self.parameter_namespaces else {"path", "query"})
            or p["schema"]["type"] not in {"string", "integer", "number", "boolean"}
            for p in parameters
        ):
            return "unsupported_parameter_serialization"
        if any(p['in'] == 'header' and (p['name'] in {'host', 'connection', 'content-length', 'transfer-encoding',
                'cookie', 'te', 'trailer', 'upgrade', 'expect'} or p['name'].startswith('proxy-')) for p in parameters):
            return 'unsafe_transport_header'
        if (
            "requestBody" in op
            and "application/json" not in op["requestBody"]["content"]
        ):
            return "unsupported_request_media"
        if (
            "requestBody" in op
            and op["requestBody"]["content"]["application/json"]["schema"]["type"]
            != "object"
        ):
            return "unsupported_request_body_type"
        if self.auth_mode(name) == 'authenticated' and self.auth is None:
            return 'unsupported_auth_mapping'
        if op["security"]:
            alternatives = [a for a in op['security'] if a]
            if (
                (self.auth_mode(name) == 'authenticated' and self.auth is None)
                or len(alternatives) > 1
            ):
                return "unsupported_auth_mapping"
            if any(len(a) != 1 or next(iter(a.values())).get('scheme') != 'bearer' for a in alternatives):
                return "unsupported_security_scheme"
        if not any(
            status.isdigit() and int(status) < 300 for status in op["responses"]
        ):
            return "unsupported_response_basis"
        return None

    def _status(self, operation):
        return min(
            int(s)
            for s in self.catalog.operations[operation]["responses"]
            if s.isdigit() and int(s) < 300
        )

    def auth_mode(self, name):
        """Выбор principal запроса независим от декларации OpenAPI security."""
        if name in self.anonymous_operations:
            return 'anonymous_negative'
        if name in self.request_auth:
            return self.request_auth[name]
        security = self.catalog.operations[name]['security']
        return 'authenticated' if security and {} not in security else 'anonymous'

    def request_mode(self, name, slot=None):
        return slot.get('auth_mode', self.auth_mode(name)) if slot else self.auth_mode(name)

    def bindings_for(self, name, occurrence_id=None):
        """Базовый каталог задаёт связи операции; goal уточняет отдельный вызов."""
        return self.binding_rules.get(name, {})

    def runtime_manifest(self):
        """Только публичные application descriptors, без тела credentials."""
        return deepcopy({**({'parameter_namespaces': True} if self.parameter_namespaces else {}),
            **({'public_response_headers':sorted(self.public_response_headers)} if self.public_response_headers else {}),
            'request_auth': self.request_auth, 'occurrences': self.occurrences,
            'admission_rules': self.admission_rules, 'binding_rules': self.binding_rules,
            'extraction': self.extraction, 'selection_scopes': self.selection_scopes,
            'conditions': self.conditions,
            'auth': ({k: v for k, v in self.auth.items() if k != 'body'} if self.auth else None),
            'anonymous_operations': sorted(self.anonymous_operations)})



    def rules(self):
        return deepcopy(self.extraction)

    def secrets(self):
        return tuple(
            v
            for k, v in (self.auth or {}).get("body", {}).items()
            if isinstance(v, str) and v and not isinstance(v, TestPassword) and k not in (self.auth or {}).get("public_body_fields", [])
        )


    def prepared(self, catalog, message, goal):
        """USER_DEFINED basis назначает приложение до наблюдения ответа."""
        result = {}
        for name in self.allowed:
            conditions = self.conditions.get(name)
            if conditions is not None:
                conditions = [
                    {**d, "source": "USER_DEFINED", "basis": {"message_id": message}}
                    for d in conditions
                ]
            result[name] = prepare_expectations(
                catalog,
                conditions,
                message,
                goal,
                operation_id=name,
                expected_status=self._status(name),
            )
            if name in self.conditions and catalog.operations[name]['method']=='GET':
                scope = self.selection_scopes.get(name, {'mode':'first_request'})
                for expectation in result[name]:
                    expectation['selection_scope'] = deepcopy(scope)
        for slot in self.occurrences:
            if 'conditions' in slot:
                conditions = [{**d, 'source': 'USER_DEFINED', 'basis': {'message_id': message}}
                              for d in slot['conditions']]
                result['occurrence:' + slot['id']] = prepare_expectations(catalog, conditions,
                    message, goal, operation_id=slot['operation_id'], expected_status=self._status(slot['operation_id']))
        return result

    def prepared_key(self, args):
        slot = next((s for s in self.occurrences if s['id'] == args.get('occurrence_id')), None)
        return 'occurrence:' + slot['id'] if slot and 'conditions' in slot else args['operation_id']



    default_limits = CatalogLimits


    def make_session(self, target, catalog, run, limits):
        from api_agent.execution.session import CatalogSession
        return CatalogSession(target, catalog, self, run, limits)

    def _effective(self):
        current = self.current_config().snapshot
        fixed = self.config.snapshot
        if any(current[k] != fixed[k] for k in ("project_id", "target", "spec_hash")):
            raise ValueError("foreign_current_config")
        policies = {}
        for name in self.allowed:
            op_hash = self.catalog.operation_hashes[name]
            old, new = decision(fixed, name, op_hash), decision(current, name, op_hash)
            if "DENY" not in {old, new}:
                policies[name] = "CONFIRM" if "CONFIRM" in {old, new} else "ALLOW"
        return current, policies

    def decision(self,name,args):
        """Внутренний HTTP adapter, без native model tool registry."""
        if name!='execute_operation':raise ValueError('unknown_tool')
        if args['operation_id'] not in self.allowed:raise ValueError('operation_denied')

    def next_occurrence(self, exchanges):
        """Последовательный manifest принят приложением до первого HTTP."""
        done = {e.get('occurrence_id') for e in exchanges if e.get('outcome') == 'received'}
        return next((s for s in self.occurrences if s['id'] not in done), None)

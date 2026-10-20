"""Принятый manifest использует существующие executor/policy/auth/validators."""
from copy import deepcopy
import json
import time

from .agent.catalog import CatalogWorkflow, CatalogSession, PhaseDecision
from .goals import check_goal, resolve_inputs, input_references
from .policy import PolicyStop
from .validation import fingerprint, schema_issues


def _tool(name, properties, required, description):
    return {'type': 'function', 'function': {'name': name, 'description': description,
        'parameters': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}}


class GoalWorkflow(CatalogWorkflow):
    """Публичная проекция отделена от private body; прав сверх config нет."""
    application_tools = {'check_goal', 'ask_user'}

    def __init__(self, catalog, config, accepted, *, proposal, human, budget, preparation,
                 extraction=None, binding_rules=None, auth=None, private_bodies=None, current_config=None):
        self.accepted, self.human, self.preparation_budget = accepted, human, budget
        self.preparation = deepcopy(preparation)
        self.preparation_active = preparation.get('active_seconds', 0)
        self.private_bodies = deepcopy(private_bodies or {})
        self.inputs = {}
        manifest = accepted.snapshot
        conditions, occurrences = {}, []
        extraction, binding_rules = deepcopy(extraction or {}), deepcopy(binding_rules or {})
        self._project_extraction, self._project_bindings = deepcopy(extraction), deepcopy(binding_rules)
        for slot in manifest['slots']:
            name = slot['operation_id']
            descriptors = []
            for condition in manifest['conditions']:
                if condition['slot'] == slot['id'] and condition['predicate'] in {'status_in', 'json_value_equals', 'json_collection_matches'} and not input_references(condition['parameters']):
                    descriptors.append({'predicate': condition['predicate'], 'parameters': deepcopy(condition['parameters']),
                        'intent': 'negative' if slot['auth_mode'] in {'anonymous_negative', 'old_epoch'} else 'positive', 'preconditions': 'verified'})
            if not descriptors:
                status = min(int(s) for s in catalog.operations[name]['responses'] if s.isdigit() and 200 <= int(s) < 300)
                descriptors = [{'predicate': 'status_in', 'parameters': {'values': [status]}, 'intent': 'positive', 'preconditions': 'verified'}]
            conditions.setdefault(name, descriptors)
            occurrence = {'id': slot['id'], 'operation_id': name, 'auth_mode': 'anonymous_negative' if slot['auth_mode'] == 'old_epoch' else slot['auth_mode'], 'conditions': descriptors}
            if slot['parameters'] and not any(isinstance(v, dict) for v in slot['parameters'].values()): occurrence['parameters'] = slot['parameters']
            if 'body' in slot and name not in self.private_bodies and not input_references(slot['body']): occurrence['body'] = slot['body']
            occurrences.append(occurrence)
            for parameter, value in slot['parameters'].items():
                if isinstance(value, dict) and 'from_slot' in value:
                    source = next(s for s in manifest['slots'] if s['id'] == value['from_slot'])
                    p = next(p for p in catalog.operations[name]['parameters'] if p['name'] == parameter)
                    existing = extraction.setdefault(source['operation_id'], {}).get(value['path'])
                    if existing and (existing.get('secret') or existing.get('type') != p['schema']['type']):
                        raise ValueError('goal_private_binding')
                    original = binding_rules.get(name, {}).get(parameter)
                    if original and any(original.get(k) != v for k, v in {'source_operation': source['operation_id'], 'source_path': value['path']}.items()):
                        raise ValueError('goal_binding_changed')
                    extraction[source['operation_id']][value['path']] = {'type': p['schema']['type'], 'secret': False}
                    binding_rules.setdefault(name, {})[parameter] = {'source_operation': source['operation_id'],
                        'source_path': value['path'], 'source_occurrence': source['id']}
        self._contract = {'manifest': manifest, 'manifest_hash': accepted.hash, 'proposal': deepcopy(proposal)}
        super().__init__(catalog, config, goal=manifest['goal'], conditions=conditions,
            extraction=extraction, binding_rules=binding_rules, auth=auth, occurrences=occurrences,
            goal_coverage='complete', approval=self._approve, current_config=current_config)

    def _approve(self, proposal):
        def redact(value):
            if isinstance(value, str):
                for secret in self.secrets(): value = value.replace(secret, '[REDACTED]')
                return value
            if isinstance(value, dict): return {k: redact(v) for k, v in value.items()}
            if isinstance(value, list): return [redact(v) for v in value]
            return value
        return self.human.approve(redact(proposal))

    def old_epoch_allowed(self, session, args, principal):
        slot = next((s for s in self.accepted.snapshot['slots'] if s['id'] == args.get('occurrence_id')), {})
        return (slot.get('auth_mode') == 'old_epoch' and principal['auth_epoch'] == slot['auth_epoch']
                and session.state.auth_epoch == session._old_current_epoch
                and principal['auth_epoch'] < session.state.auth_epoch
                and self.catalog.operations[slot['operation_id']]['method'] == 'GET'
                and any(c['slot'] == slot['id'] and c['predicate'] == 'status_in' and c['parameters'] == {'values': [401]}
                        for c in self.accepted.snapshot['conditions']))

    def runtime_manifest(self):
        return {**super().runtime_manifest(), 'goal_contract': deepcopy(self._contract)}

    def storage_metadata(self):
        if hasattr(self, '_snapshot_metadata'): return deepcopy(self._snapshot_metadata)
        metadata = {'extraction': self._project_extraction, 'binding_rules': self._project_bindings,
                    'selection_scopes': {}, 'request_auth': {}, 'occurrences': [], 'admission_rules': {}}
        if self.auth:
            metadata['auth'] = {k: self.auth[k] for k in ('login_operation', 'token_path')}
            metadata['auth']['public_body_fields'] = list(self.auth.get('public_body_fields', []))
        return deepcopy(metadata)

    def system(self):
        return super().system() + ' Follow accepted occurrence slots exactly. Use check_goal for the application checklist. ask_user can fill only a declared typed input; new requirements need a new Run.'

    def initial_context(self, run, counter, **kwargs):
        return super().initial_context(run, counter, **kwargs) + '\nAccepted goal: ' + json.dumps(self.accepted.snapshot, ensure_ascii=False)

    def registry(self, catalog):
        return super().registry(catalog) + [
            _tool('check_goal', {}, [], 'Check accepted requirements using saved evidence only, no HTTP.'),
            _tool('ask_user', {'question': {'type': 'string', 'maxLength': 2000}, 'input_slot': {'type': 'string'}},
                  ['question', 'input_slot'], 'Fill a previously accepted typed input slot; cannot change requirements.')]

    def phase_decision(self, run, registry):
        result = super().phase_decision(run, registry)
        complete = any(((s.get('observation') or {}).get('data') or {}).get('checks_complete') is True for s in run.get('steps', []))
        offered = result.tools + [t for t in registry if t['function']['name'] in self.application_tools
                                 and (not complete or t['function']['name'] != 'ask_user')
                                 and t['function']['name'] not in {x['function']['name'] for x in result.tools}]
        for tool in offered:
            if tool['function']['name'] == 'execute_operation':
                body = tool['function']['parameters'].get('properties', {}).get('body')
                slot = self.next_occurrence(run.get('exchanges', []))
                private = self.private_bodies.get(slot['operation_id'], {}) if slot else {}
                if body and private:
                    body['properties'] = {k: v for k, v in body.get('properties', {}).items() if k not in private}
                    body['required'] = [k for k in body.get('required', []) if k not in private]
        return PhaseDecision(offered, result.tool_choice if complete else 'required')

    def decision(self, name, args):
        if name in self.application_tools:
            if (name == 'check_goal' and args) or (name == 'ask_user' and set(args) != {'question', 'input_slot'}):
                raise ValueError('invalid_application_tool')
            return
        return super().decision(name, args)

    def handle_tool(self, session, name, args):
        if name == 'check_goal':
            return check_goal(self.accepted, self.catalog, session.run)
        descriptor = next((s for s in self.accepted.snapshot.get('input_slots', []) if s['id'] == args['input_slot']), None)
        if descriptor is None: raise PolicyStop('goal_revision_required')
        for slot in self.accepted.snapshot['slots']:
            refs = input_references(slot) | set().union(*(input_references(c) for c in self.accepted.snapshot['conditions'] if c['slot'] == slot['id']))
            if descriptor['id'] in refs and any(e.get('occurrence_id') == slot['id'] for e in session.exchanges):
                raise PolicyStop('input_slot_locked')
        waited = self.human.waited_seconds
        try: value = self.human.ask(args['question'], phase='run')
        except ValueError as error: raise PolicyStop(str(error)) from None
        finally: session.deadline += self.human.waited_seconds - waited
        if schema_issues(value, {'type': descriptor['type']}): raise PolicyStop('invalid_human_input')
        self.inputs[descriptor['id']] = value
        session.run['goal_inputs'] = deepcopy(self.inputs)
        session.run['human_events'] = self.human.public_events()
        return {'input_slot': descriptor['id'], 'value': value}

    def completion(self, session, validations):
        base = super().completion(session, validations)
        if base == 'fail': return 'fail'
        checklist = check_goal(self.accepted, self.catalog, {'exchanges': session.exchanges, 'goal_inputs': self.inputs})
        return {'PASS': 'pass', 'FAIL': 'fail', 'UNKNOWN': 'not_checked'}[checklist['verdict']]

    def make_session(self, target, catalog, run, limits):
        run['preparation'] = deepcopy(self.preparation)
        run['human_events'] = self.human.public_events()
        session = GoalSession(target, catalog, self, run, limits)
        session.deadline -= self.preparation_active
        return session

    def secrets(self):
        return super().secrets() + self.human.secrets() + tuple(v for body in self.private_bodies.values() for v in body.values() if isinstance(v, str))


class GoalSession(CatalogSession):
    def admit(self, args):
        slot = next((s for s in self.workflow.accepted.snapshot['slots'] if s['id'] == args.get('occurrence_id')), None)
        if slot is None: raise PolicyStop('occurrence_denied')
        references = input_references(slot) | set().union(*(input_references(c) for c in self.workflow.accepted.snapshot['conditions'] if c['slot'] == slot['id']))
        if not references <= self.workflow.inputs.keys(): raise PolicyStop('clarification_required')
        if slot['auth_mode'] != 'old_epoch' and self.state.auth_epoch != slot['auth_epoch']:
            raise PolicyStop('stale_auth_epoch')
        checklist = check_goal(self.workflow.accepted, self.catalog, {'exchanges': self.exchanges, 'goal_inputs': self.workflow.inputs})
        for dependency in slot['depends_on']:
            required = {c['id'] for c in self.workflow.accepted.snapshot['conditions'] if c['slot'] == dependency}
            if any(c['verdict'] != 'PASS' for c in checklist['conditions'] if c['condition_id'] in required):
                raise PolicyStop('precondition_not_verified')
        try: resolved_body = resolve_inputs(slot.get('body'), self.workflow.inputs)
        except KeyError: raise PolicyStop('clarification_required') from None
        try: parameters = resolve_inputs(slot['parameters'], self.workflow.inputs)
        except KeyError: raise PolicyStop('clarification_required') from None
        fixed_parameters = {k: v for k, v in parameters.items() if not (isinstance(v, dict) and 'from_slot' in v)}
        if args.get('parameters', {}) != fixed_parameters: raise PolicyStop('occurrence_inputs_changed')
        private = self.workflow.private_bodies.get(slot['operation_id'])
        if 'body' in slot and not private and args.get('body') != resolved_body:
            raise PolicyStop('occurrence_inputs_changed')
        if private:
            public = {k: v for k, v in resolved_body.items() if k not in private}
            if args.get('body', {}) != public: raise PolicyStop('occurrence_inputs_changed')
            args = deepcopy(args)
            args['body'] = {**public, **private}
        return super().admit(args)

    def authorize(self, args, result):
        slot = next(s for s in self.workflow.accepted.snapshot['slots'] if s['id'] == args['occurrence_id'])
        if slot['auth_mode'] == 'old_epoch':
            records = [r for r in self.state.public() if r.get('secret') and r.get('auth_epoch') == slot['auth_epoch']
                       and r['operation_id'] == self.workflow.auth['login_operation'] and r['path'] == self.workflow.auth['token_path']]
            if len(records) != 1: raise PolicyStop('old_auth_reference_denied')
            record = records[0]
            self._old_current_epoch = self.state.auth_epoch
            self._request_principal = {'mode': 'old_epoch', 'auth_epoch': record['auth_epoch'], 'actor': record['principal']}
            self._headers = {'Authorization': 'Bearer ' + self.state.resolve(record['ref'], 'string', secret=True)}
            if not self.workflow.old_epoch_allowed(self, args, self._request_principal): raise PolicyStop('old_auth_reference_denied')
        waited = self.workflow.human.waited_seconds
        try: return super().authorize(args, result)
        finally: self.deadline += self.workflow.human.waited_seconds - waited

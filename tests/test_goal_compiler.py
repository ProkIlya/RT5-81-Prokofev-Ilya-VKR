"""S09b1: смысл/полнота/границы проверяются до HTTP, не JSON-уверенностью."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig
from api_agent.validation import fingerprint


def fixture(domain='pet', *, wrapped=False):
    operation = {'pet': 'readPet', 'weather': 'readForecast', 'stock': 'readStock'}[domain]
    field = {'pet': 'name', 'weather': 'temperature', 'stock': 'quantity'}[domain]
    scalar = {'type': 'string'} if domain == 'pet' else {'type': 'number'}
    schema = {'type': 'object', 'required': [field], 'properties': {field: scalar}}
    if wrapped:
        schema = {'type': 'object', 'properties': {'items': {'type': 'array', 'items': schema}, 'complete': {'type': 'boolean'}}, 'required': ['items', 'complete']}
    catalog = parse_document({'openapi': '3.0.3', 'info': {'title': domain, 'version': '1'},
        'paths': {'/' + domain: {'get': {'operationId': operation, 'responses': {'200': {
            'description': 'ok', 'content': {'application/json': {'schema': schema}}}}}}}})
    config = ProjectConfig.create('p', 'http://127.0.0.1:18082', catalog, mode='READ_ONLY',
                                  resettable=True, operations={operation: 'A'})
    goal = f'Проверь {operation}: статус 200.'
    proposal = {'version': 1, 'goal_hash': fingerprint(goal), 'spec_hash': catalog.spec_hash,
        'config_hash': fingerprint(config.snapshot),
        'requirements': [{'id': 'r1', 'span': [0, len(goal)], 'interpretation': goal}],
        'slots': [{'id': 's1', 'operation_id': operation, 'parameters': {}, 'auth_mode': 'anonymous',
                   'actor': 'anonymous', 'auth_epoch': 0, 'depends_on': []}],
        'conditions': [{'id': 'c1', 'requirement_id': 'r1', 'slot': 's1', 'predicate': 'status_in',
                        'parameters': {'values': [200]}, 'source': 'MODEL_PROPOSED', 'basis': None}],
        'answer_fields': [{'id': 'a1', 'requirement_id': 'r1', 'slot': 's1', 'path': '/' + field if not wrapped else '/items'}],
        'unresolved': []}
    return catalog, config, goal, proposal


class GoalCompilerTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('api_agent.goals'), 'Нет компилятора S09b1')
        from api_agent import goals
        return goals

    def compile(self, case, *, confirm=False):
        api = self.api()
        catalog, config, goal, proposal = case
        confirmation = {'proposal_hash': fingerprint(proposal), 'approved': True, 'complete': True} if confirm else None
        return api.compile_goal(catalog, config, goal, proposal, confirmation=confirmation)

    def test_three_unrelated_apis_auto_accept_independently_supported_form(self):
        for domain in ('pet', 'weather', 'stock'):
            accepted = self.compile(fixture(domain))
            self.assertEqual(accepted.snapshot['acceptance'], 'registered_form')
            self.assertEqual(accepted.snapshot['coverage'], 'complete')
            changed = accepted.snapshot
            changed['coverage'] = 'partial'
            self.assertEqual(accepted.snapshot['coverage'], 'complete')

    def test_free_russian_goal_needs_explicit_interpretation_acceptance(self):
        case = list(fixture())
        case[2] = 'Убедись, что сведения о питомце доступны, и покажи имя.'
        case[3]['goal_hash'] = fingerprint(case[2])
        case[3]['requirements'][0]['span'] = [0, len(case[2])]
        case[3]['requirements'][0]['interpretation'] = 'Вызвать readPet, проверить статус 200, показать /name.'
        with self.assertRaisesRegex(ValueError, 'goal_acceptance_required'):
            self.compile(case)
        accepted = self.compile(case, confirm=True)
        self.assertEqual(accepted.snapshot['acceptance'], 'user_confirmed')
        self.assertEqual(accepted.snapshot['conditions'][0]['source'], 'USER_DEFINED')

    def test_missing_requirement_or_answer_field_is_not_complete(self):
        for mutation in ('gap', 'orphan', 'no_answer'):
            case = list(fixture())
            if mutation == 'gap':
                case[3]['requirements'][0]['span'][1] -= 10
            elif mutation == 'orphan':
                case[3]['requirements'].append({'id': 'r2', 'span': [0, 1], 'interpretation': 'missing'})
            else:
                case[3]['answer_fields'] = []
            with self.assertRaises(ValueError):
                self.compile(case, confirm=True)

    def test_quotes_json_false_source_and_complete_do_not_grant_acceptance(self):
        for mutation in ('source', 'complete', 'extra_condition'):
            case = list(fixture())
            if mutation == 'source':
                case[3]['conditions'][0]['source'] = 'USER_DEFINED'
            elif mutation == 'complete':
                case[3]['complete'] = True
            else:
                case[3]['conditions'][0]['parameters']['values'] = [201]
            with self.assertRaises(ValueError):
                self.compile(case)

    def test_stale_goal_config_spec_and_confirmation_are_rejected(self):
        for key in ('goal_hash', 'config_hash', 'spec_hash'):
            case = list(fixture())
            case[3][key] = 'stale'
            with self.assertRaises(ValueError):
                self.compile(case, confirm=True)
        api = self.api()
        cat, cfg, goal, proposal = fixture()
        with self.assertRaisesRegex(ValueError, 'goal_acceptance_required'):
            api.compile_goal(cat, cfg, goal, proposal, force_confirmation=True,
                confirmation={'proposal_hash': 'other', 'approved': True, 'complete': True})

    def test_foreign_slots_unsupported_path_unit_and_self_comparison_fail(self):
        for mutation in ('foreign_slot', 'path', 'unit', 'self'):
            case = list(fixture())
            condition = case[3]['conditions'][0]
            if mutation == 'foreign_slot':
                condition['slot'] = 'foreign'
            elif mutation == 'path':
                condition.update(predicate='json_value_equals', parameters={'path': '/missing', 'value': 'x'})
            elif mutation == 'unit':
                condition.update(predicate='json_value_equals', parameters={'path': '/name', 'value': 'x', 'unit': 'kg'})
            else:
                condition.update(predicate='value_equal', parameters={'left': {'slot': 's1', 'path': '/name'}, 'right': {'slot': 's1', 'path': '/name'}})
            with self.assertRaises(ValueError):
                self.compile(case, confirm=True)

    def test_collection_cannot_claim_all_pages_from_full_body(self):
        case = list(fixture(wrapped=True))
        case[3]['conditions'][0].update(predicate='json_collection_matches',
            parameters={'path': '/items', 'rows': [{'/name': 'Murka'}], 'mode': 'exact'})
        with self.assertRaisesRegex(ValueError, 'goal_collection_scope_required'):
            self.compile(case, confirm=True)
        case[3]['slots'][0]['scope'] = {'mode': 'response', 'parameters': {}}
        accepted = self.compile(case, confirm=True)
        self.assertEqual(accepted.snapshot['slots'][0]['scope']['mode'], 'response')

    def test_rounding_uses_user_precision_decimal_half_up(self):
        case = list(fixture('weather'))
        case[2] = 'Проверь расчёт: ожидаю 41.01.'
        case[3]['goal_hash'] = fingerprint(case[2])
        case[3]['requirements'][0]['span'] = [0, len(case[2])]
        case[3]['conditions'][0].update(predicate='json_number_rounded', parameters={'path': '/temperature', 'expected': '41.01'})
        accepted = self.compile(case, confirm=True)
        api = self.api()
        run = {'exchanges': [self.exchange('readForecast', {'temperature': 41.005})]}
        result = api.check_goal(accepted, case[0], run)
        self.assertEqual(result['verdict'], 'PASS')
        run['exchanges'][0]['response']['json']['temperature'] = 41.004
        self.assertEqual(api.check_goal(accepted, case[0], run)['verdict'], 'FAIL')

    def test_calculation_expectation_cannot_be_invented_by_generator(self):
        case = list(fixture('weather'))
        case[3]['conditions'][0].update(predicate='json_number_rounded', parameters={'path': '/temperature', 'expected': '41.01'})
        with self.assertRaisesRegex(ValueError, 'goal_expected_input_required'):
            self.compile(case, confirm=True)

    def test_create_read_identity_requires_dynamic_request_target(self):
        from test_catalog_execution import catalog_variant
        cat = catalog_variant('id')
        cfg = ProjectConfig.create('p', 'http://127.0.0.1:18082', cat, mode='TEST', resettable=True,
                                   operations={'idcreateOrder': 'C', 'idreadOrder': 'A'})
        goal = 'Создай заявку A и проверь чтение именно созданной заявки.'
        base = fixture()[3]
        base.update(goal_hash=fingerprint(goal), spec_hash=cat.spec_hash, config_hash=fingerprint(cfg.snapshot))
        base['requirements'][0].update(span=[0, len(goal)], interpretation=goal)
        base['slots'] = [
            {'id': 'create', 'operation_id': 'idcreateOrder', 'parameters': {}, 'body': {'name': 'A'}, 'actor': 'anonymous', 'auth_mode': 'anonymous', 'auth_epoch': 0, 'depends_on': []},
            {'id': 'read', 'operation_id': 'idreadOrder', 'parameters': {'order_id': 123}, 'actor': 'anonymous', 'auth_mode': 'anonymous', 'auth_epoch': 0, 'depends_on': ['create']}]
        base['conditions'] = [
            {'id': 'c1', 'requirement_id': 'r1', 'slot': 'create', 'predicate': 'status_in', 'parameters': {'values': [201]}, 'source': 'MODEL_PROPOSED', 'basis': None},
            {'id': 'c2', 'requirement_id': 'r1', 'slot': 'read', 'predicate': 'value_equal', 'parameters': {'left': {'slot': 'read', 'path': '/id'}, 'right': {'slot': 'create', 'path': '/id'}}, 'source': 'MODEL_PROPOSED', 'basis': None}]
        base['answer_fields'][0].update(slot='read', path='/name')
        with self.assertRaisesRegex(ValueError, 'goal_identity_target_required'):
            self.compile((cat, cfg, goal, base), confirm=True)
        base['slots'][1]['parameters']['order_id'] = {'from_slot': 'create', 'path': '/id'}
        self.assertEqual(self.compile((cat, cfg, goal, base), confirm=True).snapshot['coverage'], 'complete')

    def exchange(self, operation, body):
        return {'id': 'e1', 'run_id': 'run', 'operation_id': operation, 'occurrence_id': 's1',
            'request_parameters': {}, 'request': {'method': 'GET', 'url': 'http://127.0.0.1:18082/pet'},
            'request_principal': {'mode': 'anonymous', 'auth_epoch': 0, 'actor': None},
            'outcome': 'received', 'truncated': False,
            'response': {'status': 200, 'content_type': 'application/json', 'json_valid': True, 'json': body}}

    def test_checklist_requires_exact_occurrence_actor_scope_and_all_answers(self):
        case = fixture()
        accepted = self.compile(case)
        api = self.api()
        good = {'exchanges': [self.exchange('readPet', {'name': 'Murka'})]}
        self.assertEqual(api.check_goal(accepted, case[0], good)['answer_fields']['a1'], 'Murka')
        for change in ('slot', 'actor', 'scope', 'answer', 'duplicate', 'truncated'):
            run = deepcopy(good)
            exchange = run['exchanges'][0]
            if change == 'slot': exchange['occurrence_id'] = 'foreign'
            if change == 'actor': exchange['request_principal']['mode'] = 'authenticated'
            if change == 'scope': exchange['request_parameters'] = {'hidden_filter': 1}
            if change == 'answer': exchange['response']['json'] = {}
            if change == 'duplicate': run['exchanges'].append(deepcopy(exchange))
            if change == 'truncated': exchange['truncated'] = True
            self.assertEqual(api.check_goal(accepted, case[0], run)['verdict'], 'UNKNOWN', change)

    def test_full_response_missing_expected_field_is_fail_not_unknown(self):
        case = list(fixture())
        case[3]['conditions'][0].update(predicate='json_value_equals', parameters={'path': '/name', 'value': 'Murka'})
        accepted = self.compile(case, confirm=True)
        result = self.api().check_goal(accepted, case[0], {'exchanges': [self.exchange('readPet', {})]})
        self.assertEqual(result['verdict'], 'FAIL')

    def test_unmapped_extra_mutation_and_unsupported_old_epoch_are_rejected(self):
        case = list(fixture())
        extra = deepcopy(case[3]['slots'][0]); extra['id'] = 'hidden'
        case[3]['slots'].append(extra)
        with self.assertRaisesRegex(ValueError, 'goal_unmapped_slot'): self.compile(case, confirm=True)
        case = list(fixture())
        case[3]['slots'][0].update(auth_mode='old_epoch', actor='current_user')
        with self.assertRaisesRegex(ValueError, 'goal_invalid_old_epoch'): self.compile(case, confirm=True)


if __name__ == '__main__': unittest.main()

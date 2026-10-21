"""S09b: generator→compiler→acceptance→настоящий HTTP→checklist→SQLite."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent.catalog import CatalogLimits
from api_agent.openapi import load_source, parse_document
from api_agent.policy import ProjectConfig
from api_agent.storage import SQLiteHistory
from api_agent.validation import fingerprint
from agent_support import count_test_tokens, reply
from demo_api.server import DemoServer


def pet_proposal(catalog, config, goal):
    return {'version': 1, 'goal_hash': fingerprint(goal), 'spec_hash': catalog.spec_hash,
        'config_hash': fingerprint(config.snapshot),
        'requirements': [{'id': 'r1', 'span': [0, len(goal)], 'interpretation': 'GET питомца, статус 200, показать имя'}],
        'slots': [{'id': 's1', 'operation_id': 'getPet', 'parameters': {}, 'auth_mode': 'anonymous',
                   'actor': 'anonymous', 'auth_epoch': 0, 'depends_on': []}],
        'conditions': [{'id': 'c1', 'requirement_id': 'r1', 'slot': 's1', 'predicate': 'status_in',
                        'parameters': {'values': [200]}, 'source': 'MODEL_PROPOSED', 'basis': None}],
        'answer_fields': [{'id': 'a1', 'requirement_id': 'r1', 'slot': 's1', 'path': '/name'}], 'unresolved': []}


class PrimaryModel:
    def __init__(self, *, ask=None, change=False): self.ask, self.change = ask, change
    def __call__(self, payload):
        obs = [json.loads(m['content']) for m in payload['messages'] if m['role'] == 'tool']
        if self.ask and not any((o.get('data') or {}).get('input_slot') == self.ask for o in obs):
            return reply('ask_user', {'question': 'Уточните значение', 'input_slot': self.ask})
        exchanges = [o['data'] for o in obs if (o.get('data') or {}).get('exchange_id')]
        if not exchanges: return reply('execute_operation', {'operation_id': 'getPet', 'occurrence_id': 's1'})
        if not any((o.get('data') or {}).get('manifest_hash') for o in obs): return reply('check_goal', {})
        return reply('finish_run', {'summary': 'Проверено', 'evidence_ids': [e['exchange_id'] for e in exchanges]})


class GoalApplicationTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'goal.sqlite3'
        self.server = DemoServer(); self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.catalog = load_source(str(ROOT / 'tests/fixtures/demo_openapi.json'))
        self.config = ProjectConfig.create('p', self.server.target, self.catalog, mode='READ_ONLY',
                                         resettable=True, operations={'getPet': 'A'})

    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('api_agent.goals.application'), 'Нет application S09b')
        from api_agent.goals.application import GoalApplication
        return GoalApplication

    def app(self, history, goal, *, proposal=None, human=None, limits=None):
        self.generator_calls = []
        proposed = proposal or pet_proposal(self.catalog, self.config, goal)
        def generator(payload):
            self.generator_calls.append(deepcopy(payload))
            response = deepcopy(proposed)
            response['version'] = json.loads(payload['messages'][-1]['content'])['identity']['version']
            return reply('propose_goal', response)
        return self.api()(self.server.target, self.catalog, self.config, history=history,
            generator=generator, human=human, limits=limits or CatalogLimits(), token_counter=count_test_tokens)

    def test_real_goal_compiles_runs_checks_answers_and_deduplicates_after_restart(self):
        goal = 'Проверь getPet: статус 200.'
        with SQLiteHistory(self.path) as history:
            app = self.app(history, goal)
            run = app.start('p', 's', 'm', goal, PrimaryModel())
            self.assertEqual(run['status'], 'completed')
            self.assertEqual(run['report']['goal_verdict'], 'PASS')
            self.assertEqual(run['report']['goal_checklist']['answer_fields'], {'a1': 'Murka'})
            self.assertEqual(len(self.generator_calls), 1)
            self.assertEqual(run['preparation']['generator_calls'], 1)
            self.assertGreater(run['resources']['tokens_upper_bound'], run['preparation']['resources']['tokens_upper_bound'])
            self.assertTrue(run['admission_manifest']['goal_contract']['manifest_hash'])
        with SQLiteHistory(self.path) as history:
            app = self.app(history, goal)
            duplicate = app.start('p', 's', 'm', goal, lambda _: self.fail('primary replay'))
            self.assertEqual(duplicate['run_id'], run['run_id'])
            self.assertEqual(self.generator_calls, [])
        self.assertEqual(len(self.server.journal), 1)

    def test_natural_goal_requires_confirmation_before_http_and_false_json_never_auto_accepts(self):
        from api_agent.goals.interaction import HumanInteraction
        goal = 'Покажи имя питомца и проверь доступность сведений.'
        for value in ('no', 'yes'):
            with tempfile.TemporaryDirectory() as folder, SQLiteHistory(Path(folder) / 'h.sqlite3') as history:
                human = HumanInteraction(lambda request: {'request_id': request['id'], 'value': value})
                run = self.app(history, goal, human=human).start('p', 's', value, goal, PrimaryModel())
                self.assertEqual(run['status'], 'completed' if value == 'yes' else 'blocked')
        self.assertEqual(len(self.server.journal), 1)

    def test_rejected_generator_never_repeats_on_duplicate_or_performs_http(self):
        goal = 'Проверь getPet: статус 200.'
        proposal = pet_proposal(self.catalog, self.config, goal)
        proposal['conditions'][0]['slot'] = 'foreign'
        with SQLiteHistory(self.path) as history:
            app = self.app(history, goal, proposal=proposal)
            first = app.start('p', 's', 'm', goal, PrimaryModel())
            self.assertEqual(first['status'], 'blocked')
            again = app.start('p', 's', 'm', goal, PrimaryModel())
            self.assertEqual(first, again)
            self.assertEqual(len(self.generator_calls), 1)
        self.assertEqual(self.server.connections, 0)

    def test_change_after_http_uses_new_message_run_and_keeps_old_manifest(self):
        goal = 'Проверь getPet: статус 200.'
        with SQLiteHistory(self.path) as history:
            app = self.app(history, goal)
            first = app.start('p', 's', 'm', goal, PrimaryModel())
            altered = goal + ' Новое требование.'
            with self.assertRaisesRegex(ValueError, 'goal_message_changed'):
                app.start('p', 's', 'm', altered, PrimaryModel())
            second = app.start('p', 's', 'retest', goal, PrimaryModel(), parent_run_id=first['run_id'])
            self.assertEqual(second['parent_run_id'], first['run_id'])
            self.assertEqual(second['admission_manifest']['goal_contract']['manifest']['version'], 2)
            self.assertNotEqual(first['run_id'], second['run_id'])
            self.assertEqual(history.read_run('p', 's', first['run_id'])['admission_manifest'], first['admission_manifest'])

    def test_primary_cannot_change_conditions_or_perform_extra_http_via_check_goal(self):
        goal = 'Проверь getPet: статус 200.'
        with SQLiteHistory(self.path) as history:
            run = self.app(history, goal).start('p', 's', 'm', goal, PrimaryModel())
            self.assertEqual(run['requests'], 1)
            self.assertTrue(any(s.get('action', {}).get('name') == 'check_goal' for s in run['steps']))
            self.assertEqual(len(self.server.journal), 1)

    def test_typed_input_is_asked_before_mutation_and_cannot_change_after_http(self):
        from api_agent.goals.interaction import HumanInteraction
        from test_catalog_execution import catalog_variant
        self.catalog = catalog_variant('new')
        self.config = ProjectConfig.create('p', self.server.target, self.catalog, mode='TEST', resettable=True,
                                         operations={'newcreateOrder': 'C'})
        goal = 'Создай заявку с именем, которое я выберу при уточнении. Покажи имя.'
        proposal = pet_proposal(self.catalog, self.config, goal)
        proposal['slots'][0].update(operation_id='newcreateOrder', body={'name': {'input': 'chosen'}})
        proposal['conditions'][0].update(predicate='json_value_equals', parameters={'path': '/name', 'value': {'input': 'chosen'}})
        proposal['input_slots'] = [{'id': 'chosen', 'type': 'string'}]
        human = HumanInteraction(lambda request: {'request_id': request['id'], 'value': 'yes' if request['kind'] == 'acceptance' else 'A'})
        def primary(payload):
            obs = [json.loads(m['content']) for m in payload['messages'] if m['role'] == 'tool']
            exchanges = [o['data'] for o in obs if (o.get('data') or {}).get('exchange_id')]
            if not obs: return reply('ask_user', {'question': 'Какое имя?', 'input_slot': 'chosen'})
            if not exchanges: return reply('execute_operation', {'operation_id': 'newcreateOrder', 'occurrence_id': 's1', 'body': {'name': 'A'}})
            return reply('finish_run', {'summary': 'Создано', 'evidence_ids': [e['exchange_id'] for e in exchanges]})
        with SQLiteHistory(self.path) as history:
            run = self.app(history, goal, proposal=proposal, human=human).start('p', 's', 'm', goal, primary)
            self.assertEqual(run['status'], 'completed', run.get('stop_reason'))
            self.assertEqual(run['report']['goal_verdict'], 'PASS')
            self.assertEqual(run['goal_inputs'], {'chosen': 'A'})
        self.assertEqual(len(self.server.journal), 1)

    def test_missing_calculation_expectation_is_asked_without_generator_replay(self):
        from api_agent.goals.interaction import HumanInteraction
        goal = 'Проверь правильность расчёта сервиса.'
        proposal = pet_proposal(self.catalog, self.config, goal)
        proposal['unresolved'] = ['Какой результат расчёта вы ожидаете?']
        requests = []
        def reader(request):
            requests.append(request)
            return {'request_id': request['id'], 'value': '41.01'}
        with SQLiteHistory(self.path) as history:
            app = self.app(history, goal, proposal=proposal, human=HumanInteraction(reader))
            run = app.start('p', 's', 'm', goal, PrimaryModel())
            self.assertEqual(run['status'], 'blocked')
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]['kind'], 'ask')
            self.assertEqual(len(self.generator_calls), 1)
            self.assertEqual(app.start('p', 's', 'm', goal, PrimaryModel()), run)
        self.assertEqual(self.server.connections, 0)

    def test_generator_prefix_stable_across_goals_and_budget_preflight_stops_before_model(self):
        from api_agent.goals.application import generator_payload
        first = generator_payload(self.catalog, self.config, 'Покажи имя')
        second = generator_payload(self.catalog, self.config, 'Проверь доступность')
        self.assertEqual(first['messages'][0], second['messages'][0])
        self.assertEqual(first['tools'], second['tools'])
        self.assertNotEqual(first['messages'][1], second['messages'][1])
        schema = first['tools'][0]['function']['parameters']
        self.assertIn('input_slots', schema['properties'])
        self.assertNotIn('input_slots', schema['required'])
        goal = 'Проверь getPet: статус 200.'
        with SQLiteHistory(self.path) as history:
            app = self.app(history, goal, limits=CatalogLimits(max_tokens_total=100))
            run = app.start('p', 's', 'm', goal, PrimaryModel())
            self.assertEqual(run['status'], 'blocked')
            self.assertEqual(self.generator_calls, [])
        self.assertEqual(self.server.connections, 0)

    def test_bounded_metadata_discovery_precedes_single_generator_without_api_http(self):
        from api_agent.goals.application import GoalApplication
        from api_agent.openapi import parse_document
        doc = json.loads((ROOT / 'tests/fixtures/demo_openapi.json').read_text())
        for index in range(18):
            operation = deepcopy(doc['paths']['/pets/1'])
            operation['get']['operationId'] = 'extra' + str(index)
            doc['paths']['/extra/' + str(index)] = operation
        self.catalog = parse_document(doc)
        self.config = ProjectConfig.create('p', self.server.target, self.catalog, mode='READ_ONLY', resettable=True,
                                         operations={name: 'A' for name in self.catalog.operations})
        goal = 'Проверь getPet: статус 200.'
        calls = []
        def generator(payload):
            self.assertEqual(self.server.connections, 0)
            names = {t['function']['name'] for t in payload['tools']}
            calls.append(names)
            if 'inspect_operation' in names:
                observed = any(m['role'] == 'tool' for m in payload['messages'])
                return reply('metadata_ready', {'operation_ids': ['getPet']}) if observed else reply('inspect_operation', {'operation_id': 'getPet'})
            return reply('propose_goal', pet_proposal(self.catalog, self.config, goal))
        with SQLiteHistory(self.path) as history:
            app = GoalApplication(self.server.target, self.catalog, self.config, history=history, generator=generator,
                                  token_counter=count_test_tokens)
            run = app.start('p', 's', 'm', goal, PrimaryModel())
            self.assertEqual(run['status'], 'completed', run['stop_reason'])
            self.assertEqual(run['preparation']['metadata_calls'], 2)
            self.assertEqual(sum('propose_goal' in names for names in calls), 1)

    def test_secret_literal_in_metadata_never_reaches_generator(self):
        from api_agent.goals.application import GoalApplication
        goal = 'Проверь getPet: статус 200.'
        secret = 'Murka'
        # Секрет в публичной декларации несовместим с закреплённой identity.
        doc = json.loads((ROOT / 'tests/fixtures/demo_openapi.json').read_text())
        doc['paths']['/pets/1']['get']['responses']['200']['content']['application/json']['schema']['properties']['name']['enum'] = [secret]
        self.catalog = parse_document(doc)
        self.config = ProjectConfig.create('p', self.server.target, self.catalog, mode='READ_ONLY', resettable=True, operations={'getPet': 'A'})
        from api_agent.goals.interaction import HumanInteraction
        human = HumanInteraction(lambda q: {'request_id': q['id'], 'value': secret})
        human.private_input('test_password')
        generator_calls = []
        def generator(payload):
            generator_calls.append(payload)
            return reply('propose_goal', pet_proposal(self.catalog, self.config, goal))
        with SQLiteHistory(self.path) as history:
            app = GoalApplication(self.server.target, self.catalog, self.config, history=history,
                generator=generator, token_counter=count_test_tokens, human=human)
            run = app.start('p', 's', 'm', goal, PrimaryModel())
            self.assertEqual(run['status'], 'blocked')
            self.assertEqual(generator_calls, [])
            self.assertEqual(self.server.connections, 0)

    def test_durable_preparation_can_be_claimed_only_once_across_connections(self):
        payload = {'goal_hash': fingerprint('goal'), 'status': 'preparing', 'generator_calls': 0}
        with SQLiteHistory(self.path) as first, SQLiteHistory(self.path) as second:
            self.assertTrue(hasattr(first, 'begin_goal_preparation'), 'Нет атомарного приёма подготовки')
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda store: store.begin_goal_preparation('p', 's', 'm', payload), [first, second]))
            self.assertEqual(results.count(None), 1)
            self.assertEqual([r for r in results if r is not None], [payload])

    def test_expected_input_must_be_fixed_before_http_and_cannot_be_replaced_after(self):
        from api_agent.goals.interaction import HumanInteraction
        goal = 'Проверь, что имя равно значению, которое я укажу.'
        proposal = pet_proposal(self.catalog, self.config, goal)
        proposal['input_slots'] = [{'id': 'expected_name', 'type': 'string'}]
        proposal['conditions'][0].update(predicate='json_value_equals', parameters={'path': '/name', 'value': {'input': 'expected_name'}})
        with SQLiteHistory(self.path) as history:
            human = HumanInteraction(lambda q: {'request_id': q['id'], 'value': 'yes' if q['kind'] == 'acceptance' else 'Murka'})
            run = self.app(history, goal, proposal=proposal, human=human).start('p', 's', 'unfixed', goal, PrimaryModel())
            self.assertEqual(run['requests'], 0)
            self.assertEqual(self.server.connections, 0)
        def primary(payload):
            obs = [json.loads(m['content']) for m in payload['messages'] if m['role'] == 'tool']
            has_input = any((o.get('data') or {}).get('input_slot') for o in obs)
            has_http = any((o.get('data') or {}).get('exchange_id') for o in obs)
            if not has_input or has_http: return reply('ask_user', {'question': 'Ожидаемое имя?', 'input_slot': 'expected_name'})
            return reply('execute_operation', {'operation_id': 'getPet', 'occurrence_id': 's1'})
        with SQLiteHistory(self.path) as history:
            human = HumanInteraction(lambda q: {'request_id': q['id'], 'value': 'yes' if q['kind'] == 'acceptance' else 'Murka'})
            run = self.app(history, goal, proposal=proposal, human=human).start('p', 's', 'locked', goal, primary)
            self.assertEqual(run['requests'], 1)
            self.assertEqual(run['goal_inputs'], {'expected_name': 'Murka'})
            self.assertIn(run['stop_reason'], {'input_slot_locked', 'malformed_limit'})

    def test_compiled_create_read_keeps_s09a_fact_validators(self):
        from api_agent.goals.interaction import HumanInteraction
        from test_catalog_execution import catalog_variant, GenericModel
        self.catalog = catalog_variant('fresh')
        self.config = ProjectConfig.create('p', self.server.target, self.catalog, mode='TEST', resettable=True,
                                         operations={'freshcreateOrder': 'C', 'freshreadOrder': 'A'})
        goal = 'Создай заявку A и прочитай именно её. Покажи имя.'
        proposal = pet_proposal(self.catalog, self.config, goal)
        proposal['slots'] = [
            {'id': 'create', 'operation_id': 'freshcreateOrder', 'parameters': {}, 'body': {'name': 'A'}, 'actor': 'anonymous', 'auth_mode': 'anonymous', 'auth_epoch': 0, 'depends_on': []},
            {'id': 'read', 'operation_id': 'freshreadOrder', 'parameters': {'order_id': {'from_slot': 'create', 'path': '/id'}}, 'actor': 'anonymous', 'auth_mode': 'anonymous', 'auth_epoch': 0, 'depends_on': ['create']}]
        proposal['conditions'] = [
            {'id': 'c1', 'requirement_id': 'r1', 'slot': 'create', 'predicate': 'status_in', 'parameters': {'values': [201]}, 'source': 'MODEL_PROPOSED', 'basis': None},
            {'id': 'c2', 'requirement_id': 'r1', 'slot': 'read', 'predicate': 'value_equal', 'parameters': {'left': {'slot': 'read', 'path': '/id'}, 'right': {'slot': 'create', 'path': '/id'}}, 'source': 'MODEL_PROPOSED', 'basis': None}]
        proposal['answer_fields'][0].update(slot='read', path='/name')
        original = GenericModel('freshcreateOrder', 'freshreadOrder')
        def model(payload):
            response = original(payload)
            call = response['choices'][0]['message']['tool_calls'][0]
            args = json.loads(call['function']['arguments'])
            if call['function']['name'] == 'execute_operation':
                args['occurrence_id'] = 'create' if args['operation_id'] == 'freshcreateOrder' else 'read'
                call['function']['arguments'] = json.dumps(args)
            return response
        with SQLiteHistory(self.path) as history:
            human = HumanInteraction(lambda q: {'request_id': q['id'], 'value': 'yes'})
            run = self.app(history, goal, proposal=proposal, human=human).start('p', 's', 'm', goal, model)
            self.assertEqual(run['report']['goal_verdict'], 'PASS', run['stop_reason'])
            self.assertEqual(run['requests'], 2)
            facts = history.read_facts('p', catalog=self.catalog)
            self.assertTrue(any(f['type'] == 'create_read_link' for f in facts))


if __name__ == '__main__': unittest.main()

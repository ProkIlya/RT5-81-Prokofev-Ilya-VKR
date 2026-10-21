"""S09b2: регистрация/private body и явная проверка старой auth epoch."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig
from api_agent.storage import SQLiteHistory
from api_agent.goals.interaction import HumanInteraction
from api_agent.validation import fingerprint
from agent_support import reply, count_test_tokens
from demo_api.server import DemoServer
import test_runtime_auth as auth_support


def auth_catalog():
    doc = json.loads((ROOT / 'tests/fixtures/auth_openapi.json').read_text())
    body = deepcopy(doc['paths']['/users/login']['post']['requestBody'])
    response = lambda schema: {'description': 'ok', 'content': {'application/json': {'schema': schema}}}
    error = response({'type': 'object', 'properties': {'error': {'type': 'string'}}, 'required': ['error']})
    doc['paths']['/users/register'] = {'post': {'operationId': 'registerUser', 'requestBody': body,
        'responses': {'201': response({'type': 'object', 'properties': {'id': {'type': 'integer'}, 'login': {'type': 'string'}}, 'required': ['id', 'login']}), '409': error}}}
    doc['paths']['/users/logout'] = {'post': {'operationId': 'logoutUser', 'security': [{'bearerAuth': []}],
        'responses': {'200': response({'type': 'object', 'properties': {'logged_out': {'type': 'boolean'}}, 'required': ['logged_out']})}}}
    doc['paths']['/users/profile']['get']['responses']['401'] = error
    return parse_document(doc)


def auth_proposal(cat, cfg, goal, *, occupied=False):
    operations = [('registerUser', 'anonymous', 0, 201), ('loginUser', 'anonymous', 0, 200),
                  ('logoutUser', 'authenticated', 1, 200), ('readProfile', 'old_epoch', 1, 401),
                  ('loginUser', 'anonymous', 2, 200), ('readProfile', 'authenticated', 3, 200)]
    slots, conditions = [], []
    for i, (operation, mode, epoch, status) in enumerate(operations):
        slot = {'id': 's' + str(i), 'operation_id': operation, 'parameters': {}, 'auth_mode': mode,
                'actor': 'current_user' if mode in {'authenticated', 'old_epoch'} else 'anonymous',
                'auth_epoch': epoch, 'depends_on': ['s' + str(i-1)] if i else []}
        if i == 0: slot['body'] = {'login': 'fixture' if occupied else 'new-user', 'password': {'private': 'test_password'}}
        slots.append(slot)
        conditions.append({'id': 'c' + str(i), 'requirement_id': 'r1', 'slot': slot['id'],
            'predicate': 'status_in', 'parameters': {'values': [status]}, 'source': 'MODEL_PROPOSED', 'basis': None})
    return {'version': 1, 'goal_hash': fingerprint(goal), 'spec_hash': cat.spec_hash, 'config_hash': fingerprint(cfg.snapshot),
        'requirements': [{'id': 'r1', 'span': [0, len(goal)], 'interpretation': 'register/login/logout/old token 401/relogin/profile'}],
        'slots': slots, 'conditions': conditions, 'answer_fields': [{'id': 'a1', 'requirement_id': 'r1', 'slot': 's5', 'path': '/name'}], 'unresolved': []}


class AuthPlanModel:
    def __init__(self, proposal): self.proposal, self.payloads = proposal, []
    def __call__(self, payload):
        self.payloads.append(deepcopy(payload))
        observations = [json.loads(m['content']) for m in payload['messages'] if m['role'] == 'tool']
        exchanges = [o['data'] for o in observations if (o.get('data') or {}).get('exchange_id')]
        if len(exchanges) == len(self.proposal['slots']):
            return reply('finish_run', {'summary': 'Проверено', 'evidence_ids': [e['exchange_id'] for e in exchanges]})
        slot = self.proposal['slots'][len(exchanges)]
        args = {'operation_id': slot['operation_id'], 'occurrence_id': slot['id']}
        if slot['operation_id'] == 'registerUser': args['body'] = {'login': slot['body']['login']}
        if slot['auth_mode'] == 'authenticated':
            args['credential_ref'] = [v['ref'] for e in exchanges for v in e.get('runtime_values', []) if v.get('secret')][-1]
        return reply('execute_operation', args)


class GoalAuthTests(unittest.TestCase):
    def run_auth(self, *, occupied=False):
        from api_agent.goals.application import GoalApplication
        folder = tempfile.TemporaryDirectory(); self.addCleanup(folder.cleanup)
        server = DemoServer(); server.__enter__(); self.addCleanup(server.__exit__, None, None, None)
        catalog = auth_catalog()
        config = ProjectConfig.create('p', server.target, catalog, mode='TEST', resettable=True,
            operations={'registerUser': 'C', 'loginUser': 'C', 'logoutUser': 'C', 'readProfile': 'A'})
        goal = 'Зарегистрируй пользователя, войди, выйди, проверь отказ старого токена и войди заново.'
        proposal = auth_proposal(catalog, config, goal, occupied=occupied)
        secret = 'private-S09b-password-canary'
        human = HumanInteraction(lambda request: {'request_id': request['id'], 'value': secret if request['kind'] == 'private_input' else 'yes'})
        human.private_input('test_password')
        model = AuthPlanModel(proposal)
        path = Path(folder.name) / 'auth.sqlite3'
        with SQLiteHistory(path) as history:
            app = GoalApplication(server.target, catalog, config, history=history, human=human,
                generator=lambda _: reply('propose_goal', proposal), token_counter=count_test_tokens,
                mappings={'auth': {'login_operation': 'loginUser', 'token_path': '/token',
                    'body': {'login': 'new-user', 'password': secret}, 'public_body_fields': ['login'], 'logout_operations': ['logoutUser']},
                    'extraction': {'loginUser': {'/token': {'type': 'string', 'secret': True}}},
                    'private_bodies': {'registerUser': {'password': secret}}})
            run = app.start('p', 's', 'm', goal, model)
            dump = '\n'.join(history.db.iterdump())
        self.assertNotIn(secret, json.dumps([run, model.payloads, human.public_events(), server.journal]))
        self.assertNotIn(secret, dump)
        return server, run

    def test_registration_logout_old_token_401_and_relogin_use_real_http(self):
        server, run = self.run_auth()
        self.assertEqual(run['status'], 'completed', run.get('stop_reason'))
        self.assertEqual(run['report']['goal_verdict'], 'PASS')
        self.assertEqual([e['response']['status'] for e in run['exchanges']], [201, 200, 200, 401, 200, 200])
        self.assertFalse(server.journal[3]['auth_matched'])
        self.assertTrue(server.journal[5]['auth_matched'])

    def test_occupied_login_blocks_dependent_mutations_without_blind_retry(self):
        server, run = self.run_auth(occupied=True)
        self.assertEqual(run['report']['goal_verdict'], 'FAIL')
        self.assertEqual(len(server.journal), 1)
        self.assertEqual(run['exchanges'][0]['response']['status'], 409)


if __name__ == '__main__': unittest.main()

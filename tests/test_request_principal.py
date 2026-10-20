"""S08Rb: транспортный principal задаётся приложением, не наличием security."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent.catalog import CatalogWorkflow, CatalogSession, CatalogLimits
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig
from demo_api.server import DemoServer
from test_catalog_execution import status_condition


def principal_session(server, *, security=None, request_auth=None, negative=False):
    doc = json.loads((ROOT / 'tests/fixtures/auth_openapi.json').read_text(encoding='utf-8'))
    doc['paths']['/users/profile']['get']['security'] = [] if security is None else security
    catalog = parse_document(doc)
    config = ProjectConfig.create('p', server.target, catalog, mode='TEST', resettable=True,
        operations={'loginUser': 'C', 'readProfile': 'A'})
    condition = status_condition(401 if negative else 200)
    if negative:
        condition['intent'] = 'negative'
    wf = CatalogWorkflow(catalog, config, goal='Проверь профиль', conditions={'readProfile': [condition]},
        extraction={'loginUser': {'/token': {'type': 'string', 'secret': True}}},
        auth={'login_operation': 'loginUser', 'token_path': '/token',
              'body': {'login': 'fixture', 'password': 'fixture-password'}},
        request_auth=request_auth)
    run = {'run_id': 'run', 'project_id': 'p', 'expectations': []}
    return wf, CatalogSession(server.target, catalog, wf, run, CatalogLimits())


def login(session):
    exchange = session.execute({'operation_id': 'loginUser'})
    # Обычный путь приложения: extraction в observation после validator.
    session.state.begin_auth('fixture')
    return session.state.remember(exchange, '/token')['ref']


class RequestPrincipalTests(unittest.TestCase):
    def test_auth_mapping_cannot_change_after_run_acceptance(self):
        from api_agent.agent import StartAgentRun
        from agent_support import count_test_tokens, reply
        from test_snapshot_redaction import auth_workflow
        with DemoServer() as server:
            cat, wf = auth_workflow(server)
            def model(payload):
                wf.auth['body']['password'] = 'changed-after-acceptance'
                return reply('execute_operation', {'operation_id': 'loginUser'})
            run = StartAgentRun(server.target, cat, workflow=wf, token_counter=count_test_tokens).start(
                'p', 's', 'm', wf.goal, model)
            self.assertEqual(run['stop_reason'], 'expectation_changed')
            self.assertEqual(server.connections, 0)

    def test_auth_revocation_during_approval_prevents_prepared_http(self):
        from api_agent.policy import ProjectConfig
        with DemoServer() as server:
            wf, session = principal_session(server, request_auth={'readProfile': 'authenticated'})
            ref = login(session)
            # Риск C заставляет запросить approval даже для GET.
            session.pinned['mode'] = 'SAFE'
            session.pinned['operations']['readProfile']['risk'] = 'C'
            current = ProjectConfig.create('p', server.target, session.catalog, mode='SAFE', resettable=True,
                operations={'loginUser': 'C', 'readProfile': 'C'})
            wf.current_config = lambda: current
            def approval(proposal):
                session.state.invalidate_auth()
                return {'action_hash': proposal['action_hash'], 'config_version': proposal['config_version'], 'approved': True}
            wf.approval = approval
            before = server.connections
            with self.assertRaisesRegex(ValueError, 'stale_auth_epoch'):
                session.execute({'operation_id': 'readProfile', 'credential_ref': ref})
            self.assertEqual(server.connections, before)

    def test_explicit_authenticated_request_without_schema_security_uses_bearer(self):
        for security in ([], [{}, {'bearerAuth': []}]):
            with self.subTest(security=security), DemoServer() as server:
                wf, session = principal_session(server, security=security,
                    request_auth={'readProfile': 'authenticated'})
                ref = login(session)
                exchange = session.execute({'operation_id': 'readProfile', 'credential_ref': ref})
                self.assertEqual(exchange['response']['status'], 200)
                self.assertTrue(server.journal[-1]['auth_matched'])
                self.assertEqual(exchange['request_principal']['auth_epoch'], 1)
                self.assertEqual(exchange['request_principal']['mode'], 'authenticated')

    def test_stale_foreign_and_anonymous_autoauth_rejected_before_network(self):
        with DemoServer() as server:
            _, session = principal_session(server, request_auth={'readProfile': 'authenticated'})
            ref = login(session)
            session.state.invalidate_auth()
            before = server.connections
            for candidate in (ref, 'value-foreign'):
                with self.assertRaisesRegex(ValueError, 'auth_reference_denied'):
                    session.execute({'operation_id': 'readProfile', 'credential_ref': candidate})
            self.assertEqual(server.connections, before)
        with DemoServer() as server:
            _, session = principal_session(server,
                request_auth={'readProfile': 'anonymous_negative'}, negative=True)
            ref = login(session)
            before = server.connections
            with self.assertRaisesRegex(ValueError, 'unexpected_auth_reference'):
                session.execute({'operation_id': 'readProfile', 'credential_ref': ref})
            self.assertEqual(server.connections, before)
            exchange = session.execute({'operation_id': 'readProfile'})
            self.assertEqual(exchange['response']['status'], 401)
            self.assertFalse(server.journal[-1]['auth_matched'])


if __name__ == '__main__':
    unittest.main()

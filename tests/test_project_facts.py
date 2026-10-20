"""S09a: содержательные основания, SQLite и freshness без модельного oracle."""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests'), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.openapi import load_source, parse_document
from api_agent.storage import SQLiteHistory
from agent_support import ObserverModel, count_test_tokens
from demo_api.server import DemoServer
import test_catalog_execution as order_support


class ProjectFactTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.path = Path(self.folder.name) / 'facts.sqlite3'
        self.server = DemoServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.catalog = load_source(str(ROOT / 'tests/fixtures/demo_openapi.json'))

    def start(self, history, *, project='p', session='A', message='m', conditions=None):
        return StartAgentRun(self.server.target, self.catalog, history_store=history,
            token_counter=count_test_tokens).start(project, session, message, 'Check pet',
                ObserverModel(), conditions=conditions)

    def fact_api(self, history):
        self.assertTrue(hasattr(history, 'propose_fact'), 'S09a: нет содержательной проверки фактов')

    def observation(self, run):
        return {'type': 'response_observation', 'source': 'OBSERVED', 'operation_id': 'getPet',
                'verify': {'exchange_id': run['exchanges'][0]['id'],
                           'expectation_id': run['expectations'][0]['id']}}

    def declaration(self):
        return {'type': 'declared_contract', 'source': 'DECLARED', 'operation_id': 'getPet',
                'verify': {'pointer': '/responses/200/content/application~1json/schema/properties/id/type'}}

    def test_verified_facts_survive_restart_and_retrieve_in_other_session_without_http(self):
        with SQLiteHistory(self.path) as history:
            self.fact_api(history)
            run = self.start(history)
            facts = history.read_facts('p', catalog=self.catalog)
            self.assertEqual({f['type'] for f in facts}, {'declared_contract', 'response_observation'})
            observation = history.propose_fact('p', 'A', run['run_id'], self.observation(run))
            self.assertEqual(observation['status'], 'VERIFIED')
            self.assertEqual(observation['value'], 200)
            declared = history.propose_fact('p', 'A', run['run_id'], self.declaration())
            self.assertEqual(declared['value'], 'integer')
            self.assertEqual(declared['source'], 'DECLARED')
            self.assertEqual(declared['basis']['spec_hash'], self.catalog.spec_hash)
            self.start(history, session='B', message='other-session')
        before = len(self.server.journal)
        with SQLiteHistory(self.path, read_only=True) as history:
            facts = history.read_facts('p', catalog=self.catalog, operation_ids=['getPet'])
            self.assertTrue(any(f['id'] == observation['id'] and f['origin']['session_id'] == 'A' for f in facts))
            self.assertEqual(history.read_facts('foreign', catalog=self.catalog), [])
            self.assertEqual(history.read_facts('p', catalog=self.catalog, operation_ids=['unknown']), [])
        self.assertEqual(len(self.server.journal), before)

    def test_existing_wrong_evidence_and_unknown_exchange_do_not_verify(self):
        with SQLiteHistory(self.path) as history:
            self.fact_api(history)
            run = self.start(history)
            descriptor = self.observation(run)
            descriptor['verify']['exchange_id'] = 'invented'
            with self.assertRaisesRegex(ValueError, 'fact_'):
                history.propose_fact('p', 'A', run['run_id'], descriptor)
            descriptor = self.observation(run)
            descriptor['verify']['expectation_id'] = 'invented'
            with self.assertRaisesRegex(ValueError, 'fact_'):
                history.propose_fact('p', 'A', run['run_id'], descriptor)

    def test_failed_predicate_cannot_be_promoted_by_cached_validation_pass(self):
        conditions = [{'source': 'USER_DEFINED', 'predicate': 'json_value_equals',
            'parameters': {'path': '/name', 'value': 'A different pet'}, 'intent': 'positive',
            'basis': {'message_id': 'm'}, 'preconditions': 'verified'}]
        with SQLiteHistory(self.path) as history:
            self.fact_api(history)
            run = self.start(history, conditions=conditions)
            self.assertTrue(run['exchanges'])
            with self.assertRaisesRegex(ValueError, 'fact_predicate_not_verified'):
                history.propose_fact('p', 'A', run['run_id'], self.observation(run))
            self.assertFalse(any(f['type'] == 'response_observation' for f in history.read_facts('p', catalog=self.catalog)))

    def test_foreign_project_and_session_cannot_supply_evidence(self):
        with SQLiteHistory(self.path) as history:
            self.fact_api(history)
            run = self.start(history)
            for project, session in [('foreign', 'A'), ('p', 'foreign')]:
                with self.assertRaises(ValueError):
                    history.propose_fact(project, session, run['run_id'], self.observation(run))

    def test_inferred_or_user_provided_origin_never_becomes_verified(self):
        with SQLiteHistory(self.path) as history:
            self.fact_api(history)
            run = self.start(history)
            for source in ('INFERRED', 'USER_PROVIDED'):
                descriptor = self.observation(run)
                descriptor['source'] = source
                candidate = history.propose_fact('p', 'A', run['run_id'], descriptor)
                self.assertEqual(candidate['status'], 'CANDIDATE')
                self.assertEqual(candidate['source'], source)
                self.assertNotIn(candidate['id'], [f['id'] for f in history.read_facts('p', catalog=self.catalog)])

    def test_expiry_and_reset_exclude_observations_without_rewriting_history(self):
        with SQLiteHistory(self.path) as history:
            self.fact_api(history)
            run = self.start(history)
            history.configure_fact_policy('p', response_observation=30, create_read_link=60)
            fact = history.propose_fact('p', 'A', run['run_id'], self.observation(run))
            self.assertEqual(fact['expires_at'], fact['last_verified_at'] + 30)
            before = history.read_run('p', 'A', run['run_id'])
            fresh = history.read_facts('p', catalog=self.catalog, now=fact['expires_at'] - 0.001)
            self.assertTrue(any(f['id'] == fact['id'] for f in fresh))
            expired = history.read_facts('p', catalog=self.catalog, now=fact['expires_at'])
            self.assertFalse(any(f['type'] == 'response_observation' for f in expired))
            historical = history.read_facts('p', catalog=self.catalog, now=fact['expires_at'], include_inactive=True)
            self.assertTrue(any(f['freshness'] == 'expired' and f['status'] == 'STALE' for f in historical))
            self.server.reset()
            self.assertEqual(history.advance_fact_epoch('p'), 1)
            self.assertFalse(any(f['type'] == 'response_observation' for f in history.read_facts('p', catalog=self.catalog)))
            with self.assertRaisesRegex(ValueError, 'fact_stale_evidence'):
                history.propose_fact('p', 'A', run['run_id'], self.observation(run))
            self.assertEqual(history.read_run('p', 'A', run['run_id']), before)

    def test_reproposal_does_not_renew_evidence_time_and_invalid_policy_is_rejected(self):
        with SQLiteHistory(self.path) as history:
            self.fact_api(history)
            run = self.start(history)
            first = history.propose_fact('p', 'A', run['run_id'], self.observation(run))
            time.sleep(0.01)
            second = history.propose_fact('p', 'A', run['run_id'], self.observation(run))
            self.assertEqual(first, second)
            for ttl in (0, -1, True, float('nan')):
                with self.assertRaises(ValueError):
                    history.configure_fact_policy('p', response_observation=ttl, create_read_link=60)

    def test_changed_operation_is_stale_but_unrelated_spec_change_is_not(self):
        with SQLiteHistory(self.path) as history:
            self.fact_api(history)
            run = self.start(history)
            fact = history.propose_fact('p', 'A', run['run_id'], self.declaration())
            doc = json.loads((ROOT / 'tests/fixtures/demo_openapi.json').read_text())
            doc['info']['version'] = 'new metadata'
            same = parse_document(doc)
            self.assertTrue(any(f['id'] == fact['id'] for f in history.read_facts('p', catalog=same)))
            doc['paths']['/pets/1']['get']['responses']['200']['content']['application/json']['schema']['properties']['id']['type'] = 'string'
            changed = parse_document(doc)
            self.assertEqual(history.read_facts('p', catalog=changed), [])

    def test_declaration_pointer_and_descriptor_cannot_be_forged(self):
        with SQLiteHistory(self.path) as history:
            self.fact_api(history)
            run = self.start(history)
            for pointer in ('/missing', '/responses/200/~2bad'):
                descriptor = self.declaration()
                descriptor['verify']['pointer'] = pointer
                with self.assertRaisesRegex(ValueError, 'fact_'):
                    history.propose_fact('p', 'A', run['run_id'], descriptor)
            descriptor = self.observation(run)
            descriptor['statement'] = 'requires_auth universally; private-password'
            with self.assertRaisesRegex(ValueError, 'fact_'):
                history.propose_fact('p', 'A', run['run_id'], descriptor)

    def order_run(self, history, mode='good'):
        self.server.mode = mode
        catalog, _, workflow, create, read = order_support.GenericExecutionTests().workflow(self.server)
        run = StartAgentRun(self.server.target, catalog, workflow=workflow, history_store=history,
            token_counter=count_test_tokens).start('p', 'A', 'order', workflow.goal, order_support.GenericModel(create, read))
        return catalog, run

    def link(self, run):
        create, read = run['exchanges'][:2]
        expectation = next(e for e in run['expectations'] if e['operation_id'] == read['operation_id'])
        return {'type': 'create_read_link', 'source': 'OBSERVED', 'operation_id': read['operation_id'],
            'verify': {'create_exchange_id': create['id'], 'read_exchange_id': read['id'],
                'create_id_path': '/id', 'read_id_path': '/id', 'request_parameter': 'order_id',
                'expectation_id': expectation['id']}}

    def test_create_read_link_checks_actual_http_and_does_not_store_runtime_id_as_knowledge(self):
        with SQLiteHistory(self.path) as history:
            self.fact_api(history)
            catalog, run = self.order_run(history)
            link = history.propose_fact('p', 'A', run['run_id'], self.link(run))
            self.assertEqual(link['status'], 'VERIFIED')
            self.assertEqual(link['dependencies'], {name: catalog.operation_hashes[name] for name in ('v1createOrder', 'v1readOrder')})
            self.assertEqual(link['value']['request_parameter'], 'order_id')
            self.assertNotIn(str(self.server.journal[0]['created_id']), json.dumps(link))
            self.assertTrue(any(f['type'] == 'create_read_link' for f in history.read_facts('p', catalog=catalog)))

    def test_wrong_response_id_does_not_verify_create_read_link(self):
        with SQLiteHistory(self.path) as history:
            self.fact_api(history)
            catalog, run = self.order_run(history, 'state_id_mismatch')
            with self.assertRaisesRegex(ValueError, 'fact_identity_mismatch'):
                history.propose_fact('p', 'A', run['run_id'], self.link(run))
            self.assertFalse(any(f['type'] == 'create_read_link' for f in history.read_facts('p', catalog=catalog)))

    def test_read_only_legacy_schema_never_migrates(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript((ROOT / 'src/api_agent/storage/schema.sql').read_text())
            db.execute('PRAGMA user_version=1')
        with SQLiteHistory(self.path, read_only=True) as history:
            self.fact_api(history)
            self.assertEqual(history.messages('p', 'A'), [])
            self.assertEqual(history.read_facts('p', catalog=self.catalog), [])
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
        with SQLiteHistory(self.path) as history:
            self.start(history)
            self.assertTrue(history.read_facts('p', catalog=self.catalog))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)

    def name_condition(self, message, name):
        return [{'source': 'USER_DEFINED', 'predicate': 'json_value_equals',
            'parameters': {'path': '/name', 'value': name}, 'intent': 'positive',
            'basis': {'message_id': message}, 'preconditions': 'verified'}]

    def test_conflicting_observations_are_not_resolved_by_newest_timestamp(self):
        with SQLiteHistory(self.path) as history:
            a = self.start(history, conditions=self.name_condition('m', 'Murka'))
            first = history.propose_fact('p', 'A', a['run_id'], self.observation(a))
            self.server.pet['name'] = 'Barsik'
            b = self.start(history, session='B', message='m2', conditions=self.name_condition('m2', 'Barsik'))
            second = history.propose_fact('p', 'B', b['run_id'], self.observation(b))
            active = history.read_facts('p', catalog=self.catalog)
            self.assertFalse(any(f['id'] in {first['id'], second['id']} for f in active))
            all_facts = history.read_facts('p', catalog=self.catalog, include_inactive=True)
            self.assertEqual([f['freshness'] for f in all_facts if f['type'] == 'response_observation'], ['conflict', 'conflict'])

    def test_redacted_value_and_incomplete_response_never_verify(self):
        with SQLiteHistory(self.path) as history:
            self.server.pet['name'] = '[REDACTED]'
            run = self.start(history, conditions=self.name_condition('m', '[REDACTED]'))
            with self.assertRaisesRegex(ValueError, 'fact_'):
                history.propose_fact('p', 'A', run['run_id'], self.observation(run))
            self.server.mode = 'oversize'
            limited = self.start(history, message='limited')
            with self.assertRaisesRegex(ValueError, 'fact_incomplete_evidence'):
                history.propose_fact('p', 'A', limited['run_id'], self.observation(limited))

    def test_fact_payload_cannot_claim_foreign_origin_or_unchecked_value(self):
        with SQLiteHistory(self.path) as history:
            run = self.start(history)
            original = history.propose_fact('p', 'A', run['run_id'], self.observation(run))
            for field in ('origin', 'value'):
                changed = deepcopy(original)
                if field == 'origin':
                    changed['origin']['project_id'] = 'foreign'
                else:
                    changed['value'] = 401
                history.db.execute('UPDATE project_facts SET payload=? WHERE fact_id=?', (json.dumps(changed), original['id']))
                with self.assertRaisesRegex(ValueError, 'fact_storage_binding_mismatch'):
                    history.read_facts('p', catalog=self.catalog)
                history.db.execute('UPDATE project_facts SET payload=? WHERE fact_id=?', (json.dumps(original), original['id']))

    def test_explicit_invalidation_requires_a_real_contradicting_exchange(self):
        with SQLiteHistory(self.path) as history:
            self.assertTrue(hasattr(history, 'invalidate_fact'), 'Нет проверяемого INVALIDATED lifecycle')
            original = self.start(history, conditions=self.name_condition('m', 'Murka'))
            fact = history.propose_fact('p', 'A', original['run_id'], self.observation(original))
            same = self.start(history, session='B', message='same', conditions=self.name_condition('same', 'Murka'))
            with self.assertRaisesRegex(ValueError, 'fact_not_contradicted'):
                history.invalidate_fact('p', fact['id'], 'B', same['run_id'], same['exchanges'][0]['id'])
            self.server.pet['name'] = 'Barsik'
            changed = self.start(history, session='B', message='changed', conditions=self.name_condition('changed', 'Murka'))
            result = history.invalidate_fact('p', fact['id'], 'B', changed['run_id'], changed['exchanges'][0]['id'])
            self.assertEqual(result['status'], 'INVALIDATED')
            self.assertEqual(result['value'], 'Murka')
            self.assertEqual(result['invalidation']['exchange_id'], changed['exchanges'][0]['id'])
            self.assertFalse(any(f['id'] == fact['id'] for f in history.read_facts('p', catalog=self.catalog)))

    def test_401_observation_does_not_become_universal_auth_requirement(self):
        from api_agent.agent.catalog import CatalogWorkflow
        from api_agent.policy import ProjectConfig
        from agent_support import reply
        catalog = load_source(str(ROOT / 'tests/fixtures/auth_openapi.json'))
        config = ProjectConfig.create('p', self.server.target, catalog, mode='READ_ONLY', resettable=True,
                                     operations={'readProfile': 'A'})
        workflow = CatalogWorkflow(catalog, config, goal='Проверь профиль без входа',
            conditions={'readProfile': [{'predicate': 'status_in', 'parameters': {'values': [401]},
                        'intent': 'negative', 'preconditions': 'verified'}]}, anonymous_operations=['readProfile'])
        def model(payload):
            obs = [json.loads(m['content']) for m in payload['messages'] if m['role'] == 'tool']
            return (reply('finish_run', {'summary': 'Expected rejection', 'evidence_ids': obs[-1]['data']['finish_evidence_ids']})
                    if obs else reply('execute_operation', {'operation_id': 'readProfile'}))
        with SQLiteHistory(self.path) as history:
            run = StartAgentRun(self.server.target, catalog, workflow=workflow, history_store=history,
                token_counter=count_test_tokens).start('p', 'A', 'm', workflow.goal, model)
            facts = history.read_facts('p', catalog=catalog)
            observed = [f for f in facts if f['type'] == 'response_observation']
            self.assertEqual(len(observed), 1)
            self.assertEqual(observed[0]['value'], 401)
            self.assertEqual(observed[0]['evidence_ids'], [run['exchanges'][0]['id']])
            self.assertNotIn('requires_auth', json.dumps(observed))

    def test_increasing_ttl_does_not_resurrect_previously_expired_fact(self):
        with SQLiteHistory(self.path) as history:
            run = self.start(history)
            fact = history.propose_fact('p', 'A', run['run_id'], self.observation(run))
            history.configure_fact_policy('p', response_observation=1, create_read_link=1)
            future = fact['last_verified_at'] + 2
            self.assertFalse(any(f['id'] == fact['id'] for f in history.read_facts('p', catalog=self.catalog, now=future)))
            history.configure_fact_policy('p', response_observation=3600, create_read_link=3600)
            self.assertFalse(any(f['id'] == fact['id'] for f in history.read_facts('p', catalog=self.catalog, now=future)))


if __name__ == '__main__':
    unittest.main()

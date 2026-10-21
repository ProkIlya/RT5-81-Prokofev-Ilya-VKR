"""S09d1: SDK из публичного профиля, без транспорта или goal adapters."""
from copy import deepcopy
import importlib.util
import inspect
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig


def document(domain='pet'):
    field = {'pet': 'name', 'weather': 'temperature', 'stock': 'quantity'}[domain]
    value = {'type': 'string' if domain == 'pet' else 'number', 'description': 'Значение по публичному контракту'}
    row = {'type': 'object', 'properties': {field: value}, 'required': [field]}
    response = row if domain == 'pet' else {'type': 'array', 'items': row} if domain == 'stock' else {
        'type': 'object', 'properties': {'items': {'type': 'array', 'items': row}}}
    return {'openapi': '3.0.3', 'info': {'title': domain, 'version': '1'},
        'paths': {'/' + domain + '/{id}': {'get': {'operationId': 'read' + domain.title(), 'summary': 'Чтение ' + domain,
            'description': 'Получить данные; это описание не даёт прав.',
            'parameters': [{'name': 'id', 'in': 'path', 'required': True, 'description': 'Идентификатор сущности', 'schema': {'type': 'integer'}},
                           {'name': 'id', 'in': 'query', 'schema': {'type': 'string'}},
                           {'name': 'active', 'in': 'query', 'schema': {'type': 'boolean'}}],
            'responses': {'200': {'description': 'Результат', 'content': {'application/json': {'schema': response}}}}}}}}


class WorkspaceSdkTests(unittest.TestCase):
    def api(self):
        self.assertIsNotNone(importlib.util.find_spec('api_agent.workspace.sdk'), 'Нет SDK builder S09d1')
        from api_agent.workspace import sdk as workspace_sdk
        return workspace_sdk

    def build(self, doc=None, *, operations=None):
        cat = parse_document(doc or document())
        cfg = ProjectConfig.create('p', 'http://127.0.0.1:18082', cat, mode='TEST', resettable=True,
            operations=operations or {name: 'A' if op['method'] == 'GET' else 'C' for name, op in cat.operations.items()})
        return self.api().build_sdk(cat, cfg), cat, cfg

    def test_three_unrelated_schemas_exact_signatures_and_distinct_path_query(self):
        for domain in ('pet', 'weather', 'stock'):
            artifact, _, _ = self.build(document(domain))
            calls = []
            def dispatch(request): calls.append(request); return {'status': 200, 'body': {'ordinary': ['dict', 1]}}
            api = artifact.bind(dispatch)
            name = artifact.manifest['operations']['read' + domain.title()]['python_name']
            function = getattr(api, name)
            signature = inspect.signature(function)
            self.assertEqual(list(signature.parameters), ['path__id', 'query__active', 'query__id'])
            self.assertTrue(all(p.kind == inspect.Parameter.KEYWORD_ONLY for p in signature.parameters.values()))
            value = function(path__id=7, query__id='other', query__active=False)
            self.assertEqual(value, {'status': 200, 'body': {'ordinary': ['dict', 1]}})
            self.assertEqual(calls[0]['parameters'], {'path': {'id': 7}, 'query': {'id': 'other', 'active': False}, 'header': {}})
            self.assertEqual(calls[0]['operation_id'], 'read' + domain.title())
            self.assertEqual(calls[0]['artifact_hash'], artifact.hash)
            self.assertNotIn('body', calls[0])
            with self.assertRaises(TypeError): function(id=7)
            with self.assertRaises(TypeError): function(7)
            with self.assertRaises(TypeError): function(path__id=7, invented=True)

    def test_nested_body_and_optional_omission_do_not_coerce_json(self):
        doc = document('stock')
        operation = doc['paths']['/stock/{id}'].pop('get')
        operation['operationId'] = 'updateStock'
        operation['requestBody'] = {'required': True, 'content': {'application/json': {'schema': {
            'type': 'object', 'properties': {'changes': {'type': 'array', 'items': {'type': 'object', 'properties': {'quantity': {'type': 'integer'}}}}}}}}}
        doc['paths']['/stock/{id}']['post'] = operation
        artifact, _, _ = self.build(doc)
        calls = []
        api = artifact.bind(lambda request: calls.append(request) or {'status': 204, 'body': None, 'json_valid': False})
        payload = {'changes': [{'quantity': 4}]}
        self.assertEqual(api.updateStock(path__id=1, body=payload)['status'], 204)
        self.assertEqual(calls[0]['body'], payload)
        self.assertEqual(calls[0]['parameters']['query'], {})
        with self.assertRaises(TypeError): api.updateStock(path__id=1)
        # SDK не подменяет JSON null пропуском, а число — строкой.
        api.updateStock(path__id='not-an-int', query__id=None, body={'changes': []})
        self.assertEqual(calls[1]['parameters']['path']['id'], 'not-an-int')
        self.assertIsNone(calls[1]['parameters']['query']['id'])

    def test_untrusted_names_and_facade_collisions_cannot_become_code(self):
        doc = document()
        template = deepcopy(doc['paths']['/pet/{id}']['get'])
        doc['paths'] = {}
        names = ['finish', 'locals', 'read-pet', 'read_pet', '__class__', 'class', 'op_reserved', "x');raise SystemExit();#", 'питомец']
        for index, name in enumerate(names):
            op = deepcopy(template); op['operationId'] = name
            doc['paths'][f'/x{index}/{{id}}'] = {'get': op}
        artifact, _, _ = self.build(doc)
        mapping = {name: value['python_name'] for name, value in artifact.manifest['operations'].items()}
        self.assertEqual(len(set(mapping.values())), len(names))
        self.assertEqual(mapping['read_pet'], 'read_pet')
        self.assertNotEqual(mapping['finish'], 'finish')
        called = []
        api = artifact.bind(lambda request: called.append(request) or {'status': 401, 'body': {'error': 'denied'}})
        for name in names:
            self.assertEqual(getattr(api, mapping[name])(path__id=1)['status'], 401)
        self.assertEqual({v['operation_id'] for v in called}, set(names))
        self.assertTrue(callable(api.finish))
        doc['paths'] = dict(reversed(list(doc['paths'].items())))
        reordered, _, _ = self.build(doc)
        self.assertEqual(reordered.hash, artifact.hash)

    def test_metadata_change_preserves_execution_hash_and_creates_new_sdk_version(self):
        doc = document(); first, cat, cfg = self.build(doc)
        op = doc['paths']['/pet/{id}']['get']
        op['description'] = 'Новое объяснение'
        op['parameters'][0]['description'] = 'Новая подсказка ID'
        second, changed, _ = self.build(doc)
        self.assertEqual(cat.spec_hash, changed.spec_hash)
        self.assertEqual(cat.operation_hashes, changed.operation_hashes)
        self.assertNotEqual(first.manifest['metadata_hash'], second.manifest['metadata_hash'])
        self.assertNotEqual(first.hash, second.hash)
        # Старый artifact продолжает описывать свою версию, без перезаписи.
        self.assertIn('Получить данные', json.dumps(first.manifest, ensure_ascii=False))
        self.assertIn('Новое объяснение', json.dumps(second.manifest, ensure_ascii=False))
        self.assertEqual(first.manifest['config_hash'], second.manifest['config_hash'])

    def test_descriptions_from_refs_and_schema_fields_remain_untrusted_metadata(self):
        doc = document()
        op = doc['paths']['/pet/{id}']['get']
        doc['components'] = {'schemas': {'Row': op['responses']['200']['content']['application/json']['schema']}}
        op['responses']['200']['content']['application/json']['schema'] = {'$ref': '#/components/schemas/Row'}
        artifact, cat, _ = self.build(doc)
        metadata = artifact.manifest['operations']['readPet']['documentation']
        self.assertIn('Значение по публичному контракту', json.dumps(metadata, ensure_ascii=False))
        self.assertNotIn('description', cat.operations['readPet'])
        api = artifact.bind(lambda q: self.fail('Discovery must not dispatch'))
        result = api.describe('readPet', section='documentation')
        self.assertIn('Идентификатор сущности', json.dumps(result, ensure_ascii=False))

    def test_paginated_index_search_and_sections_reach_beyond_first_page_without_rpc(self):
        doc = document(); template = doc['paths']['/pet/{id}']['get']; doc['paths'] = {}
        for index in range(40):
            op = deepcopy(template); op['operationId'] = f'operation{index:02}'
            op['summary'] = 'Особая последняя операция' if index == 39 else 'Обычная операция'
            doc['paths'][f'/p{index}/{{id}}'] = {'get': op}
        artifact, _, _ = self.build(doc)
        api = artifact.bind(lambda q: self.fail('Metadata caused RPC'))
        first = api.list_operations(limit=8)
        self.assertEqual(first['total'], 40); self.assertTrue(first['more'])
        last = api.list_operations(offset=32, limit=8)
        self.assertFalse(last['more']); self.assertEqual(len(last['items']), 8)
        found = api.search('последняя')
        self.assertEqual([row['operation_id'] for row in found['items']], ['operation39'])
        self.assertEqual(api.describe('operation39', section='responses')['value']['200']['content']['application/json']['schema']['type'], 'object')
        short = artifact.initial_index(max_bytes=256)
        self.assertTrue(short['more']); self.assertEqual(short['total'], 40)
        with self.assertRaises(ValueError): api.list_operations(offset=-1)
        with self.assertRaises(ValueError): api.describe('not-granted')

    def test_large_schema_is_read_by_pointer_with_explicit_omissions(self):
        doc = document()
        schema = doc['paths']['/pet/{id}']['get']['responses']['200']['content']['application/json']['schema']
        schema['properties'] = {f'field{i}': {'type': 'string'} for i in range(100)}
        schema.pop('required')
        artifact, _, _ = self.build(doc)
        api = artifact.bind(lambda q: self.fail('Metadata RPC'))
        result = api.describe('readPet', section='responses')
        self.assertTrue(result['truncated'])
        leaf = api.describe('readPet', section='responses', pointer='/200/content/application~1json/schema/properties/field99')
        self.assertEqual(leaf['value'], {'type': 'string'})
        self.assertFalse(leaf['truncated'])

    def test_only_project_grants_are_generated_and_stale_config_fails(self):
        doc = document(); op = deepcopy(doc['paths']['/pet/{id}']['get']); op['operationId'] = 'foreign'
        doc['paths']['/other/{id}'] = {'get': op}
        artifact, cat, cfg = self.build(doc, operations={'readPet': 'A'})
        self.assertEqual(set(artifact.manifest['operations']), {'readPet'})
        self.assertEqual(artifact.manifest['exclusions'], [{'operation_id': 'foreign', 'reason': 'not_granted'}])
        doc['paths']['/pet/{id}']['get']['responses']['201'] = {'description': 'new'}
        with self.assertRaises(ValueError): self.api().build_sdk(parse_document(doc), cfg)

    def test_immutable_artifact_roundtrip_and_tampering_is_rejected(self):
        artifact, _, _ = self.build()
        altered = artifact.manifest; altered['operations'].clear()
        self.assertEqual(len(artifact.manifest['operations']), 1)
        with tempfile.TemporaryDirectory() as folder:
            path = artifact.save(Path(folder))
            restored = self.api().SdkArtifact.load(path, expected_hash=artifact.hash)
            self.assertEqual(restored.hash, artifact.hash)
            self.assertEqual(artifact.save(Path(folder)), path)
            file = path / 'sdk.py'; file.write_text('raise SystemExit()', encoding='utf-8')
            with self.assertRaises(ValueError): self.api().SdkArtifact.load(path, expected_hash=artifact.hash)
            with self.assertRaises(ValueError): artifact.save(Path(folder))

    def test_application_facade_exact_calls_cannot_supply_fact_evidence_or_verdict(self):
        artifact, _, _ = self.build(); requests = []
        api = artifact.bind(lambda request: requests.append(request) or {'ok': True})
        api.ask_user('Какое значение?', input_slot='expected')
        api.check_goal()
        api.propose_fact({'type': 'response_observation', 'verify': {'predicate': 'status_in'}})
        api.finish('Проверка выполнена.', claims=[{'condition_id': 'c1', 'verdict': 'FAIL'}], answer_fields={'a1': 1})
        self.assertEqual([q['kind'] for q in requests], ['ask_user', 'check_goal', 'propose_fact', 'finish'])
        self.assertTrue(all(q['artifact_hash'] == artifact.hash for q in requests))
        with self.assertRaises(TypeError): api.check_goal(verdict='PASS')
        with self.assertRaises(TypeError): api.propose_fact({}, evidence={'foreign': True})
        with self.assertRaises(TypeError): api.ask_user('new requirement')

    def test_long_identifiers_and_small_index_budget_remain_navigable(self):
        doc = document()
        operation = doc['paths']['/pet/{id}']['get']
        name = 'read' + 'a' * 10000
        operation['operationId'] = name
        artifact, _, _ = self.build(doc)
        alias = artifact.manifest['operations'][name]['python_name']
        self.assertLess(len(alias), 128)
        index = artifact.initial_index(max_bytes=128)
        self.assertLessEqual(len(json.dumps(index, ensure_ascii=False, separators=(',', ':')).encode()), 128)
        api = artifact.bind(lambda q: {'status': 200, 'body': None})
        page = api.list_operations()
        self.assertEqual(len(page['items']), 1)
        self.assertEqual(page['items'][0]['python_name'], alias)
        self.assertTrue(page['items'][0]['operation_id']['omitted'])
        overview = api.describe(alias, pointer='/operation_id')
        self.assertTrue(overview['truncated'])

    def test_source_and_manifest_changes_cannot_be_loaded_with_old_expected_hash(self):
        artifact, _, _ = self.build()
        with tempfile.TemporaryDirectory() as folder:
            path = artifact.save(folder)
            manifest = json.loads((path / 'manifest.json').read_bytes())
            manifest['operations']['readPet']['path'] = '/different'
            (path / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            with self.assertRaises(ValueError): self.api().SdkArtifact.load(path, expected_hash=artifact.hash)

    def test_project_builder_cli_saves_artifact_and_reuses_identical_version(self):
        from contextlib import redirect_stdout
        import io
        artifact, _, cfg = self.build()
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            spec = folder / 'api.json'; spec.write_text(json.dumps(document()), encoding='utf-8')
            config = folder / 'config.json'; cfg.save(config)
            outputs = []
            for _ in range(2):
                with redirect_stdout(io.StringIO()) as output:
                    code = self.api().main(['--spec', str(spec), '--project-config', str(config), '--output', str(folder / 'sdk')])
                self.assertEqual(code, 0)
                outputs.append(json.loads(output.getvalue()))
            self.assertEqual(outputs[0]['artifact_hash'], artifact.hash)
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(len(list((folder / 'sdk').iterdir())), 1)

    def test_truncated_description_tail_still_changes_metadata_identity(self):
        doc = document(); op = doc['paths']['/pet/{id}']['get']
        op['description'] = 'x' * 3000 + 'A'
        first, before, _ = self.build(doc)
        op['description'] = 'x' * 3000 + 'B'
        second, after, _ = self.build(doc)
        self.assertEqual(before.spec_hash, after.spec_hash)
        self.assertNotEqual(first.manifest['metadata_hash'], second.manifest['metadata_hash'])
        descriptions = first.manifest['operations']['readPet']['documentation']
        self.assertTrue(any(item['truncated'] for item in descriptions.values()))

    def test_shared_path_description_is_preserved_for_each_allowed_operation(self):
        doc = document(); item = doc['paths']['/pet/{id}']
        item['description'] = 'Общая подсказка для группы операций'
        artifact, _, _ = self.build(doc)
        descriptions = artifact.manifest['operations']['readPet']['documentation']
        self.assertIn('Общая подсказка', json.dumps(descriptions, ensure_ascii=False))


if __name__ == '__main__': unittest.main()

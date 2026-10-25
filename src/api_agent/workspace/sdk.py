"""S09d1: детерминированный SDK/metadata artifact; собственных HTTP прав нет.

Сгенерированные функции только формируют типизированные сообщения dispatcher.
Проверка policy, входов, фактов и evidence принадлежит внешнему broker S09d2.
"""
import ast
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import keyword
from pathlib import Path
import re

from ..bounded_json import validate_tree
from ..openapi.parser import canonical, pointer_get, pointer_part
from ..openapi.documentation import documentation_preview
from ..policy import ProjectConfig
from ..validation import fingerprint

BUILDER_VERSION = 'workspace-sdk-v3'
METADATA_VERSION = 'workspace-metadata-v1'
FACADES = {'list_operations', 'search', 'describe', 'check_goal', 'ask_user', 'propose_fact', 'finish'}
MISSING = object()


def _name(value, prefix, reserved=()):
    if len(value) <= 64 and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', value) and not keyword.iskeyword(value) and value not in reserved and not value.startswith(prefix):
        return value
    return prefix + hashlib.sha256(value.encode()).hexdigest()


def _operation_names(names):
    """Читаемые aliases при безопасной уникальной замене пунктуации; иначе hash."""
    reserved = FACADES | {'locals'}
    result = {name: _name(name, 'op_', reserved) for name in names}
    candidates = {name: re.sub(r'[^A-Za-z0-9_]', '_', name)
                  for name in names if result[name] != name}
    counts = Counter(candidates.values())
    for name, candidate in candidates.items():
        if (counts[candidate] == 1 and candidate not in result.values()
                and _name(candidate, 'op_', reserved) == candidate):
            result[name] = candidate
    return result


def _source(operations):
    """Только AST identifiers и literals: текст спецификации не Python syntax."""
    functions = []
    types = {'string': 'str', 'integer': 'int', 'number': 'float', 'boolean': 'bool', 'object': 'dict', 'array': 'list'}
    for name, operation in sorted(operations.items()):
        function = ast.parse('def template():\n    return _invoke(None, locals())').body[0]
        function.name = operation['python_name']
        fields = operation['parameter_mapping'] + ([{'python_name': 'body', 'required': operation['body_required'],
            'schema': operation['body']}] if operation['body'] is not None else [])
        function.args.kwonlyargs = [ast.arg(arg=p['python_name'], annotation=ast.Constant(types.get(p['schema'].get('type'), 'object'))
            if isinstance(p['schema'].get('type'), str) else ast.Constant('object')) for p in fields]
        function.args.kw_defaults = [None if p['required'] else ast.Name(id='_MISSING', ctx=ast.Load()) for p in fields]
        function.body[0].value.args[0] = ast.Constant(name)
        notes = documentation_preview(operation, operation.get('documentation', {}))
        if notes:
            function.body.insert(0, ast.Expr(value=ast.Constant(json.dumps(notes, ensure_ascii=False))))
        functions.append(function)
    return ast.unparse(ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))) + '\n'


def build_sdk(catalog, config):
    """Сборка по Project/spec, без цели, модели, discovery router или HTTP."""
    snapshot = config.snapshot
    try:
        checked = ProjectConfig.create(snapshot['project_id'], snapshot['target'], catalog, mode=snapshot['mode'],
            resettable=snapshot['resettable'], version=snapshot['version'],
            operations={name: grant['risk'] for name, grant in snapshot['operations'].items()})
        if checked.snapshot != snapshot: raise ValueError()
    except (ValueError, KeyError, TypeError): raise ValueError('sdk_stale_or_invalid_config') from None
    operations = {}
    names = _operation_names(snapshot['operations'])
    for name in sorted(snapshot['operations']):
        op = catalog.operations[name]
        body = op.get('requestBody', {})
        operations[name] = {'python_name': names[name], 'operation_hash': catalog.operation_hashes[name],
            'method': op['method'], 'path': op['path'], 'security': op['security'], 'responses': op['responses'],
            'body': body.get('content', {}).get('application/json', {}).get('schema'), 'body_required': body.get('required', False),
            'parameter_mapping': [{**p, 'python_name': p['in'] + '__' + _name(p['name'], 'p_')} for p in op['parameters']],
            'summary': catalog.search_metadata.get(name, {}).get('summary', ''),
            'documentation': catalog.documentation.get(name, {})}
    metadata = {name: {key: op[key] for key in ('summary', 'documentation')} for name, op in operations.items()}
    manifest = {'builder_version': BUILDER_VERSION, 'metadata_version': METADATA_VERSION,
        'profile_version': catalog.snapshot['profile_version'], 'parser_version': catalog.snapshot['parser_version'],
        'spec_hash': catalog.spec_hash, 'config_hash': fingerprint(snapshot), 'config_version': snapshot['version'],
        'project_id': snapshot['project_id'], 'metadata_hash': fingerprint(metadata), 'operations': operations,
        'exclusions': [{'operation_id': name, 'reason': 'not_granted'} for name in sorted(catalog.operations.keys() - operations.keys())],
        'source_hash': hashlib.sha256(_source(operations).encode()).hexdigest()}
    validate_tree(manifest, max_depth=64, max_nodes=100000, max_bytes=4194304)
    return SdkArtifact(canonical(manifest))


@dataclass(frozen=True)
class SdkArtifact:
    """Immutable bytes, content-addressed хранение и trusted expected hash."""
    _payload: bytes

    @property
    def hash(self): return hashlib.sha256(self._payload).hexdigest()

    @property
    def manifest(self): return {**json.loads(self._payload), 'artifact_hash': self.hash}

    @property
    def source(self): return _source(self.manifest['operations'])

    def bind(self, dispatcher):
        if not callable(dispatcher): raise ValueError('sdk_dispatcher_required')
        return _Api(self, dispatcher)

    def initial_index(self, *, max_bytes=4096):
        if type(max_bytes) is not int or max_bytes < 128: raise ValueError('sdk_index_limit')
        rows = [_entry(name, op) for name, op in self.manifest['operations'].items()]
        result = {'items': rows, 'total': len(rows), 'more': False, 'omitted': 0}
        if len(canonical(result)) > max_bytes:
            result.update(items=[], more=bool(rows), omitted=len(rows), discovery='api.list_operations(offset=0, limit=16)')
            if len(canonical(result)) > max_bytes: result.pop('discovery')
        return result

    def save(self, root):
        root = Path(root).resolve()
        directory = root / self.hash
        if directory.is_symlink() or not directory.resolve().is_relative_to(root): raise ValueError('sdk_unsafe_path')
        directory.mkdir(parents=True, exist_ok=True)
        files = {'manifest.json': canonical(self.manifest), 'sdk.py': self.source.encode()}
        for name, data in files.items():
            file = directory / name
            if file.is_symlink() or file.exists() and file.read_bytes() != data: raise ValueError('sdk_artifact_changed')
        for name, data in files.items():
            file = directory / name
            try:
                with file.open('xb') as stream: stream.write(data)
            except FileExistsError:
                if file.read_bytes() != data: raise ValueError('sdk_artifact_changed')
        return directory

    @classmethod
    def load(cls, directory, *, expected_hash):
        try:
            directory = Path(directory)
            if directory.is_symlink() or any((directory / name).is_symlink() for name in ('manifest.json', 'sdk.py')): raise ValueError()
            if (directory / 'manifest.json').stat().st_size > 4194304: raise ValueError()
            manifest = json.loads((directory / 'manifest.json').read_bytes())
            if (manifest.pop('artifact_hash') != expected_hash
                    or manifest['builder_version'] not in {'workspace-sdk-v1', BUILDER_VERSION}): raise ValueError()
            artifact = cls(canonical(manifest))
            if artifact.hash != expected_hash or (directory / 'sdk.py').read_bytes() != artifact.source.encode(): raise ValueError()
            if hashlib.sha256(artifact.source.encode()).hexdigest() != manifest['source_hash']: raise ValueError()
            return artifact
        except (OSError, ValueError, KeyError, TypeError, SyntaxError): raise ValueError('sdk_artifact_changed') from None


def _entry(name, operation):
    return {'operation_id': name if len(name.encode()) <= 256 else {'omitted': True, 'bytes': len(name.encode())},
        'python_name': operation['python_name'], 'summary': operation['summary']}


def _page(rows, offset, limit):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 32:
        raise ValueError('sdk_invalid_page')
    selected = deepcopy(rows[offset:offset + limit])
    result = {'items': selected, 'total': len(rows), 'offset': offset, 'more': offset + len(selected) < len(rows)}
    while len(canonical(result)) > 8192 and selected:
        selected.pop(); result['more'] = True
    if not selected and offset < len(rows): raise ValueError('sdk_metadata_item_limit')
    if len(canonical(result)) > 8192: raise ValueError('sdk_metadata_limit')
    return result


class _Api:
    """Тонкий facade; dispatcher — внешняя trust boundary, а не права SDK."""
    def __init__(self, artifact, dispatcher):
        self._artifact, self._dispatcher = artifact, dispatcher
        self._operations = artifact.manifest['operations']
        namespace = {'_invoke': self._invoke, '_MISSING': MISSING, 'locals': locals, '__builtins__': {}}
        exec(compile(artifact.source, '<generated-sdk>', 'exec'), namespace)
        for op in self._operations.values(): setattr(self, op['python_name'], namespace[op['python_name']])

    def _send(self, kind, **data):
        message = {'artifact_hash': self._artifact.hash, 'kind': kind, **data}
        validate_tree(message, max_depth=32, max_bytes=32768)
        return self._dispatcher(deepcopy(message))

    def _invoke(self, operation, values):
        descriptor = self._operations[operation]
        parameters = {'path': {}, 'query': {}, 'header': {}}
        for p in descriptor['parameter_mapping']:
            value = values[p['python_name']]
            if value is not MISSING: parameters[p['in']][p['name']] = value
        body = values.get('body', MISSING)
        return self._send('operation', operation_id=operation, parameters=parameters,
            **({'body': body} if body is not MISSING else {}))

    def list_operations(self, *, offset=0, limit=16):
        rows = [_entry(name, op) for name, op in self._operations.items()]
        return _page(rows, offset, limit)

    def search(self, query, *, offset=0, limit=16):
        if not isinstance(query, str) or len(query) > 500: raise ValueError('sdk_invalid_query')
        terms = query.casefold().split()
        rows = []
        for name, op in self._operations.items():
            text = (name + ' ' + op['summary'] + ' ' + json.dumps(op['documentation'], ensure_ascii=False)).casefold()
            if all(term in text for term in terms): rows.append(_entry(name, op))
        return _page(rows, offset, limit)

    def describe(self, operation, *, section='overview', pointer='', offset=0, limit=16):
        name = next((name for name, op in self._operations.items() if operation in (name, op['python_name'])), None)
        if name is None: raise ValueError('sdk_unknown_operation')
        op = self._operations[name]
        sections = {'overview': {'operation_id': name, **{key: op[key] for key in ('python_name', 'method', 'path', 'security', 'summary', 'operation_hash')}},
            'inputs': op['parameter_mapping'], 'body': op['body'], 'responses': op['responses'], 'documentation': op['documentation']}
        if section not in sections or not isinstance(pointer, str): raise ValueError('sdk_invalid_section')
        try: node = pointer_get(sections[section], pointer) if pointer else sections[section]
        except (KeyError, TypeError, IndexError): raise ValueError('sdk_invalid_pointer') from None
        _page([], offset, limit)
        if len(canonical(node)) <= 2048 and offset == 0:
            return {'value': deepcopy(node), 'pointer': pointer, 'truncated': False, 'more': False}
        if isinstance(node, str):
            return {'value': node[offset:offset + 1024], 'characters_total': len(node), 'offset': offset,
                'more': offset + 1024 < len(node), 'truncated': True}
        pairs = list(node.items()) if isinstance(node, dict) else list(enumerate(node)) if isinstance(node, list) else []
        rows = [{'key': key, 'pointer': pointer + '/' + pointer_part(str(key)),
            'value': value if len(canonical(value)) <= 512 else {'omitted': True, 'bytes': len(canonical(value))}}
            for key, value in pairs]
        return {**_page(rows, offset, limit), 'truncated': True, 'pointer': pointer}

    def ask_user(self, question, *, input_slot): return self._send('ask_user', question=question, input_slot=input_slot)

    def check_goal(self): return self._send('check_goal')

    def propose_fact(self, descriptor): return self._send('propose_fact', descriptor=descriptor)

    def finish(self, summary, *, claims, answer_fields):
        return self._send('finish', summary=summary, claims=claims, answer_fields=answer_fields)


def main(argv=None):
    """Подготовить SDK для версии Project/spec, без создания или исполнения Run."""
    import argparse
    from ..openapi import load_source
    parser = argparse.ArgumentParser(description='Build a versioned SDK from an approved Project config')
    parser.add_argument('--spec', required=True)
    parser.add_argument('--project-config', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        catalog = load_source(args.spec)
        artifact = build_sdk(catalog, ProjectConfig.load(args.project_config, catalog))
        directory = artifact.save(args.output)
        print(json.dumps({'artifact_hash': artifact.hash, 'directory': str(directory),
            'operations': len(artifact.manifest['operations'])}, ensure_ascii=False))
        return 0
    except (ValueError, OSError):
        print(json.dumps({'error': 'sdk_build_failed'}))
        return 1


if __name__ == '__main__': raise SystemExit(main())

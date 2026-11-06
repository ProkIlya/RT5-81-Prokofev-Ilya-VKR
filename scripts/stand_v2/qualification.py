"""Узкая квалификация R14-01 после frozen диагностики; не новая модельная серия."""
import ast
import hashlib
import json
from pathlib import Path
import zipfile

BASELINE = '2d9a2b6eeb60f46ee0fe8989e0057f70f751c5896ea2187306864d6491d20681'
CHANGED = {
    'src/api_agent/desktop/application/app.py',
    'src/api_agent/desktop/views/project_setup.py',
    'src/api_agent/setup/credentials.py',
    'tests/test_setup_ui.py', 'scripts/check_setup_stage.py'}
ADDED = {'scripts/stand_v2/qualification.py', 'tests/test_stand_v2_qualification.py'}
METHODS = {
    'src/api_agent/desktop/application/app.py': {'AgentApp.create_project'},
    'src/api_agent/desktop/views/project_setup.py': {
        'ProjectSetupDialog.on_button_pressed', 'ProjectSetupDialog.dismiss'},
    'scripts/check_setup_stage.py': {'live_evidence', 'controls'}}


def manifest_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def normalized(name, raw):
    """Остальная логика/сигнатуры неизменны; игнорируются только перечисленные тела."""
    class Scope(ast.NodeTransformer):
        def __init__(self): self.prefix = []

        def visit_ClassDef(self, node):
            if name == 'tests/test_setup_ui.py' and node.name == 'ConnectionSourceTests': return None
            self.prefix.append(node.name); node = self.generic_visit(node); self.prefix.pop()
            return node

        def visit_FunctionDef(self, node):
            key = '.'.join(self.prefix + [node.name])
            if name == 'src/api_agent/setup/credentials.py' and key == 'project_connection': return None
            if key in METHODS.get(name, set()): node.body = [ast.Pass()]
            return node

        def visit_AsyncFunctionDef(self, node): return self.visit_FunctionDef(node)

        def visit_ImportFrom(self, node):
            if (name == 'tests/test_setup_ui.py' and node.module == 'contextlib'
                    and [a.name for a in node.names] == ['asynccontextmanager']): return None
            return node

        def visit_Assign(self, node):
            if (name == 'scripts/check_setup_stage.py' and any(isinstance(t, ast.Name)
                    and t.id == 'B_MODULES' for t in node.targets) and isinstance(node.value, ast.List)):
                node.value.elts = [e for e in node.value.elts if not (isinstance(e, ast.Constant)
                    and e.value == 'test_stand_v2_qualification')]
            return node
    return ast.dump(Scope().visit(ast.parse(raw.decode('utf-8-sig'))), include_attributes=False)


def validate_delta(before, current, archive_path, root):
    """Сверить реальные bytes; нельзя квалифицировать Core/SDK/fixtures/oracle/лимиты."""
    root = Path(root).resolve()
    changed = sorted(name for name in before if current.get(name) != before[name])
    added = sorted(set(current) - set(before))
    if not before or set(before) - set(current) or set(changed) - CHANGED or set(added) - ADDED:
        raise ValueError('setup_delta_paths')
    try:
        with zipfile.ZipFile(archive_path) as archive:
            for name in changed:
                old = archive.read(name); path = (root / name).resolve(); path.relative_to(root)
                new = path.read_bytes()
                if hashlib.sha256(old).hexdigest() != before[name] or hashlib.sha256(new).hexdigest() != current[name]:
                    raise ValueError('setup_delta_bytes')
                if normalized(name, old) != normalized(name, new): raise ValueError('setup_delta_other_logic')
            for name in added:
                path = (root / name).resolve(); path.relative_to(root)
                if hashlib.sha256(path.read_bytes()).hexdigest() != current[name]: raise ValueError('setup_delta_bytes')
    except (KeyError, OSError, SyntaxError, zipfile.BadZipFile) as error:
        raise ValueError('setup_delta_archive_or_source') from error
    return {'scope': 'R14-01 setup-only release qualification; frozen cohort retains its producer identity',
        'baseline_manifest_sha256': manifest_hash(before), 'release_manifest_sha256': manifest_hash(current),
        'changed': changed, 'added': added, 'execution_sources_unchanged': True,
        'new_model_trials': 0, 'same_source_cohort_claimed': False}


def qualify_setup_evidence(directory, current, root):
    directory = Path(directory)
    before = json.loads((directory / 'source-manifest.json').read_text(encoding='utf-8'))
    freeze = json.loads((directory / 'freeze.json').read_text(encoding='utf-8'))
    if manifest_hash(before) != BASELINE or freeze.get('source_manifest_sha256') != BASELINE:
        raise ValueError('setup_delta_unknown_baseline')
    return validate_delta(before, current, directory / 'source-snapshot.zip', root)

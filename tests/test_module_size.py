"""Ограничение размера поддерживаемых модулей проекта."""
from pathlib import Path
import unittest


class ModuleSizeTests(unittest.TestCase):
    def test_owned_python_modules_fit_500_lines(self):
        root = Path(__file__).resolve().parents[1]
        oversized = []
        for folder in ('src', 'tests', 'scripts', 'demo_api'):
            for path in (root / folder).rglob('*.py'):
                lines = len(path.read_text(encoding='utf-8-sig').splitlines())
                if lines > 500:
                    oversized.append(f'{path.relative_to(root)}: {lines}')
        self.assertEqual(oversized, [], 'Модули длиннее 500 строк:\n' + '\n'.join(oversized))

    def test_runtime_root_keeps_only_package_and_shared_json_boundary(self):
        root = Path(__file__).resolve().parents[1] / 'src/api_agent'
        ungrouped = sorted(p.name for p in root.glob('*.py')
                           if p.name not in {'__init__.py', 'bounded_json.py'})
        self.assertEqual(ungrouped, [], 'Перенесите runtime-модули в профильный пакет')

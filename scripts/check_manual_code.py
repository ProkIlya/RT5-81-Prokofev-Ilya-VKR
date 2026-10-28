"""Регрессия выбранного manual code после O1; прежний142-test профиль сохранён отдельно."""
from contextlib import redirect_stderr,redirect_stdout
import hashlib
import json
from pathlib import Path
import time
import unittest
import os
import sys
import uuid
sys.path[:0]=[str(Path(__file__).resolve().parents[1]/name) for name in ("src","tests","")]

root=Path(os.environ.get("MANUAL_CORE_EVIDENCE_DIR", "evidence/S10/manual-code-"+uuid.uuid4().hex));root.mkdir(parents=True,exist_ok=True)
# Полный текущий discover; архивные generator/native tests хранятся отдельно.
if os.environ.get('WORKSPACE_DOCKER_TESTS') != '1' or not os.environ.get('WORKSPACE_IMAGE'):
    print('WORKSPACE_DOCKER_TESTS=1 and pinned WORKSPACE_IMAGE required', file=sys.stderr)
    raise SystemExit(3)
started=time.monotonic()
def identity():
    return {p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('src').rglob('*')
        if p.is_file() and p.suffix in {'.py','.sql'}}

def test_ids(suite):
    for item in suite:
        if isinstance(item,unittest.TestSuite):yield from test_ids(item)
        else:yield item.id()

def test_identity():
    return {p.as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in Path('tests').rglob('*')
        if p.is_file() and p.suffix in {'.py','.json'}}

before=identity()
tests_before=test_identity()
runner_before=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
with (root/'post-checks.log').open('x',encoding='utf-8') as log,redirect_stdout(log),redirect_stderr(log):
    suite=unittest.defaultTestLoader.discover('tests')
    ids=list(test_ids(suite))
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(suite)
after=identity()
data={'discovery':'tests/test*.py','tests_run':result.testsRun,'failures':len(result.failures),
    'errors':len(result.errors),'skips':len(result.skipped),'seconds':time.monotonic()-started,
    'source_hashes':before,'test_ids':ids,'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    'test_source_hashes':tests_before,'tests_unchanged':tests_before==test_identity(),
    'source_unchanged':before==after,'passing':result.wasSuccessful() and before==after and tests_before==test_identity()
        and runner_before==hashlib.sha256(Path(__file__).read_bytes()).hexdigest() and not result.skipped,
    'scope':'Полный текущий manual/Core/S10 discover; Scripted primary, actual worker/HTTP/GGUF where required. No skips accepted.',
    'historical_native_tests':'Исходные tests и прежние scripts сохранены побайтно в архиве. Текущий discover включает manual equivalents и прямые проверки SDK/broker/sandbox; архивные generator/native entry tests требуют исторической версии.'}
(root/'post-checks.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
print({k:v for k,v in data.items() if k not in {'requested_tests','historical_native_tests','source_hashes','test_ids','test_source_hashes'}})
raise SystemExit(0 if data['passing'] else 1)

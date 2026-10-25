"""Три подряд в каждом режиме через неизменный reference runner, без скрытых retries."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
FREEZE = ROOT / 'evidence/S09/S09d3-20261021'
sys.path.insert(0, str(ROOT / 'src'))
from api_agent.agent.model import configured_goal_model, goal_sampling


def runtime_identity():
    """Не смешивать разные загруженные модели/конфигурации в одной серии."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:18081/v1/models', timeout=10) as response:
        raw = response.read(1048577)
    if len(raw) > 1048576: raise ValueError('runtime_identity_limit')
    models = json.loads(raw)
    names = {name for item in models.get('data', [])
             for name in [item.get('id'), *item.get('aliases', [])]}
    if configured_goal_model() not in names: raise ValueError('runtime_model_mismatch')
    # llama.cpp создаёт этот timestamp при чтении, а не при загрузке модели.
    for item in models.get('data', []): item.pop('created', None)
    return {'name': configured_goal_model(), 'sampling': goal_sampling(),
            'worker_image': os.environ.get('WORKSPACE_IMAGE'), 'models': models}


def same_runtime(expected):
    try: return runtime_identity() == expected
    except Exception: return False


def hashes(paths):
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def frozen_changes():
    changed = []
    for name in ('reference-test-freeze.json', 'reference-external-test-freeze.json'):
        baseline = json.loads((FREEZE / name).read_text(encoding='utf-8'))['sha256']
        changed.extend(path for path, digest in baseline.items()
                       if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != digest)
    return changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    sources = [*sorted((ROOT / 'src').rglob('*.py')), Path(__file__).resolve(),
               ROOT / 'scripts/evaluate_workspace_trip.py', ROOT / 'scripts/stand_trip_case.py']
    identity = hashes(sources)
    result = {'source_hashes': identity, 'required_consecutive': 3,
              'streaks': {'controlled': 0, 'live': 0}, 'attempts': [], 'passing': False}
    def save():
        (output / 'series.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    try: result['runtime'] = runtime_identity()
    except Exception:
        result['error'] = 'runtime_identity_error'; save(); return 1
    save()
    if frozen_changes():
        result['error'] = 'frozen_test_changed'; save(); return 1
    for mode in ('controlled', 'live'):
        for index in range(1, 4):
            if hashes(sources) != identity or frozen_changes() or not same_runtime(result['runtime']):
                result['error'] = 'source_test_or_runtime_changed'; save(); return 1
            attempt = output / (mode + '-' + str(index))
            command = [sys.executable, '-X', 'utf8', str(ROOT / 'scripts/evaluate_workspace_trip.py'),
                       '--preparation', mode, '--output', str(attempt)]
            completed = subprocess.run(command, cwd=ROOT, capture_output=True)
            attempt.mkdir(exist_ok=True)
            (attempt / 'runner.stdout.log').write_bytes(completed.stdout)
            (attempt / 'runner.stderr.log').write_bytes(completed.stderr)
            summary_path = attempt / 'summary.json'
            summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {}
            source_unchanged = hashes(sources) == identity
            runtime_unchanged = same_runtime(result['runtime'])
            tests_unchanged = not frozen_changes()
            unchanged = source_unchanged and runtime_unchanged and tests_unchanged
            run_path = attempt / 'run.json'
            run = json.loads(run_path.read_text(encoding='utf-8')) if run_path.exists() else {}
            recorded_model = run.get('model', {})
            correct_model = (recorded_model.get('name') == result['runtime']['name']
                and all(recorded_model.get('sampling', {}).get(k) == v
                        for k, v in result['runtime']['sampling'].items()))
            success = (completed.returncode == 0 and summary.get('task_success') is True
                       and summary.get('source_unchanged') is True and summary.get('preparation') == mode
                       and unchanged and correct_model)
            record = {'mode': mode, 'index': index, 'exit_code': completed.returncode,
                      'task_success': success, 'stop_reason': summary.get('stop_reason'),
                      'source_unchanged': source_unchanged, 'runtime_unchanged': runtime_unchanged,
                      'tests_unchanged': tests_unchanged, 'model_matches': correct_model, 'evidence': str(attempt)}
            result['attempts'].append(record)
            result['streaks'][mode] = result['streaks'][mode] + 1 if success else 0
            save(); print(json.dumps(record, ensure_ascii=False), flush=True)
            if not success: return 1
    result['passing'] = all(value == 3 for value in result['streaks'].values())
    save()
    return 0 if result['passing'] else 1


if __name__ == '__main__': raise SystemExit(main())

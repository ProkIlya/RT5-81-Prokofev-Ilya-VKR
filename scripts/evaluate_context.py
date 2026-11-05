"""Адресные реальные запуски с измеряемым сжатием в двух режимах S13."""
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / name) for name in ('src', 'tests', '')]

from api_agent.agent.model import LocalModel
from api_agent.execution.contracts import CatalogLimits
from api_agent.storage import SQLiteHistory
from check_desktop_stage import identities
from recovery_support import RecoveryServer, application
from selected_support import card, configuration


def save(path, value):
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')


def run_case(directory, mode, endpoint):
    directory.mkdir()
    with RecoveryServer() as server, SQLiteHistory(directory / 'history.sqlite3') as history:
        values = dict(enumerate((57, 80, 91, 63, 72, 86, 105, 112), start=42))
        server.devices = {identifier: {'id': identifier, 'value': value, 'label': None,
            'rows': [{'name': sha256(f'{identifier}/{index}'.encode()).hexdigest(), 'amount': index}
                for index in range(16)]} for identifier, value in values.items()}
        app = application(history, server, limits=CatalogLimits(max_context_tokens=6144))
        model = LocalModel(endpoint)
        app.token_counter = model.count_tokens
        cards = [card(where={'parameters': {'id': 42}}, assertions=[{
            'predicate': 'equals', 'path': '/value', 'value': 57}])] if mode == 'testing' else []
        document = configuration(cards, mode=mode, goal=(
            'Прочитай восемь приборов с номерами 42, 43, 44, 45, 46, 47, 48 и 49. '
            'Сохрани каждый HTTP-ответ в Python. Когда прочитаешь все приборы, вернись '
            'к сохранённому ответу прибора 42 и начни итоговый список с его значения. '
            'Для каждого прибора укажи пару «номер — значение» по полю value '
            'в JSON-теле фактического ответа (response["body"]["value"]). API-данные не изменяй.'))
        preview = app.preview(document)
        save(directory / 'input.json', {'document': document, 'limits': vars(app.limits),
            'accepted_hash': preview['preview_hash']})
        result = app.start('p', 's', 'live-' + mode, document, model, accepted_hash=preview['preview_hash'])
        save(directory / 'run.json', result)
        save(directory / 'observer.json', {'journal': server.journal, 'values': values})
        summary = (result.get('final') or {}).get('summary', '')
        expected_paths = {f'/devices/{identifier}' for identifier in values}
        task = (result['status'] == 'completed' and {path for _, path, _ in server.journal} == expected_paths
            and all(method == 'GET' for method, _, _ in server.journal)
            and all(re.search(rf'\b{identifier}\b[^\d\n]{{0,48}}\b{value}\b', summary)
                for identifier, value in values.items()))
        compaction = result.get('context_projections', [])
        return {'mode': mode, 'status': result['status'], 'stop_reason': result['stop_reason'],
            'task_success': task, 'checks': result['report']['checks_verdict'], 'projections': compaction,
            'compacted': any(item['input_tokens_after'] < item['input_tokens_before'] for item in compaction),
            'resources': result['resources'], 'latency_seconds': result['wall_seconds']}


def main(output):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    source = identities()
    save(output / 'identity.json', {'source_and_tests': source,
        'model_name': os.environ['API_AGENT_MODEL_NAME'], 'model_lock': json.loads(
            (ROOT / 'experiments/local_model/qwen35-4b-lock.json').read_text(encoding='utf-8'))})
    records = []
    for mode in ('research', 'testing'):
        record = run_case(output / mode, mode, os.environ['WORKSPACE_TOKENIZER_URL'])
        records.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
    passed = all(r['task_success'] and r['compacted'] and
        r['checks'] == ('PASS' if r['mode'] == 'testing' else 'N/A') for r in records) and identities() == source
    save(output / 'summary.json', {'records': records, 'passing': passed, 'source_unchanged': identities() == source})
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1]))

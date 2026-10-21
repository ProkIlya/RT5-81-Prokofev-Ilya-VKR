# API Testing Agent

**ВКР:** «Разработка интеллектуальной агентной системы функционального тестирования REST API по спецификации OpenAPI с использованием локальных языковых моделей».

**Автор:** Прокофьев Илья Викторович, группа РТ5-81Б.

Проект посвящён приложению, которое по OpenAPI и сформулированной пользователем цели проверяет разрешённый тестовый REST API. Локальная языковая модель предлагает следующий шаг, а программа проверяет допустимость действия, выполняет HTTP-запрос, сопоставляет ответ с контрактом и сохраняет свидетельства. Агентный цикл нужен для сценариев, где последующие действия зависят от фактических ответов API.

Планируемый результат включает ограниченный профиль OpenAPI, безопасный доступ к одному разрешённому test target на Project, последовательный agent loop, детерминированную проверку ответов, историю запусков и находок, терминальный интерфейс Textual и сбрасываемый Demo API. Под «диагностикой» понимается анализ наблюдаемых HTTP- и контрактных отклонений, а не доказательство внутренней причины дефекта.

**Состояние и документация:** [PROGRESS.md](PROGRESS.md) — текущий этап и ограничения; [docs/README.md](docs/README.md) — карта документов. Полное приложение ещё не готово.

**На шаге S02** добавлены сбрасываемый Demo API и первый живой сценарий: локальная модель вызывает `execute_operation(getPet)`, программа выполняет один разрешённый `GET /pets/1`, а `finish_run` ссылается на реальный HTTP-обмен. Проверяются сетевые ограничения, время выполнения, размер и полнота ответа; запрещённые действия не отправляются. Полное приложение ещё не готово.

**На шаге S03** добавлен ограниченный parser OpenAPI 3.0/3.1 в JSON: каталог операций с параметрами, телом, ответами и security, локальные refs, очищенный снимок и хеши. Неподдерживаемые конструкции отклоняются явно. Источник по URL допускается только на явно заданном числовом loopback target, без redirects и загрузки внешних refs. HTTP-сценарий S02 по-прежнему ограничен своей операцией. Адаптивный цикл добавлен на шаге S04.

Просмотр контрольного контракта в PowerShell (Python 3.12+, без дополнительных зависимостей):

```powershell
$env:PYTHONPATH = 'src'
python -m api_agent.openapi tests/fixtures/profile_openapi.json
python scripts/check_stage.py S03 good
python scripts/check_stage.py S03 bad
```

CLI выводит каталог и хеши; ошибочный контракт возвращает diagnostic и exit 2. Для URL передаётся отдельный `--target http://127.0.0.1:<port>`. Профиль принимает простые типы, object/array, required/enum/nullable, скалярные path/query/header параметры, JSON body/responses и bearer/basic/header API key; YAML, servers, schema compositions и неизвестные ключи не поддерживаются. Good gate даёт exit 0, bad — exit 2 при доказанном отклонении; evidence сохраняется локально в `evidence/S03/`.


**На шаге S04** модель выбирает следующий инструмент по observation. Приложение ограничивает число шагов, запросов, время, повторы и исправления malformed calls; сохраняет trace и основание ожидания до HTTP. `StartAgentRun` создаёт один Run на сообщение и возвращает его при повторной доставке команды. Каталог S03 не расширяет сетевые права: исполняется только разрешённый GET /pets/1 на числовом loopback target; inspect доступен для той же операции. На момент S04 validator ещё отсутствовал; текущая проверка S05 описана ниже. Auth, policy modes, SQLite и Textual относятся к следующим срезам. После S04 требуется независимое ревью R3 в чистой сессии.

Запуск раннего прототипа (локальный llama-server должен быть запущен с проверенной моделью):

```powershell
$env:PYTHONPATH = 'src'
python -m demo_api.server --port 18082
# В другом терминале:
python -m api_agent.agent --target http://127.0.0.1:18082 --spec tests/fixtures/demo_openapi.json 'Check the test pet and report the actual observation.'
python scripts/check_stage.py S04 good
python scripts/check_stage.py S04 bad
```

CLI печатает Run ID, статус, stop reason и путь JSON trace. Изменение ответа стенда может изменить продолжение; отдельный gate доказывает это одной целью и двумя observations. Good требует живую модель и даёт exit 0; bad доказывает контролируемые отказы и даёт exit 2; ошибка подготовки даёт exit 1. Evidence по умолчанию: `evidence/S04/`; переопределение: `S04_EVIDENCE_DIR`. Дедупликация пока действует в одном экземпляре приложения, не между отдельными запусками CLI. CLI сохраняет trace в `evidence/agent/`; журнал не является SQLite-историей. Извлечение secrets/auth ещё не поддерживается; программный интерфейс умеет маскировать заранее настроенные значения. До S05 статус completed означал завершение сценария с полученным JSON. В текущем S05 он проверяет полноту поддержанных условий, как описано ниже.

После исправлений R3 недоверенный JSON проверяется до рекурсивного копирования и маскирования: глубина до 64, до 10000 узлов, HTTP body до 64 KiB и ответ модели до 1 MiB. Разрешены только JSON-типы и конечные числа; timing/usage принимают лишь известные числовые поля. Глубокий или некорректный HTTP JSON даёт `invalid_json` без сохранения raw body. Итоговый Run сохраняет фактические попытки и exchanges даже при неожиданной ошибке исполнения; повтор сообщения возвращает этот результат.

Диагностика модели различает `time_limit` общего Run, `model_timeout` адаптера, `model_transport_error`, `model_http_error`, `model_malformed_stream`, `model_response_limit`, `model_incomplete_stream` и `model_generation_limit`. Последний означает `finish_reason=length`: доступные finish reason и timing сохраняются, но неполный tool call не исполняется. Отмена имеет приоритет над ошибкой модели. Недоступный trace sink по-прежнему даёт `trace_error`; гарантировать запись на неисправное хранилище приложение не может. Исправления не означают принятие независимого R3.

Если настроенный секрет требует маскирования содержимого поддержанного контракта или идентичности его basis, Run получает `blocked/contract_redaction_conflict` до LLM и HTTP. В таком отказе `contract`, `spec_hash`, `operation_hash` равны null, expectation отсутствует: приложение не хранит секретное ограничение и не выдаёт маску за эквивалентный enum. При разрешённом запуске доступный snapshot и hashes остаются неизменными до запроса и во всех сохранениях. Аннотации, которые профиль S03 уже исключил до вычисления hashes, не блокируют запуск. Это консервативное ограничение раннего прототипа; response validation относится к S05.

## Проверки ответов и устойчивость (S05)

После каждого HTTP приложение проверяет полный ограниченный исходный ответ до маскирования: статус, Content-Type, JSON и профиль схемы S03. Отдельно сохраняются `contract_verdict`, `intent_verdict` и статус исполнения Run. Ожидаемый 404 может пройти негативное условие и одновременно нарушить объявленный контракт. Ошибка соединения, неполное тело или предел глубины приложения не подтверждают дефект API.

Ожидание, его источник и основание фиксируются до HTTP. `CONTRACT` требует содержательно подходящего pointer/hash; `USER_DEFINED` — явного условия исходного сообщения; `MODEL_HYPOTHESIS` остаётся непроверенным предположением. Модель не может назначить результат проверки своим summary. `finish_run` проверяет полноту результатов и собственные evidence. `completed` означает выполнение поддержанных проверок и сохранение итога, включая найденные нарушения, а не успех любой произвольной цели.

По умолчанию доступны короткие цели `Check pet`, `Check getPet`, `Проверь питомца`, `Проверь контракт getPet`, `Проверить контракт getPet`, `Check the test pet and report the actual observation.`. Неоднозначная или неподдержанная цель требует уточнения до запросов. Для явных условий CLI принимает `--conditions <local-json-file>`: список объектов с полями `predicate`, `parameters`, `intent` (`positive`/`negative`), `preconditions` (`verified`/`unknown`). Например:

```json
[
  {
    "predicate": "json_value_equals",
    "parameters": {"path": "/name", "value": "Murka"},
    "intent": "positive",
    "preconditions": "verified"
  }
]
```

Файл является явной командой пользователя; source/basis связывает приложение, модель не получает право их подменять. Без файла проверяется контракт getPet. Поддержанные predicates: `status_in` (`values`), `content_type_is_json` и `json_valid` (пустые parameters), `required_fields_present` (`fields`), `supported_schema_types` (`schema_pointer`), `json_value_equals` (`path`, `value`). Неизвестные условия блокируют HTTP. Свободный текст не превращается автоматически в машинный критерий.

```powershell
$env:PYTHONPATH = 'src'
python -m api_agent.agent --target http://127.0.0.1:18082 --spec tests/fixtures/demo_openapi.json 'Check pet'
python scripts/check_stage.py S05 good
python scripts/check_stage.py S05 bad
```

CLI сохраняет безопасный snapshot `.json` и append-only журнал `.jsonl`, показывает итог, отдельные результаты и пути ошибок. Gate сохраняет именованные части `a`/`b` в `evidence/S05/`; переопределение — `S05_EVIDENCE_DIR`. Good требует закреплённый локальный runtime и вес; bad не требует LLM и возвращает 2 при обнаружении всех намеренных нарушений, ошибка проверки — 1. Для изолированных tests tokenizer заменяется только явной test-инъекцией; production использует `/apply-template` и `/tokenize` того же загруженного GGUF.

Лимиты обычного режима S05: 8 шагов, один HTTP, 180 секунд, две пустые генерации подряд, одна коррекция malformed, три одинаковых действия, 12000 bytes payload, окно 2048 и общий бюджет 8192 токена. Перед generation резервируется настоящий templated input вместе с tools плюс максимум 384 output tokens; после сверяется actual usage. Unknown usage хранится явно, резерв не освобождается как нулевой расход. Предупреждения на 80% попадают в контекст; отчёт при hard stop формирует приложение без дополнительной генерации. Полная безопасная история и отдельное рабочее окно разделены; compaction не реализована.

Настроенные secrets маскируются в prompt, snapshot, журнале и отчёте. Отказ tokenizer/журнала/trace/validator прекращает новые действия. Частичный отчёт пригоден для передачи, но автоматическое восстановление, повтор изменяющих запросов и SQLite здесь не реализованы. Bearer-аутентификация и зависимые запросы — S06; полные policy modes — S07; UI — S12. Отдельный LLM-планировщик пока не добавлен.

## Испытательный Docker-стенд

[Запуск и тестирование стенда](docs/testing/test-stand.md): команды из проекта агента, good/bad, реальный прогон LLM и сбор evidence. Инструкция хранится локально вместе с docs/; при передаче проекта новой сессии передать её отдельно.


## S06: короткие зависимые цепочки

Локальный llama-server запускается на 127.0.0.1:18081 по runtime-lock (окно сервера8192). Run S06 сохраняет лимит контекста2048, max_tokens512 и enable_thinking=false; параметры обычного S05 сохраняются. Общий каталог S07 также использует резерв512.

```powershell
$env:PYTHONPATH = 'src;.'
python -m demo_api.server --port 18082
# В другом терминале из корня проекта:
python -m api_agent.agent 'Create and read order' --workflow create-read --target http://127.0.0.1:18082 --spec tests/fixtures/chain_openapi.json --evidence-dir evidence/manual-create-read
python -m api_agent.agent 'Login and read profile' --workflow auth-read --target http://127.0.0.1:18082 --spec tests/fixtures/auth_openapi.json --evidence-dir evidence/manual-auth-read
python scripts/check_stage.py S06 good # ожидается 0, включает живую модель
python scripts/check_stage.py S06 bad  # ожидается 2, доказанное отклонение
```

Auth Demo использует fixture/fixture-password. Для другого доверенного аккаунта передайте `--credentials-file config/credentials.json` с объектом `{"login":"...","password":"..."}`; файл храните вне Git. JWT передаёт приложение, модель видит только credential_ref. CLI печатает contract_verdict, intent_verdict, chain_verdict и пути trace/JSONL. Выполнение законченной цепочки не означает прохождение произвольной бизнес-цели: поддерживаются перечисленные короткие цели и их русские эквиваленты. Профиль и малый allowlist выбирает приложение; неизвестные цели требуют уточнения.

Gates поднимают изолированный Demo автоматически; `S06_EVIDENCE_DIR` переопределяет каталог evidence. Отдельные части: `python scripts/check_stage.py S06a good|bad` и `S06b good|bad`. Документация реализации: docs/s06-implementation.md (документ отсутствует; см. архив); запуск Docker-стенда, reset и ограничения: [docs/test-stand.md](docs/testing/test-stand.md). Полная корзина/заявка и общая policy относятся к последующим этапам.

## S07: права Project и общий каталог

Предварительно выполните preview, выберите конкретные operation IDs и назначьте риски A–E в локальном `config/selection.json`. Каталог сам по себе не выдаёт прав.

```powershell
$env:PYTHONPATH = 'src;.'
python -m api_agent.policy preview --spec tests/fixtures/chain_openapi.json --target http://127.0.0.1:18082
python -m api_agent.policy configure --spec tests/fixtures/chain_openapi.json --target http://127.0.0.1:18082 --mode SAFE --selection config/selection.json --output config/project.json --approve
python -m api_agent.agent 'Создай заявку A и проверь её чтение' --target http://127.0.0.1:18082 --spec tests/fixtures/chain_openapi.json --project-config config/project.json --goal-checks config/goal-checks.json --runtime-mappings config/runtime-mappings.json --interactive-approval
python scripts/check_stage.py S07 good # ожидается 0; настоящий llama-server
python scripts/check_stage.py S07 bad  # ожидается 2; доказанный отказ
```

`--goal-checks` содержит объект operation_id → список доверенных scalar predicates (`predicate`, `parameters`, `intent`, `preconditions`). `--runtime-mappings` содержит правила `extraction`, `binding_rules`, `auth`, `anonymous_operations`, `selection_scopes`; модель не создаёт эти правила. Неизвестные поля отклоняются до запуска. Для общего каталога credentials задаются в приватном `auth.body` файла `--runtime-mappings`; `--credentials-file` применяется к пилотному `--workflow auth-read`. Неподдержанные схемы/критерии отклоняются; общий исполнитель ещё не является автоматическим oracle для произвольной бизнес-цели.

Бюджеты каталога и цепочек по умолчанию: 10 HTTP-запросов (включая login), 24 шага основной модели и 180 секунд. Для каталога общий токенный бюджет 32768, окно одного вызова 8192. Пользователь задаёт пределы до Run:

```powershell
# Добавьте параметры к своей команде запуска с Project/spec/goal:
--max-requests 10 --max-steps 24 --max-seconds 300 --max-tokens-total 65536
```

Все пределы действуют одновременно; увеличение HTTP-бюджета не расширяет права Project и не разрешает повтор неопределённого POST. Допускается любой положительный целочисленный HTTP-бюджет, включая значения больше 10. Бюджеты сохраняются в Run; повтор прежнего message-id возвращает прежний результат и прежние пределы, для нового испытания нужен новый message-id. Ранняя калибровка без workflow сохраняет свой транспорт с одним запросом. Настройка будущего TUI относится к S12.

Область проверки GET задаётся до HTTP в `selection_scopes`, например `{"selection_scopes":{"listItems":{"mode":"exact","parameters":{"category":"active"}}}}` — только указанная выборка; `{"selection_scopes":{"listItems":{"mode":"unfiltered"}}}` — чтение без query. Здесь `listItems` и `category` — примеры: используются реальные operationId и параметры вашей спецификации. Обязательные path-параметры передаются в `parameters` в обоих режимах. Для `unfiltered` query-параметры недопустимы; типы значений и принадлежность операции условиям проверяются по каталогу. Без явной области условия привязываются к первому запросу и не доказывают глобальное свойство коллекции. Область сохраняется в истории Run; повторное чтение истории не вызывает API или модель.

SAFE требует подтверждения точного действия перед HTTP; READ_ONLY запрещает изменения; TEST разрешает предусмотренные policy изменения только на resettable target. Риски D/E требуют подтверждения. Approval привязан к Run/config/action hash; отмена, отказ, просрочка и изменение конфигурации прекращают действие. Исходные права Run не расширяются: новая операция требует нового Run. Неопределённый исход изменяющего запроса не повторяется автоматически.

Общий каталог использует окно 8192, резерв ответа 512 и прежний суммарный бюджет 8192 токенов. Сервер запускайте с `-c 8192` по `experiments/local_model/runtime-lock.json`; S05/S06 сохраняют свои меньшие Run limits. Размер окна не гарантирует поддержку любой длины задачи. В prompt входят ограниченные карточки; остальные разрешённые операции доступны через scoped search/inspect. Параметры и тело проверяются программой; защищённые headers задаёт приложение.

Архитектура и результаты: локальные `docs/reports/S07/design-and-interview.md`, `docs/reports/S07/implementation.md`, `PROGRESS.md`. Эти документы и evidence передаются отдельно от Git.


## S08: постоянная история

CLI сохраняет историю в SQLite: по умолчанию `<evidence-dir>/history.sqlite3`, либо `--history-db`. Для повторной доставки команды задайте прежние `--project-id`, `--session-id`, `--message-id`; она вернёт сохранённый Run без повторного HTTP. Новый message ID создаёт новый Run. `--parent-run-id` связывает явное продолжение с завершённым Run того же Project.

```powershell
$env:PYTHONPATH = 'src;.'
python -m api_agent.storage --db evidence/agent/history.sqlite3 --project cli-project --session cli-session
python -m api_agent.storage --db evidence/agent/history.sqlite3 --project cli-project --session cli-session --message <answer-message-id>
python scripts/check_stage.py S08 good
python scripts/check_stage.py S08 bad
```

Read-only CLI не открывает target/model, не создаёт отсутствующую базу и не восстанавливает активные запуски. Crash recovery выполняется под execution lease при принятии следующей команды: старый Run становится interrupted; автоматического replay нет. Повтор просмотра ответа возвращает его собственный contract/trace. SQLite не восстанавливает приватные credentials и не реализует память/compaction S13.

В общем каталоге ответ модели разделяет actual HTTP и machine checks. Большой JSON доступен ограниченной проекцией и `read_response` по exchange ID/JSON Pointer, без нового HTTP; неполнота явно обозначена. `json_collection_matches` проверяет содержимое коллекции по доверенным условиям: `path`, `rows` (JSON Pointer → scalar), `mode=contains|exact|absent`. HTTP200 не заменяет содержательную проверку. Модель не создаёт проверенное основание этих условий.

Локальные отчёты реализации, ограничений и экспериментов: [S08](docs/reports/S08/README.md). Текущий статус — [PROGRESS.md](PROGRESS.md); machine checks не заменяют независимый ACCEPT. Разделы S04–S07 выше описывают соответствующие исторические вертикали; ограничение dedup одним процессом больше не относится к SQLite CLI.

### Экспериментальный Qwen3-4B

Для сравнения STAND-37 загружен официальный `Qwen/Qwen3-4B-GGUF`, `Q4_K_M` (revision `bc640142c66e1fdd12af0bd68f40445458f3869b`). SHA256: `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5`. Веса находятся в `models/Qwen3-4B/` на диске D и не входят в Git. Основная конфигурация MiniCPM не заменена.

На GTX1060 3GB кандидат запущен с частичным GPU offload. После остановки текущего локального runtime команда из корня проекта:

```powershell
./experiments/local_model/runtime/cuda/llama-server.exe -m ./models/Qwen3-4B/Qwen3-4B-Q4_K_M.gguf --host 127.0.0.1 --port 18081 -c 8192 -ngl 24 -b 128 -ub 64 -t 4 -np 1 --jinja --no-webui -lv 4
```

Дождитесь `/health` со статусом `ok`. В команде агента задайте `--model-name Qwen3-4B-Q4_K_M`: это фиксирует имя в trace, сами веса выбираются при запуске llama-server. Для сравнения на изолированном стенде: `python scripts/evaluate_stand.py --output evidence/S08/qwen3-live --model-name Qwen3-4B-Q4_K_M`. Требуются локальные docs/evals и owner helper стенда; collector exit0 означает сбор данных, свободные объяснения оцениваются отдельно. Скорость и качество на произвольных целях этим не гарантируются.


Второй кандидат — `unsloth/Qwen3.5-4B-GGUF`, `IQ4_XS`, revision `e87f176479d0855a907a41277aca2f8ee7a09523`; SHA256 `658a9e7e406deb06d0179755e3c14f6a82915a4be4962a2f92a64d948d2e572f`. Для него в команде runtime замените путь на `./models/Qwen3.5-4B/Qwen3.5-4B-IQ4_XS.gguf`, `-ngl 24` на `-ngl 99`, а имя в CLI на `Qwen3.5-4B-IQ4_XS`. Проверено 33/33 GPU layers при context8192 на этом GTX1060. Объяснения STAND-37 согласованы в шести прогонах, однако дополнительный библиотечный сценарий не пройден; кандидат не выбран основным автоматически. Профиль и подробные результаты локальные: `experiments/local_model/qwen35-4b-lock.json`, `docs/reports/S08/model-comparison-2026-10-15.md`.

## S09a: проверяемые факты Project

При SQLite-финализации приложение автоматически сохраняет поддержанные проверенные факты: `declared_contract`, `response_observation`, `create_read_link`. Proposal содержит тип, источник, operation ID и закрытый verify descriptor; обмены и принятые условия читаются из собственной закреплённой истории. Модель не передаёт готовый VERIFIED или произвольный текст памяти. Ошибочное содержание, чужой Run/Project, неполный или маскированный результат не подтверждают факт. Источники INFERRED/USER_PROVIDED остаются CANDIDATE.

Application API: `SQLiteHistory.propose_fact(project, session, run_id, descriptor)` и `read_facts(project, catalog=...)`. Чтение не вызывает HTTP/LLM и доступно любой сессии того же Project. По умолчанию возвращаются только актуальные VERIFIED; `include_inactive=True` показывает также кандидатов, конфликты, STALE и INVALIDATED. Исходное значение и evidence сохраняются при явном `invalidate_fact`, который требует сопоставимого опровергающего HTTP.

```python
from api_agent.openapi import load_source
from api_agent.storage import SQLiteHistory

catalog = load_source("tests/fixtures/demo_openapi.json")
with SQLiteHistory("history.sqlite3", read_only=True) as history:
    facts = history.read_facts("my-project", catalog=catalog, operation_ids=["getPet"])
```

Наблюдения и связи имеют настраиваемый TTL по типу внутри Project/среды: начальный профиль — 3600 секунд для обоих типов. `configure_fact_policy(project, response_observation=..., create_read_link=...)` изменяет профиль; увеличение TTL не продлевает старые наблюдения. Срок отсчитывается консервативно от начала исходного Run. После настоящего сброса стенда приложение вызывает `advance_fact_epoch(project)`; неизвестный внешний reset автоматически не обнаруживается. Декларации зависят от operation hash. Старые версии схемы SQLite мигрируются при записи; read-only история v1 читается без миграции и возвращает пустой список фактов.

Минимальный create/read validator поддерживает явное публичное extraction/binding, один path ID и поля create body на тех же путях корневого read object. Он проверяет фактический URL, request parameter, response ID, содержимое и schemas двух exchanges одного Run. Числовой ID не записывается как долговременное значение связи. Неподдержанные формы не получают VERIFIED. Общий поиск исторических сущностей, передача фактов модели и полный сценарий STAND-34 относятся к последующим срезам S09; S09a предоставляет проверяемый application/storage API.

Проверки: `python scripts/check_stage.py S09a good` (0), `python scripts/check_stage.py S09a bad` (2 при обнаруженных нарушениях; подготовка/сбой проверки — 1). Evidence сохраняется в `evidence/S09/`; технические проверки используют controlled model и настоящий Demo HTTP/SQLite, не являются полной серией STAND-37.

## S09b: цель, принятые условия и вопросы

Режим `--generate-goal` принимает русскую цель без ручного `--goal-checks`. Приложение ограниченно изучает метаданные разрешённых операций, один раз вызывает generator и проверяет предложение compiler. До HTTP пользователь принимает интерпретацию с условиями и обязательными полями ответа. Автоматически принимается только независимо проверяемая форма `Проверь <operationId>: статус <код>.` для одного анонимного GET без параметров. `--confirm-goal` требует принятия и для этой формы.

```powershell
$env:PYTHONPATH = 'src;.'
python -m api_agent.agent 'Покажи имя питомца и проверь доступность сведений.' --generate-goal --target http://127.0.0.1:18082 --spec tests/fixtures/demo_openapi.json --project-config config/project.json --history-db evidence/agent/history.sqlite3 --session-id goal-session --message-id goal-001
python scripts/check_stage.py S09b good
python scripts/check_stage.py S09b bad
```

Project config предварительно создаётся и одобряется через `api_agent.policy configure` для того же target/spec, как выше. Новый режим использует общие бюджеты каталога; generator/metadata и основная модель расходуют один токенный бюджет и активное время. `check_goal` читает собственные exchanges без сети и возвращает checklist; финальный verdict вычисляется приложением даже без вызова инструмента.

На ответ человеку отведено 120 секунд (`--human-timeout`), ожидание учитывается отдельно. Лимиты вопросов — три до Run и три во время Run, подтверждения действий отдельно. Во время Run можно заполнить только заранее принятый typed input до связанного HTTP. Изменение смысла/ожиданий после HTTP требует нового message ID и `--parent-run-id`; исходный manifest сохраняется. Повтор того же message ID возвращает результат без повторного generator/HTTP, включая отклонённую подготовку. После unresolved/rejection нужна новая команда, автоматического второго generator нет.

Приватные поля приложения задаются через `--runtime-mappings`: например, `{"private_bodies":{"register":{"password":{"private":"test-password"}}}}`. Здесь `register` — реальный operationId проекта; одна метка повторно использует введённое приватное значение, в том числе в `auth.body`. CLI запрашивает его через скрытый ввод. Bearer и control keys не передаются модели; приватные значения маскируются в SQLite, trace и публичных событиях. Разрешены ключи mappings `auth`, `extraction`, `binding_rules`, `private_bodies`; права задаёт только одобренный Project config.

Закрытые проверки: статус, scalar equality, schema, коллекция в явно указанной области одного ответа, equality между связанными occurrences, числовой результат с округлением Decimal ROUND_HALF_UP до точности пользователя. Ожидаемый результат расчёта задаёт пользователь; формула не угадывается. Произвольный NL, вся пагинация и полный STAND-37 этим не гарантируются. Проверка ответа S09c описана ниже; кодовый workspace, compaction и полное сравнение — следующие срезы.

## S09c: проверка и коррекция ответа

В `--generate-goal` приложение сопоставляет структурированные утверждения `finish_run.claims` и `answer_fields` с принятым checklist. Ложный вердикт, пропущенное условие или неверное поле ответа дают программный feedback с expected/actual, наличием значения, source/basis и evidence IDs. При таком сигнале доступна одна коррекция полей основной моделью; новых HTTP-запросов, generator или цикла исполнения нет. Правильные структурированные утверждения не вызывают дополнительных обращений к модели.

Диагност по умолчанию выключен. Для одного дополнительного advisory вызова перед коррекцией добавьте `--answer-diagnostic` к команде `--generate-goal`. Его ответ проверяется по схеме и ссылкам; он не меняет условия, права или verdict. Старый экспериментальный `--review-summary` используется отдельно от этого режима. Все роли расходуют общий token ledger и оставшееся активное время Run. При отмене, нехватке ресурсов или непроверенной коррекции используется канонический отчёт, повторов auxiliary нет.

`report.answer` содержит проверенные факты и незаполненные поля; `report.answer_review` — статус диагностики/коррекции. Свободная проза модели не считается программно доказанной: она сохраняется как draft, пользовательский ответ строится из проверенных полей. Исходный FAIL и все доступные drafts остаются в истории, повтор message ID не запускает модель или HTTP. Большие значения помечаются как omitted с размером/hash, а не выдаются за полное evidence.

В малой парной серии на закреплённых HTTP-свидетельствах локальная LLM исправила 3/10 проблемных drafts без диагноста и 4/10 с ним, при нуле вызовов для 5 корректных drafts в каждом варианте. Время — 18,109/45,142 s, токены — 6774/16294; непроверенные коррекции заменены каноническим ответом. Диагност не выполнил заранее заданное условие роста времени ≤2× и остаётся опцией. Это answer-only replay, source primary/generator controlled, не полный STAND-37 и не оценка понимания произвольной прозы.

Проверки: `python scripts/check_stage.py S09c good|bad` (0/2; сбой подготовки или проверки —1). Методика сравнения: сначала `python scripts/evaluate_answer_feedback.py --phase pilot --output <новый каталог>`, затем зафиксировать thresholds JSON и выполнить `--phase series --thresholds <файл> --output <новый каталог>`. Нужен прежний локальный llama-server18081; модель не скачивается. Кодовый workspace, compaction и полное сравнение остаются следующими срезами.

## S09d1: подготовка SDK для Project/spec

Детерминированный builder подготавливает SDK без цели, LLM и API HTTP. Укажите прежнюю одобренную конфигурацию с совпадающим spec:

```powershell
$env:PYTHONPATH = 'src;.'
python -m api_agent.workspace_sdk --spec tests/fixtures/chain_openapi.json --project-config config/project.json --output evidence/sdk
python scripts/check_stage.py S09d1 good
python scripts/check_stage.py S09d1 bad
```

В `<output>/<artifact_hash>/` сохраняются `manifest.json` и `sdk.py`. Повтор той же сборки использует прежний артефакт, другая версия остаётся в отдельном каталоге. Manifest содержит spec/config/operation hashes, версии builder/profile/metadata, mapping имён и исключения неразрешённых операций. `SdkArtifact.load(directory, expected_hash=...)` проверяет закреплённый приложением hash и оба файла. Изменённый контракт требует пересмотра Project config; правка description меняет metadata/artifact hash, сохраняя execution hash.

Функции имеют настоящие keyword-only сигнатуры: например, `api.readOrder(path__order_id=7)`. Path/query/header разделены префиксами, JSON body — аргумент `body`, ответы — обычные значения dispatcher. Необычные имена и коллизии получают стабильные aliases с обратным mapping. SDK не коэрцирует типы, не создаёт credentials и не делает HTTP самостоятельно: входы и права проверяет внешний broker.

В facade есть локальные `list_operations`, `search`, `describe` и сообщения приложения `ask_user(question, input_slot=...)`, `check_goal()`, `propose_fact(descriptor)`, `finish(summary, claims=..., answer_fields=...)`. Они не присваивают VERIFIED/PASS/approval. Discovery всех разрешённых операций доступен без RPC; большие схемы читаются секциями/JSON Pointer с явными omissions. Описания, включая refs/fields/parameters, хранятся как недоверенные metadata отдельно от validation basis. Byte limits metadata не являются измерением токенов.

S09d1 проверяет builder и протокол facade через controlled dispatcher. Sandbox, broker, пользовательский `execute_code`, Run namespace и live code-agent pilot относятся к S09d2–3; SDK не запускает код модели в процессе приложения. Весь S09d этим не закрыт.

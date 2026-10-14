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

`--goal-checks` содержит объект operation_id → список доверенных scalar predicates (`predicate`, `parameters`, `intent`, `preconditions`). `--runtime-mappings` содержит правила `extraction`, `binding_rules`, `auth`, `anonymous_operations`; модель не создаёт эти правила. Для общего каталога credentials задаются в приватном `auth.body` файла `--runtime-mappings`; `--credentials-file` применяется к пилотному `--workflow auth-read`. Неподдержанные схемы/критерии отклоняются; общий исполнитель ещё не является автоматическим oracle для произвольной бизнес-цели.

SAFE требует подтверждения точного действия перед HTTP; READ_ONLY запрещает изменения; TEST разрешает предусмотренные policy изменения только на resettable target. Риски D/E требуют подтверждения. Approval привязан к Run/config/action hash; отмена, отказ, просрочка и изменение конфигурации прекращают действие. Исходные права Run не расширяются: новая операция требует нового Run. Неопределённый исход изменяющего запроса не повторяется автоматически.

Общий каталог использует окно 8192, резерв ответа 512 и прежний суммарный бюджет 8192 токенов. Сервер запускайте с `-c 8192` по `experiments/local_model/runtime-lock.json`; S05/S06 сохраняют свои меньшие Run limits. Размер окна не гарантирует поддержку любой длины задачи. В prompt входят ограниченные карточки; остальные разрешённые операции доступны через scoped search/inspect. Параметры и тело проверяются программой; защищённые headers задаёт приложение.

Архитектура и результаты: локальные `docs/reports/S07/design-and-interview.md`, `docs/reports/S07/implementation.md`, `PROGRESS.md`. Эти документы и evidence передаются отдельно от Git.

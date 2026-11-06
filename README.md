# API Testing Agent

**ВКР:** «Разработка интеллектуальной агентной системы функционального тестирования REST API по спецификации OpenAPI с использованием локальных языковых моделей».
Автор: Прокофьев Илья Викторович, РТ5-81Б.

Приложение исполняет пользовательскую цель для одного разрешённого REST/JSON API по OpenAPI. Локальная LLM предлагает Python-блоки; изолированный Docker worker вызывает SDK, а host broker проверяет права, выполняет HTTP и сохраняет evidence. Проверки ответов и итоговый отчёт формирует программа.

Начало работы: [PROGRESS](PROGRESS.md) → [план](PLAN.md) → [карта документации](docs/README.md). Разработка: [AGENTS](AGENTS.md), [workflow](docs/engineering/development-workflow.md), [карта кода](docs/engineering/source-map.md). Документация/tests/experiments/evidence локальные и исключены Git: для разработки и воспроизведения прежних проверок они передаются отдельными bundles. Пользовательский запуск готовится из исходников репозитория.

## Установка и запуск

Проверяемый профиль: Windows x64, Python3.11/3.12, Git, терминал с ANSI/VT и Unicode, заранее установленный Docker с работающим Linux-container engine. Docker приложение не устанавливает. Для модели нужны уже имеющиеся GGUF + совместимый llama-server либо готовый локальный llama-server с tokenizer/template endpoints. Тестируемый сервис пользователь запускает любым способом; дополнительные health/control/reset endpoints ему не требуются.

```powershell
git clone https://github.com/ProkIlya/RT5-81-Prokofev-Ilya-VKR.git
cd RT5-81-Prokofev-Ilya-VKR
python scripts/bootstrap.py --prepare
.\.venv\Scripts\python.exe main.py --tui --state .api-agent/ui.sqlite3
```

Подготовка создаёт отдельную `.venv`, устанавливает закреплённые зависимости и собирает worker из зафиксированного публичного образа Python. Каждая сборка сохраняется под отдельным тегом; immutable image ID записывается в локальные настройки новых проектов. Прежние проекты/Run не переключаются на новую сборку. Первый запуск требует доступа к GitHub, Python-пакетам и Docker registry; модель не скачивается автоматически.

В TUI выберите существующий GGUF/runtime или локальный сервер, затем добавьте проект: адрес API и локальный OpenAPI3 JSON. «Проверить» показывает поддержку/качество спецификации и права; проверка соединения выполняется только по выбранному публичному разрешённому GET. Недоступность API не запрещает сохранение проекта/запуск цели: фактический сетевой отказ показывается при исполнении. «Настроить вход» позволяет выбрать POST-операцию и поле токена, ввести поля по схеме; credential сохраняется через Windows DPAPI с привязкой к пользователю/Project/target/spec. Вход при настройке автоматически не выполняется. Файловый mappings import остаётся для совместимости и заменяет прежний вход. Новый вход через форму отключает импорт; «Без настроенного входа» отключает оба источника, сохраняя файлы на диске.

Полная диагностика: «Настройки приложения → Проверить окружение» либо `.\.venv\Scripts\python.exe scripts/bootstrap.py --doctor --model-id <ID>` с тем же `--state`, если он отличается от `.api-agent/ui.sqlite3`. Без выбранной/доступной модели, поддержанного окна, исправных миграций и worker полный Ready не выдаётся. SQL-миграции проверяются на временной копии; исходная история не переписывается.

F2 открывает навигацию, F3 — инспектор, F5 отправляет цель, Ctrl+Q сохраняет доступный результат и завершает собственный runtime. Изменения API и права задаются в проекте, права активного Run не расширяются. Отдельный графический установщик и каталог скачивания моделей не требуются. Установка кандидата из отдельного checkout проверяется в S14; это не утверждение, что неопубликованные изменения уже доступны в remote.

## Дополнительные CLI-входы

```powershell
$env:PYTHONPATH = 'src'
python main.py --help
# Совместимый модульный вход:
python -m api_agent.agent --help
python -m api_agent.policy --help
python -m api_agent.workspace.sdk --help
python -m api_agent.storage --help
```

Терминальный интерфейс S12 (рабочая реализация):

```powershell
python -m pip install -r requirements.txt
python main.py --tui --state config/desktop/ui.sqlite3
```

Нужен терминал с ANSI/VT и Unicode (например Windows Terminal или WezTerm). Обычный вывод с перенаправлением в файл не заменяет интерактивный терминал. Setup предлагает модель/Project; модель задаётся локальным GGUF + поддержанным llama-server либо адресом уже работающего локального сервера. Project использует адрес API, OpenAPI JSON, проверенные права и подготовленный bootstrap worker; поддержанное подключение настраивается формой, прежний mappings import сохранён. F2 открывает навигацию, F3 переключает инспектор, F5 отправляет цель, Ctrl+Q сохраняет результат и завершает собственный runtime. Точные границы реализации/проверок — в PROGRESS.

Ручная цель задаётся через `--manual-config goal.json`. Нужны `--target`, `--spec`, `--project-config`, `--code-image` с immutable sha256 и `--history-db`. SDK можно построить до запуска и передать через `--code-sdk`/`--code-sdk-hash`. Сначала те же аргументы с `--preview-only` показывают нормализованный снимок и `preview_hash`; запуск требует явного принятия или `--accept-preview HASH` именно этого снимка. Wire и формы полей: [контракт ручного входа](docs/engineering/goal-verification.md#9-wire-ручного-входа-s10).

Research использует `cards: []` и N/A; Testing требует ≥1 включённой карточки. PASS выбранных проверок не завершает свободную цель автоматически. Прикреплённые публичные поля прошлого ответа и пояснения к ним добавляются в контекст только при наличии attachments. Лимиты по умолчанию:20 HTTP,24 шага,180s active,65536 total tokens, context8192/output1024; до8 последовательных блоков без HTTP/finish.

## История и повторное использование

История читается без модели и API: `python -m api_agent.storage --db history.sqlite3 --view projects`. Для Project/Session доступны `sessions`, `messages`; для Run — `report`, `exchanges`. `--view section --run R --exchange E --section response --pointer /json` открывает сохранённое тело. `--view selection --run R --exchange E --field /id --field /value` возвращает locator для `attachments` новой цели. `--pin --run R --label TEXT` создаёт закладку отчёта. Старые условия/credentials/approvals при этом не переносятся. Подробности: [история](docs/project/project-knowledge.md).

Для прерванной задачи предусмотрены `--continue-run RUN_ID --origin-session SESSION` вместо `--manual-config`. Сначала `--preview-only` показывает восстановленные публичные данные, источники и остатки общих бюджетов; запуск принимает тот же preview hash. Цель/условия/лимиты не редактируются, Python-процесс создаётся заново. При необходимости в trusted `--runtime-mappings` можно задать `"health_check":{"path":"/health","expected_json":{"ready":true}}`: тогда перед новым Run приложение делает одну проверку того же target. Этот endpoint и mapping необязательны. Результат проверки соединения сохраняется как информационная подготовка; её отказ не блокирует продолжение. Фактический сетевой отказ показывается обычным Run. Uncertain write и защита выполненных записей сохраняются. Точный [CLI-контракт S11](docs/engineering/goal-verification.md#11-продолжение-прерванного-run-s11).

## Проверки и структура

`main.py` в корне запускает `src/api_agent/main.py`; основной цикл — `src/api_agent/agent/runtime.py` (`run_workspace` — вход, `_drive` — цикл ходов). `goals/entry` содержит ручной вход и interaction, `goals/checks` — текущие проверки, `goals/legacy` — pure совместимость старых контрактов. `workspace/context` формирует контекст, `workspace/execution` содержит broker/изоляцию/worker; публичный SDK остаётся в `workspace/sdk.py`. Общий host kernel — `execution`, данные SQL — `storage/sql`. `openapi`, `policy`, `transport`, `validation`, `storage` сохраняют свои границы. Малые прежние chains/catalog/state пакеты объединены по ответственности в execution. Ограничение Python-модуля —500 физических строк.

```powershell
$env:PYTHONPATH = 'src;tests;.'
$env:WORKSPACE_DOCKER_TESTS = '1'
$env:WORKSPACE_IMAGE = '<проверенный sha256 image>'
$env:WORKSPACE_TOKENIZER_URL = 'http://127.0.0.1:18081'
python scripts/check_manual_code.py
python scripts/evaluate_manual_entry.py evidence/S10/<новая-папка-live>
$env:S10_LIVE_EVIDENCE = 'evidence/S10/<новая-папка-live>/summary.json'
python scripts/check_stage.py S10 good
python scripts/check_stage.py S10 bad
python scripts/check_stage.py S11 good
python scripts/check_stage.py S11 bad
```

Полный текущий discover требует реальный Docker и tokenizer; skips не принимаются. Good=0, negative controls=2, preparation error=3; S10 good требует live evidence той же source identity. `evaluate_selected_stand.py <новая-папка>` проверяет два связанных Run на предметном стенде без reset; `evaluate_continuation.py <новая-папка>` — два linked continuation с настоящей локальной моделью. Текущие scripts и профили — в [карте кода](docs/engineering/source-map.md).

Исторические реализации/команды S01–S09 и исследовательские прототипы сохранены в [архивах](docs/archive/README.md). Они исполняются со своей версией source/tests. Текущая очистка и её проверки описаны в [отчёте](docs/reports/S10/runtime-cleanup-2026-10-26.md); статусы и ограничения — только в PROGRESS. TUI относится к S12; диагностическая STAND40×3 выполняется в S14, отдельная итоговая серия после доводки — в S15. Technical passing не заменяет независимый ACCEPT.

"""Транзакционные связи и неизменное основание исторических результатов.

Входные DTO уже маскированы приложением. Authority IDs используются только
как хешированные ключи: public redaction не определяет принадлежность команды.
Эксклюзивная OS lease отличает аварийно оставленный Run от живого исполнителя.
"""

from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from ..bounded_json import validate_tree
from ..first_http import loopback_origin
from ..validation import report


class StorageError(ValueError):
    """Безопасная причина отказа: SQL/пути/исходные значения не публикуются."""


def _key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def _json(value):
    validate_tree(value, max_depth=96, max_nodes=100000, max_bytes=4194304)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class SQLiteHistory:
    """Одна база для Projects; чтение истории не требует сетевых адаптеров."""

    def __init__(self, path, *, read_only=False):
        self.path = Path(path).resolve()
        self.read_only = read_only
        if read_only and not self.path.is_file():
            raise StorageError("history_not_found")
        if not read_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.db = sqlite3.connect(self.path.as_uri() + "?mode=ro" if read_only else self.path,
                                  uri=read_only, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=2000")
        try:
            self._migrate()
        except Exception:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        """Связи фиксируются вместе; отказ до commit не оставляет Message без Run."""
        with self._lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                yield
                self.db.execute("COMMIT")
            except Exception as error:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                if isinstance(error, sqlite3.Error):
                    raise StorageError("storage_write_failed") from None
                raise

    def _migrate(self):
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version > 3:
            raise StorageError("unsupported_schema_version")
        if version == 3 or (version in (1, 2) and self.read_only):
            return
        if version == 2:
            script = Path(__file__).with_name('preparations.sql').read_text(encoding='utf-8')
            try:
                self.db.executescript('BEGIN IMMEDIATE;\n' + script + '\nPRAGMA user_version=3;\nCOMMIT;')
            except sqlite3.Error:
                if self.db.in_transaction: self.db.execute('ROLLBACK')
                raise StorageError('migration_failed') from None
            return
        if version == 1:
            script = Path(__file__).with_name('facts.sql').read_text(encoding='utf-8')
            try:
                self.db.executescript('BEGIN IMMEDIATE;\n' + script + '\nPRAGMA user_version=2;\nCOMMIT;')
            except sqlite3.Error:
                if self.db.in_transaction:
                    self.db.execute('ROLLBACK')
                raise StorageError('migration_failed') from None
            self._migrate()
            return
        if self.read_only:
            raise StorageError("unsupported_schema_version")
        tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        if tables:
            backup = self.path.with_name(self.path.name + ".before-migration-" + uuid.uuid4().hex)
            with closing(sqlite3.connect(backup)) as destination:
                self.db.backup(destination)
            raise StorageError("unrecognized_schema_backup_saved")
        script = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
        try:
            self.db.executescript("BEGIN IMMEDIATE;\n" + script + "\nPRAGMA user_version=1;\nCOMMIT;")
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise StorageError("migration_failed") from None
        self._migrate()

    @contextmanager
    def execution(self):
        """Один процесс исполняет базу; lock автоматически отпускается при crash."""
        if self.read_only:
            raise StorageError("read_only_history")
        lock_path = self.path.with_name(self.path.name + ".lock")
        lock_path.touch(exist_ok=True)
        with self._lock, lock_path.open("r+b") as file:
            if lock_path.stat().st_size == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise StorageError("history_execution_busy") from None
            try:
                self.recover_unfinished()
                yield
            finally:
                file.seek(0)
                if os.name == "nt":
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(file.fileno(), fcntl.LOCK_UN)

    def _scope(self, project, session):
        return _key(project), _key(project, session)

    def goal_preparation(self, project, session, message, goal_hash):
        row = self.db.execute('SELECT * FROM goal_preparations WHERE message_key=?', (_key(message),)).fetchone()
        if row is None: return None
        if (row['project_key'], row['session_key']) != self._scope(project, session):
            raise ValueError('goal_preparation_scope_mismatch')
        if row['goal_hash'] != goal_hash: raise ValueError('goal_message_changed')
        return json.loads(row['payload'])

    def begin_goal_preparation(self, project, session, message, payload):
        """Ровно один caller получает право на первую генерацию команды."""
        with self.transaction():
            previous = self.goal_preparation(project, session, message, payload['goal_hash'])
            if previous is not None: return previous
            self.db.execute('INSERT INTO goal_preparations VALUES(?,?,?,?,?)',
                (_key(message), *self._scope(project, session), payload['goal_hash'], _json(payload)))
        return None

    def goal_parent(self, project, run_id):
        row = self.db.execute('SELECT payload FROM runs WHERE run_id=? AND project_key=? AND lifecycle=?',
                              (run_id, _key(project), 'finalized')).fetchone()
        if row is None: raise ValueError('goal_foreign_or_unfinished_parent')
        saved = json.loads(row['payload'])
        return self.read_run(project, saved['session_id'], run_id)

    def goal_project_metadata(self, config, metadata):
        """Project mappings остаются pinned; goal/occurrences принадлежат Run."""
        row = self.db.execute('SELECT payload FROM snapshots WHERE project_key=? AND config_version=?',
                              (_key(config.snapshot['project_id']), config.snapshot['version'])).fetchone()
        if row is None: return metadata
        saved = json.loads(row['payload'])
        if saved['configuration'] != config.snapshot: raise ValueError('goal_project_config_changed')
        for name in ('extraction', 'binding_rules', 'auth'):
            if saved['metadata'].get(name) != metadata.get(name): raise ValueError('goal_project_mappings_changed')
        # Старый снимок не переписывается. Текущие Run-specific descriptors
        # доступны в его admission_manifest, а не под видом Project config.
        return saved['metadata']

    def save_goal_preparation(self, project, session, message, payload):
        """Application checkpoint до LLM и после принятия; terminal immutable."""
        with self.transaction():
            old = self.goal_preparation(project, session, message, payload['goal_hash'])
            if old and old['status'] in {'completed', 'blocked'} and _json(old) != _json(payload):
                raise ValueError('goal_preparation_finalized')
            self.db.execute('INSERT INTO goal_preparations VALUES(?,?,?,?,?) ON CONFLICT(message_key) DO UPDATE SET payload=excluded.payload',
                (_key(message), *self._scope(project, session), payload['goal_hash'], _json(payload)))

    def lookup(self, project, session, message):
        row = self.db.execute("SELECT * FROM messages WHERE message_key=?", (_key(message),)).fetchone()
        if row is None:
            return None
        if (row["project_key"], row["session_key"]) != self._scope(project, session):
            raise ValueError("message_scope_mismatch")
        run = self.read_run(project, session, row["run_id"])
        if row["role"] == "assistant":
            if run.get("response_message_id") != row["public_id"]:
                raise ValueError("answer_run_binding_mismatch")
        else:
            original = self.db.execute("SELECT original_message_key FROM runs WHERE run_id=?", (row["run_id"],)).fetchone()
            if original["original_message_key"] != row["message_key"]:
                raise ValueError("original_run_binding_mismatch")
        return run

    def begin_run(self, project, session, message, run, *, environment, configuration, metadata):
        """Message, снимок конфигурации и Run атомарны до любого HTTP."""
        project_key, session_key = self._scope(project, session)
        target, _ = loopback_origin(run["target"])
        if not isinstance(environment, str) or not environment or len(environment) > 200:
            raise ValueError("invalid_environment")
        snapshot = {"contract": run["contract"], "configuration": configuration, "metadata": metadata}
        version = configuration.get("version", 1)
        if type(version) is not int or version < 1:
            raise ValueError("invalid_config_version")
        with self.transaction():
            previous = self.lookup(project, session, message)
            if previous is not None:
                return previous
            stored = self.db.execute("SELECT * FROM projects WHERE project_key=?", (project_key,)).fetchone()
            if stored and (stored["target"] != target or stored["environment"] != environment):
                raise ValueError("immutable_project_environment")
            self.db.execute("INSERT OR IGNORE INTO projects VALUES(?,?,?)", (project_key, target, environment))
            self.db.execute('INSERT OR IGNORE INTO fact_projects(project_key) VALUES(?)', (project_key,))
            if run.get("parent_run_id"):
                parent = self.db.execute("SELECT project_key, lifecycle FROM runs WHERE run_id=?", (run["parent_run_id"],)).fetchone()
                if parent is None or parent["project_key"] != project_key or parent["lifecycle"] != "finalized":
                    raise ValueError("foreign_or_unfinished_parent")
            existing = self.db.execute("SELECT * FROM snapshots WHERE project_key=? AND config_version=?", (project_key, version)).fetchone()
            encoded = _json(snapshot)
            if existing and existing["payload"] != encoded:
                raise ValueError("stale_or_modified_configuration")
            latest = self.db.execute("SELECT max(config_version) FROM snapshots WHERE project_key=?", (project_key,)).fetchone()[0]
            if latest is not None and version < latest:
                raise ValueError("stale_project_configuration")
            snapshot_id = existing["snapshot_id"] if existing else str(uuid.uuid4())
            if not existing:
                self.db.execute("INSERT INTO snapshots VALUES(?,?,?,?,?)", (snapshot_id, project_key, version, run["spec_hash"], encoded))
            self.db.execute("INSERT OR IGNORE INTO sessions VALUES(?,?,?)", (session_key, project_key, run["session_id"]))
            self.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)", (_key(message), project_key, session_key, run["message_id"], "user", run["goal"], None))
            self.db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?,?)", (run["run_id"], project_key, session_key, _key(message), snapshot_id, "active", _json(run), run.get("parent_run_id")))
            self.db.execute('INSERT INTO fact_runs SELECT ?,reset_epoch,? FROM fact_projects WHERE project_key=?',
                            (run['run_id'], time.time(), project_key))
            self.db.execute("UPDATE messages SET run_id=? WHERE message_key=?", (run["run_id"], _key(message)))
        return None

    def save_snapshot(self, run):
        """Снимок содержит только безопасные DTO; authority scope из базы неизменен."""
        with self.transaction():
            row = self.db.execute("SELECT lifecycle,payload FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()
            if row is None:
                raise ValueError("unknown_run")
            previous = json.loads(row["payload"])
            for name in ("project_id", "session_id", "message_id", "target", "contract", "spec_hash", "goal", "goal_hash", "parent_run_id", "admission_manifest"):
                if run.get(name) != previous.get(name):
                    raise ValueError("run_identity_changed")
            if row["lifecycle"] == "finalized" and _json(run) != row["payload"]:
                raise ValueError("finalized_run_immutable")
            self.db.execute("UPDATE runs SET payload=? WHERE run_id=?", (_json(run), run["run_id"]))

    def unresolved_mutations(self, project, *, exclude_run_id):
        """Durable запрет Project не снимается новым Message/Session/parent.

        История эффекта является authority приложения, а не памятью модели.
        До отдельного проверенного resolution используется консервативный
        запрет всех новых мутаций Project; чтения и другие Projects доступны.
        """
        unresolved = []
        for row in self.db.execute('SELECT run_id,payload FROM runs WHERE project_key=? AND run_id<>?',
                                   (_key(project), exclude_run_id)):
            saved = json.loads(row['payload'])
            if saved.get('run_id') != row['run_id']:
                raise StorageError('run_snapshot_binding_mismatch')
            if saved.get('uncertain_write') or saved.get('pending_mutation'):
                unresolved.append(row['run_id'])
        return unresolved

    def append_event(self, event):
        with self.transaction():
            value = _json(event)
            old = self.db.execute("SELECT payload FROM events WHERE run_id=? AND sequence=?", (event["run_id"], event["sequence"])).fetchone()
            if old and old["payload"] != value:
                raise ValueError("event_changed")
            self.db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?)", (event["run_id"], event["sequence"], value))

    def _finalize(self, row, run):
        """Итог, assistant Message и эпизод фиксируются одной транзакцией."""
        if row["lifecycle"] == "finalized":
            if _json(run) != row["payload"]:
                raise ValueError("finalized_run_immutable")
            return
        answer_id = run["response_message_id"]
        self.db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?)", (_key(answer_id), row["project_key"], row["session_key"], answer_id, "assistant", run["report"]["summary"], run["run_id"]))
        episode = {
            "outcome": run["status"], "stop_reason": run["stop_reason"],
            "summary": run["report"]["summary"], "spec_hash": run["spec_hash"],
            "model": run["model"], "snapshot_id": row["snapshot_id"],
            "evidence_ids": run["report"]["evidence_ids"], "lessons": None,
        }
        self.db.execute("INSERT INTO episodes VALUES(?,?)", (run["run_id"], _json(episode)))
        self.db.execute("UPDATE runs SET payload=?, lifecycle='finalized' WHERE run_id=?", (_json(run), run["run_id"]))

    def finalize(self, project, session, message, run):
        with self.transaction():
            row = self.db.execute("SELECT * FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()
            if row is None or (row["project_key"], row["session_key"]) != self._scope(project, session) or row["original_message_key"] != _key(message):
                raise ValueError("finalization_scope_mismatch")
            previous = json.loads(row["payload"])
            for name in ("project_id", "session_id", "message_id", "goal", "target", "spec_hash", "contract", "parent_run_id", "admission_manifest"):
                if previous.get(name) != run.get(name):
                    raise ValueError("run_identity_changed")
            self._finalize(row, run)
            self._collect_facts(project, session, run['run_id'])

    def _fact_policy(self, project):
        row = self.db.execute('SELECT * FROM fact_projects WHERE project_key=?', (_key(project),)).fetchone()
        if row is None:
            raise ValueError('fact_unknown_project')
        return dict(row)

    def configure_fact_policy(self, project, *, response_observation, create_read_link):
        """Доверенная настройка Project/среды; модель не меняет TTL."""
        if any(type(v) is not int or not 1 <= v <= 31536000 for v in (response_observation, create_read_link)):
            raise ValueError('fact_invalid_ttl')
        with self.transaction():
            self._fact_policy(project)
            self.db.execute('UPDATE fact_projects SET response_observation=?,create_read_link=? WHERE project_key=?',
                            (response_observation, create_read_link, _key(project)))
            from ..validation import fingerprint
            limits = {'response_observation': response_observation, 'create_read_link': create_read_link}
            for row in self.db.execute('SELECT * FROM project_facts WHERE project_key=?', (_key(project),)).fetchall():
                fact = self._fact_record(row)
                if fact['type'] in limits:
                    fact['expires_at'] = min(fact['expires_at'], fact['last_verified_at'] + limits[fact['type']])
                    self.db.execute('UPDATE project_facts SET payload=?,payload_hash=? WHERE fact_id=?',
                                    (_json(fact), fingerprint(fact), fact['id']))

    def advance_fact_epoch(self, project):
        """Приложение сообщает о настоящем reset; epoch нельзя перемотать назад."""
        with self.transaction():
            self._fact_policy(project)
            self.db.execute('UPDATE fact_projects SET reset_epoch=reset_epoch+1 WHERE project_key=?', (_key(project),))
            return self._fact_policy(project)['reset_epoch']

    def _propose_fact(self, project, session, run_id, descriptor, *, preview=False):
        from ..facts import verify_fact, fact_view
        from ..openapi.parser import Catalog, canonical
        from ..validation import fingerprint
        run = self.read_run(project, session, run_id)
        row = self.db.execute('SELECT lifecycle FROM runs WHERE run_id=?', (run_id,)).fetchone()
        if row['lifecycle'] != 'finalized' and not preview:
            raise ValueError('fact_unfinished_run')
        snapshot = self._fact_snapshot(project, session, run_id, run)
        stamp = self.db.execute('SELECT * FROM fact_runs WHERE run_id=?', (run_id,)).fetchone()
        policy = self._fact_policy(project)
        fact = verify_fact(run, snapshot, descriptor, dict(stamp) if stamp else None, policy)
        if preview:
            return {'status': 'pending_finalization', 'type': fact['type'], 'proposal_hash': fingerprint(descriptor)}
        self.db.execute('INSERT OR IGNORE INTO project_facts VALUES(?,?,?,?,?)',
                        (fact['id'], _key(project), run_id, _json(fact), fingerprint(fact)))
        stored = self.db.execute('SELECT * FROM project_facts WHERE fact_id=? AND project_key=?',
                                 (fact['id'], _key(project))).fetchone()
        return fact_view(self._fact_record(stored), Catalog(canonical(run['contract'])), policy, time.time())

    def _fact_record(self, row):
        """Отпечаток ловит повреждение DTO; SQL-связи определяют Project/Run."""
        from ..validation import fingerprint
        fact = json.loads(row['payload'])
        origin = self.db.execute('SELECT payload FROM runs WHERE run_id=? AND project_key=?',
                                 (row['run_id'], row['project_key'])).fetchone()
        saved = json.loads(origin['payload']) if origin else {}
        if (fingerprint(fact) != row['payload_hash'] or fact['id'] != row['fact_id']
                or fact['origin'] != {'project_id': saved.get('project_id'),
                    'session_id': saved.get('session_id'), 'run_id': row['run_id']}):
            raise ValueError('fact_storage_binding_mismatch')
        return fact

    def _fact_snapshot(self, project, session, run_id, run):
        snapshot = self.snapshot(project, session, run_id)
        manifest = run.get('admission_manifest') or {}
        goal = manifest.get('goal_contract')
        if goal:
            from ..validation import fingerprint
            if fingerprint(goal['manifest']) != goal['manifest_hash']: raise ValueError('goal_manifest_changed')
            for name in ('extraction', 'binding_rules'):
                snapshot['metadata'][name] = manifest[name]
        return snapshot

    def propose_fact(self, project, session, run_id, descriptor):
        """Application API принимает только descriptor, никогда supplied evidence/verdict."""
        with self.transaction():
            return self._propose_fact(project, session, run_id, descriptor)

    def preview_fact(self, project, session, run_id, descriptor):
        """Проверить proposal на собственной истории; до finalize факт не публикуется."""
        with self.transaction():
            return self._propose_fact(project, session, run_id, descriptor, preview=True)

    def invalidate_fact(self, project, fact_id, session, run_id, exchange_id):
        """Сохранить опровержение без удаления исходного значения и evidence."""
        from ..facts import contradict_fact
        from ..validation import fingerprint
        with self.transaction():
            row = self.db.execute('SELECT * FROM project_facts WHERE fact_id=? AND project_key=?',
                                  (fact_id, _key(project))).fetchone()
            if row is None:
                raise ValueError('fact_unknown_fact')
            run = self.read_run(project, session, run_id)
            if self.db.execute('SELECT lifecycle FROM runs WHERE run_id=?', (run_id,)).fetchone()[0] != 'finalized':
                raise ValueError('fact_unfinished_run')
            stamp = self.db.execute('SELECT * FROM fact_runs WHERE run_id=?', (run_id,)).fetchone()
            fact = contradict_fact(self._fact_record(row), run, self.snapshot(project, session, run_id),
                                   dict(stamp) if stamp else None, self._fact_policy(project), exchange_id)
            self.db.execute('UPDATE project_facts SET payload=?,payload_hash=? WHERE fact_id=?',
                            (_json(fact), fingerprint(fact), fact_id))
            return fact

    def _collect_facts(self, project, session, run_id):
        from ..facts import automatic_proposals
        from itertools import chain
        run = self.read_run(project, session, run_id)
        if not run.get('contract'):
            return
        snapshot = self._fact_snapshot(project, session, run_id, run)
        for descriptor in chain(automatic_proposals(run, snapshot), run.get('fact_proposals', [])[:16]):
            try:
                self._propose_fact(project, session, run_id, descriptor)
            except ValueError:
                # Неподтверждённое наблюдение не становится фактом. Отказ SQL
                # не подавляется: итог и факты должны фиксироваться атомарно.
                continue

    def read_facts(self, project, *, catalog, operation_ids=None, now=None, include_inactive=False):
        """Без HTTP/LLM: Project facts доступны новой Session с текущими hashes."""
        from ..facts import checked_time, fact_view
        now = checked_time(time.time() if now is None else now)
        if self.db.execute('PRAGMA user_version').fetchone()[0] < 2:
            return []
        if self.db.execute('SELECT 1 FROM fact_projects WHERE project_key=?', (_key(project),)).fetchone() is None:
            return []
        policy = self._fact_policy(project)
        facts = []
        for row in self.db.execute('SELECT * FROM project_facts WHERE project_key=? ORDER BY rowid', (_key(project),)):
            fact = fact_view(self._fact_record(row), catalog, policy, now)
            if operation_ids is None or set(operation_ids) & fact['dependencies'].keys():
                facts.append(fact)
        # Разные подтверждённые значения того же scoped утверждения не
        # разрешаются уверенностью/датой. Оба сохраняются, актуальный выбор закрыт.
        groups = {}
        for fact in facts:
            if fact['type'] == 'response_observation' and fact['status'] == 'VERIFIED':
                key = _json([fact['dependencies'], fact['scope']])
                groups.setdefault(key, []).append(fact)
        for group in groups.values():
            if len({_json(f['value']) for f in group}) > 1:
                for fact in group:
                    fact.update(status='SUPPORTED', freshness='conflict')
        return [f for f in facts if include_inactive or f['status'] == 'VERIFIED']

    def recover_unfinished(self):
        """Вызывать только под execution lease; никакого replay внешних эффектов."""
        with self.transaction():
            for row in self.db.execute("SELECT * FROM runs WHERE lifecycle='active'").fetchall():
                run = json.loads(row["payload"])
                run.update(status="partial", stop_reason="interrupted", stop_category="interrupted")
                uncertain = run.get('uncertain_write') or run.get('pending_mutation') or any(e.get('effect_status') == 'uncertain'
                    for e in run.get('exchanges', []))
                if uncertain:
                    run.update(uncertain_write=True, primary_safety_cause='uncertain_write',
                               secondary_stop_reason='interrupted', stop_reason='uncertain_write',
                               stop_category='needs_human')
                run["report"] = report(run)
                if not uncertain:
                    run["report"]["summary"] = "Запуск прерван; внешний результат может быть неизвестен. Автоматического повтора нет."
                run["response_message_id"] = str(uuid.uuid5(uuid.NAMESPACE_URL, "answer:" + run["run_id"]))
                self._finalize(row, run)

    def read_run(self, project, session, run_id):
        row = self.db.execute("SELECT runs.payload, snapshots.payload AS snapshot_payload, snapshots.spec_hash FROM runs JOIN snapshots USING(snapshot_id) WHERE run_id=? AND runs.project_key=? AND session_key=?", (run_id, *self._scope(project, session))).fetchone()
        if row is None:
            raise ValueError("foreign_or_unknown_run")
        run = json.loads(row["payload"])
        snapshot = json.loads(row["snapshot_payload"])
        # SQL scope не делает подменённый JSON собственным историческим trace.
        if run.get("run_id") != run_id or run.get("contract") != snapshot.get("contract") or run.get("spec_hash") != row["spec_hash"]:
            raise ValueError("run_snapshot_binding_mismatch")
        return run

    def run_for_message(self, project, session, message):
        run = self.lookup(project, session, message)
        if run is None:
            raise ValueError("unknown_message")
        return run

    def messages(self, project, session):
        return [{"message_id": r["public_id"], "role": r["role"], "content": r["content"], "run_id": r["run_id"]} for r in self.db.execute("SELECT * FROM messages WHERE project_key=? AND session_key=? ORDER BY rowid", self._scope(project, session))]

    def episode(self, project, session, run_id):
        self.read_run(project, session, run_id)
        row = self.db.execute("SELECT payload FROM episodes WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def snapshot(self, project, session, run_id):
        self.read_run(project, session, run_id)
        row = self.db.execute("SELECT snapshots.payload FROM snapshots JOIN runs USING(snapshot_id) WHERE run_id=?", (run_id,)).fetchone()
        return json.loads(row["payload"])

"""Рестарт, атомарность и чтение истории проверяются на настоящей SQLite/HTTP."""

import json
from contextlib import closing
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.openapi import load_source
from demo_api.server import DemoServer
from agent_support import ObserverModel, count_test_tokens

try:
    from api_agent.storage import SQLiteHistory, StorageError
except ImportError:
    SQLiteHistory = None
    StorageError = ValueError


class SQLiteHistoryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(SQLiteHistory, "Нет постоянного хранилища истории")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "history.sqlite3"
        self.server = DemoServer()
        self.server.__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.cat = load_source(str(ROOT / "tests/fixtures/demo_openapi.json"))

    def app(self, store, **kw):
        return StartAgentRun(self.server.target, self.cat, history_store=store,
                             token_counter=count_test_tokens, **kw)

    def test_two_sessions_restore_their_own_answers_without_http(self):
        with SQLiteHistory(self.path) as store:
            a = self.app(store).start("p", "auth", "m1", "Check pet", ObserverModel())
            b = self.app(store).start("p", "orders", "m2", "Проверь контракт getPet", ObserverModel())
            self.assertEqual([a["status"], b["status"]], ["completed", "completed"])
        count = len(self.server.journal)
        with SQLiteHistory(self.path) as store:
            for session, run in [("auth", a), ("orders", b)]:
                answer = store.messages("p", session)[-1]
                self.assertEqual(answer["role"], "assistant")
                restored = store.run_for_message("p", session, answer["message_id"])
                self.assertEqual(restored["run_id"], run["run_id"])
                self.assertEqual(restored["exchanges"], run["exchanges"])
                self.assertEqual(restored["contract"], self.cat.snapshot)
                self.assertEqual(restored["goal"], run["goal"])
                self.assertEqual(store.episode("p", session, run["run_id"])["summary"], run["report"]["summary"])
            self.assertEqual(len(self.server.journal), count)

    def test_swapped_json_payload_is_rejected_before_showing_foreign_trace(self):
        with SQLiteHistory(self.path) as store:
            a = self.app(store).start("p", "s", "m1", "Check pet", ObserverModel())
            b = self.app(store).start("q", "s", "m2", "Проверь контракт getPet", ObserverModel())
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("UPDATE runs SET payload=(SELECT payload FROM runs WHERE run_id=?) WHERE run_id=?", (b["run_id"], a["run_id"]))
                db.commit()
            with self.assertRaises(ValueError):
                store.read_run("p", "s", a["run_id"])
            self.assertEqual(len(self.server.journal), 2)

    def test_exported_final_trace_contains_durable_answer_link(self):
        snapshots = []
        with SQLiteHistory(self.path) as store:
            run = self.app(store, trace_sink=snapshots.append).start("p", "s", "m", "Check pet", ObserverModel())
            self.assertEqual(snapshots[-1].get("response_message_id"), run["response_message_id"])
            self.assertEqual(store.run_for_message("p", "s", snapshots[-1]["response_message_id"])["run_id"], run["run_id"])

    def test_duplicate_delivery_after_restart_does_not_call_model_or_http(self):
        with SQLiteHistory(self.path) as store:
            old = self.app(store).start("p", "s", "m", "Check pet", ObserverModel())
        with SQLiteHistory(self.path) as store:
            def forbidden(payload):
                self.fail("Повторная доставка вызвала модель")
            actual = self.app(store).start("p", "s", "m", "Check pet", forbidden)
            self.assertEqual(actual["run_id"], old["run_id"])
            self.assertEqual(len(store.messages("p", "s")), 2)
            self.assertEqual(len(self.server.journal), 1)
            with self.assertRaises(ValueError):
                self.app(store).start("other", "s", "m", "Check pet", forbidden)

    def test_foreign_project_and_swapped_answer_rejected(self):
        with SQLiteHistory(self.path) as store:
            a = self.app(store).start("p", "a", "m1", "Check pet", ObserverModel())
            b = self.app(store).start("q", "b", "m2", "Check pet", ObserverModel())
            for project, session, run_id in [("q", "b", a["run_id"]), ("p", "b", a["run_id"]), ("p", "a", b["run_id"])]:
                with self.assertRaises(ValueError):
                    store.read_run(project, session, run_id)
            with self.assertRaises(ValueError):
                store.run_for_message("p", "a", b["response_message_id"])
            self.assertEqual(len(self.server.journal), 2)

    def test_target_and_environment_immutable(self):
        with SQLiteHistory(self.path) as store:
            self.app(store).start("p", "s", "m", "Check pet", ObserverModel())
            with DemoServer() as other:
                with self.assertRaises(ValueError):
                    StartAgentRun(other.target, self.cat, history_store=store,
                                  token_counter=count_test_tokens).start("p", "s", "m2", "Check pet", ObserverModel())
                self.assertEqual(other.journal, [])
            with self.assertRaises(ValueError):
                self.app(store, environment="other").start("p", "s", "m3", "Check pet", ObserverModel())

    def test_atomic_creation_failure_leaves_no_message_or_http(self):
        with SQLiteHistory(self.path) as store:
            app = self.app(store)
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("CREATE TRIGGER fail_start BEFORE INSERT ON runs BEGIN SELECT RAISE(ABORT, 'test failure'); END")
                db.commit()
            with self.assertRaises(StorageError):
                app.start("p", "s", "m", "Check pet", ObserverModel())
            with closing(sqlite3.connect(self.path)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM messages").fetchone()[0], 0)
            self.assertEqual(self.server.journal, [])
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("DROP TRIGGER fail_start")
                db.commit()
            retry = app.start("p", "s", "m", "Check pet", ObserverModel())
            self.assertEqual(retry["status"], "completed")
            self.assertEqual(len(self.server.journal), 1)

    def test_configuration_version_keeps_old_snapshot_and_rejects_stale_start(self):
        from api_agent.policy import ProjectConfig
        from api_agent.agent.catalog import CatalogWorkflow
        from test_catalog_execution import status_condition
        with SQLiteHistory(self.path) as store:
            def app(version, mode):
                cfg = ProjectConfig.create('p', self.server.target, self.cat, mode=mode, resettable=True,
                                           operations={'getPet':'A'}, version=version)
                wf = CatalogWorkflow(self.cat, cfg, goal='Check pet', conditions={'getPet':[status_condition(200)]})
                return self.app(store, workflow=wf)
            old = app(1, 'READ_ONLY').start('p','s','m1','Check pet',ObserverModel())
            newer = app(2, 'TEST').start('p','s','m2','Check pet',ObserverModel())
            self.assertEqual(store.snapshot('p','s',old['run_id'])['configuration']['mode'], 'READ_ONLY')
            self.assertEqual(store.snapshot('p','s',newer['run_id'])['configuration']['mode'], 'TEST')
            with self.assertRaises(ValueError):
                app(1, 'READ_ONLY').start('p','s','stale','Check pet',ObserverModel())
            with self.assertRaises(ValueError):
                app(2, 'READ_ONLY').start('p','s','tamper','Check pet',ObserverModel())
            self.assertEqual(len(self.server.journal), 2)

    def test_finalization_idempotent_and_parent_requires_same_project(self):
        with SQLiteHistory(self.path) as store:
            app = self.app(store)
            a = app.start("p", "s", "m", "Check pet", ObserverModel())
            store.finalize("p", "s", "m", a)
            store.finalize("p", "s", "m", a)
            self.assertEqual(len(store.messages("p", "s")), 2)
            b = app.start("p", "s", "retest", "Check pet", ObserverModel(), parent_run_id=a["run_id"])
            self.assertEqual(b["parent_run_id"], a["run_id"])
            with self.assertRaises(ValueError):
                app.start("q", "s", "foreign-parent", "Check pet", ObserverModel(), parent_run_id=a["run_id"])
            self.assertEqual(len(self.server.journal), 2)

    def test_process_crash_marks_original_run_interrupted_without_replay(self):
        with SQLiteHistory(self.path):
            pass
        worker = '''
import os,sys
sys.path[:0]=["src","tests","."]
from api_agent.storage import SQLiteHistory
from api_agent.agent import StartAgentRun
from api_agent.openapi import load_source
from agent_support import reply,count_test_tokens
calls=0
def model(payload):
 global calls
 calls+=1
 if calls==1:return reply("execute_operation",{"operation_id":"getPet"})
 os._exit(9)
with SQLiteHistory(sys.argv[1]) as store:
 StartAgentRun(sys.argv[2],load_source("tests/fixtures/demo_openapi.json"),history_store=store,token_counter=count_test_tokens).start("p","s","crash","Check pet",model)
'''
        process = subprocess.run([sys.executable, "-c", worker, str(self.path), self.server.target], cwd=ROOT, capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 9, process.stderr.decode())
        with SQLiteHistory(self.path) as store:
            run = self.app(store).start("p", "s", "crash", "Check pet", lambda _: self.fail("Повтор после crash"))
            self.assertEqual(run["stop_reason"], "interrupted")
            self.assertEqual(run["requests"], 1)
            self.assertEqual(len(self.server.journal), 1)
            self.assertEqual(store.messages("p", "s")[-1]["run_id"], run["run_id"])

    def test_public_snapshot_and_database_do_not_store_configured_secrets(self):
        with SQLiteHistory(self.path) as store:
            run = self.app(store, secrets=("private-marker",)).start("private-marker", "s", "private-marker", "Check pet", ObserverModel())
            self.assertNotIn("private-marker", json.dumps(store.read_run("private-marker", "s", run["run_id"])))
        self.assertNotIn(b"private-marker", self.path.read_bytes())

    def test_swapped_run_in_same_session_is_not_presented_as_own_answer(self):
        with SQLiteHistory(self.path) as store:
            a = self.app(store).start("p", "s", "m1", "Check pet", ObserverModel())
            b = self.app(store).start("p", "s", "m2", "Check pet", ObserverModel())
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("UPDATE messages SET run_id=? WHERE public_id=?", (b["run_id"], a["response_message_id"]))
                db.commit()
            with self.assertRaises(ValueError):
                store.run_for_message("p", "s", a["response_message_id"])

    def test_finalization_failure_rolls_back_answer_and_episode(self):
        with SQLiteHistory(self.path) as store:
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("CREATE TRIGGER fail_finish BEFORE INSERT ON episodes BEGIN SELECT RAISE(ABORT, 'test'); END")
                db.commit()
            run = self.app(store).start("p", "s", "m", "Check pet", ObserverModel())
            self.assertEqual(run["stop_reason"], "storage_error")
            self.assertEqual([m["role"] for m in store.messages("p", "s")], ["user"])
            self.assertIsNone(store.episode("p", "s", run["run_id"]))
            self.assertEqual(len(self.server.journal), 1)

    def test_unrecognized_schema_keeps_original_and_backup(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("CREATE TABLE owner_data(value TEXT)")
            db.execute("INSERT INTO owner_data VALUES('preserve')")
            db.commit()
        with self.assertRaises(StorageError):
            SQLiteHistory(self.path)
        self.assertEqual(len(list(self.path.parent.glob('*.before-migration-*'))), 1)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT value FROM owner_data").fetchone()[0], 'preserve')

    def test_live_execution_lease_does_not_recover_another_process_run(self):
        with SQLiteHistory(self.path) as first, SQLiteHistory(self.path) as second:
            with first.execution():
                with self.assertRaises(StorageError):
                    with second.execution():
                        self.fail("Два владельца исполнения одной базы")

    def test_read_only_history_never_creates_database_or_recovers_runs(self):
        import inspect
        self.assertIn("read_only", inspect.signature(SQLiteHistory).parameters, "Нет read-only открытия истории")
        absent = self.path.parent / 'absent.sqlite3'
        with self.assertRaises(StorageError):
            SQLiteHistory(absent, read_only=True)
        self.assertFalse(absent.exists())
        with SQLiteHistory(self.path) as store:
            run = self.app(store).start("p", "s", "m", "Check pet", ObserverModel())
        with SQLiteHistory(self.path, read_only=True) as reader:
            self.assertEqual(reader.read_run("p", "s", run["run_id"])["status"], "completed")
            with self.assertRaises(StorageError):
                with reader.execution():
                    pass
        self.assertEqual(len(self.server.journal), 1)


if __name__ == "__main__":
    unittest.main()

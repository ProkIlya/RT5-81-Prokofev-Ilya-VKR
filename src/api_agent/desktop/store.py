"""Отдельное состояние интерфейса; схема immutable Run history не изменяется."""
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import threading
import uuid

from api_agent.bounded_json import validate_tree
from api_agent.validation import fingerprint


def encoded(value):
    validate_tree(value, max_depth=24, max_bytes=65536)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class DesktopStore:
    def __init__(self, path):
        self.path = Path(path).resolve(); self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON'); self.db.execute('PRAGMA busy_timeout=2000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS ui_projects(id TEXT PRIMARY KEY,label TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ui_sessions(project TEXT NOT NULL REFERENCES ui_projects,id TEXT NOT NULL,
                label TEXT NOT NULL,marker TEXT NOT NULL DEFAULT '',PRIMARY KEY(project,id));
            CREATE TABLE IF NOT EXISTS ui_drafts(project TEXT NOT NULL,session TEXT NOT NULL,payload TEXT NOT NULL,
                PRIMARY KEY(project,session),FOREIGN KEY(project,session) REFERENCES ui_sessions(project,id));
            CREATE TABLE IF NOT EXISTS ui_models(id TEXT NOT NULL,revision INTEGER NOT NULL,payload TEXT NOT NULL,
                hash TEXT NOT NULL,PRIMARY KEY(id,revision));
            CREATE TABLE IF NOT EXISTS ui_settings(id TEXT PRIMARY KEY,payload TEXT NOT NULL);
        ''')

    def __enter__(self): return self
    def __exit__(self, *args): self.close()
    def close(self): self.db.close()

    def add_project(self, project, label, configuration):
        allowed = {'target','spec_path','policy_path','mappings_path','image','history_path'}
        if set(configuration) - allowed: raise ValueError('desktop_project_fields')
        old = self.project(project, required=False)
        if old and old['configuration'].get('target') != configuration.get('target'):
            raise ValueError('desktop_project_target_immutable')
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO ui_projects VALUES(?,?,?)',
                            (project, str(label)[:200], encoded(configuration)))

    def project(self, project, *, required=True):
        row = self.db.execute('SELECT * FROM ui_projects WHERE id=?', (project,)).fetchone()
        if row is None:
            if required: raise ValueError('desktop_unknown_project')
            return None
        return {'id': row['id'], 'label': row['label'], 'configuration': json.loads(row['payload'])}

    def projects(self):
        return [self.project(r['id']) for r in self.db.execute('SELECT id FROM ui_projects ORDER BY rowid')]

    def add_session(self, project, session=None, label='Основная'):
        self.project(project); session = session or uuid.uuid4().hex
        with self.lock:
            self.db.execute('INSERT OR IGNORE INTO ui_sessions(project,id,label) VALUES(?,?,?)',
                            (project, session, str(label)[:200]))
        return session

    def sessions(self, project):
        return [dict(r) for r in self.db.execute('SELECT * FROM ui_sessions WHERE project=? ORDER BY rowid', (project,))]

    def mark(self, project, session, marker):
        if marker not in {'', 'active', 'input', 'done', 'error'}: raise ValueError('desktop_invalid_marker')
        with self.lock:
            self.db.execute('UPDATE ui_sessions SET marker=? WHERE project=? AND id=?', (marker, project, session))

    def draft(self, project, session):
        row = self.db.execute('SELECT payload FROM ui_drafts WHERE project=? AND session=?', (project, session)).fetchone()
        return json.loads(row[0]) if row else {'goal': '', 'cards': [], 'attachments': [], 'model_id': None}

    def save_draft(self, project, session, draft):
        if set(draft) - {'goal','cards','attachments','model_id','input_slots'}:
            raise ValueError('desktop_private_or_unknown_draft_field')
        if not isinstance(draft.get('goal'), str) or not isinstance(draft.get('cards'), list):
            raise ValueError('desktop_invalid_draft')
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO ui_drafts VALUES(?,?,?)', (project, session, encoded(draft)))

    def save_model(self, key, profile):
        allowed = {'name','kind','endpoint','model_name','context_tokens','output_tokens',
                   'gguf','executable','port','gpu_layers','startup_seconds'}
        if set(profile) - allowed or profile.get('kind') not in {'server','gguf'}:
            raise ValueError('desktop_invalid_model_profile')
        payload = encoded(profile); digest = fingerprint(profile)
        with self.lock:
            old = self.model(key, required=False)
            if old and old['hash'] == digest: return old
            revision = old['revision'] + 1 if old else 1
            self.db.execute('INSERT INTO ui_models VALUES(?,?,?,?)', (key, revision, payload, digest))
        return {'id': key, 'revision': revision, 'profile': deepcopy(profile), 'hash': digest}

    def model(self, key, *, required=True):
        row = self.db.execute('SELECT * FROM ui_models WHERE id=? ORDER BY revision DESC LIMIT 1', (key,)).fetchone()
        if row is None:
            if required: raise ValueError('desktop_unknown_model')
            return None
        return {'id': row['id'], 'revision': row['revision'], 'profile': json.loads(row['payload']), 'hash': row['hash']}

    def models(self):
        return [self.model(r[0]) for r in self.db.execute('SELECT DISTINCT id FROM ui_models ORDER BY id')]

    def setting(self, key, default=None):
        row = self.db.execute('SELECT payload FROM ui_settings WHERE id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else deepcopy(default)

    def set_setting(self, key, value):
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO ui_settings VALUES(?,?)', (key, encoded(value)))

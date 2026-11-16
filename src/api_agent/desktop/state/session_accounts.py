"""Учётные данные сессии связаны с проектом, целью API и версией OpenAPI."""
import json

from api_agent.openapi.model import canonical
from api_agent.setup.credentials import _crypt


class SessionAccounts:
    def __init__(self, store):
        self.store = store
        with store.lock:
            store.db.execute('''CREATE TABLE IF NOT EXISTS ui_session_accounts(
                project TEXT NOT NULL, session TEXT NOT NULL, login TEXT, sealed BLOB,
                PRIMARY KEY(project,session), FOREIGN KEY(project,session) REFERENCES ui_sessions(project,id),
                CHECK ((login IS NULL) = (sealed IS NULL)))''')

    def _row(self, project, session):
        with self.store.lock:
            return self.store.db.execute('SELECT login,sealed FROM ui_session_accounts WHERE project=? AND session=?',
                                         (project, session)).fetchone()

    def login_name(self, project, session):
        row = self._row(project, session)
        return row['login'] if row is not None else None

    def save(self, project, session, scope, account):
        login, sealed = None, None
        if account is not None:
            login, password = account
            if not 0 < len(login.strip()) <= 200 or not 0 < len(password) <= 4096 or any(c in login for c in '\r\n'):
                raise ValueError('connection_goal_credentials_missing')
            sealed = _crypt(canonical(account), {**scope, 'project': project, 'session': session, 'kind': 'session-account'})
        with self.store.lock:
            self.store.db.execute('INSERT OR REPLACE INTO ui_session_accounts VALUES(?,?,?,?)',
                                  (project, session, login, sealed))

    def load(self, project, session, scope):
        row = self._row(project, session)
        if row is None:
            return False, None
        if row['sealed'] is None:
            return True, None
        try:
            raw = _crypt(row['sealed'], {**scope, 'project': project, 'session': session, 'kind': 'session-account'}, decrypt=True)
            login, password = json.loads(raw)
            if login != row['login'] or not isinstance(password, str) or not password:
                raise ValueError('invalid_account')
            return True, (login, password)
        except (OSError, ValueError, TypeError):
            raise ValueError('connection_secure_storage_failed') from None

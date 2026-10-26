-- Связи ограничены Project/Session; исходное сообщение создаёт один Run.
CREATE TABLE projects (
 project_key TEXT PRIMARY KEY, target TEXT NOT NULL, environment TEXT NOT NULL
);
CREATE TABLE snapshots (
 snapshot_id TEXT PRIMARY KEY, project_key TEXT NOT NULL REFERENCES projects,
 config_version INTEGER NOT NULL, spec_hash TEXT, payload TEXT NOT NULL,
 UNIQUE(project_key, config_version), UNIQUE(snapshot_id, project_key)
);
CREATE TABLE sessions (
 session_key TEXT PRIMARY KEY, project_key TEXT NOT NULL REFERENCES projects,
 public_id TEXT NOT NULL, UNIQUE(session_key, project_key)
);
CREATE TABLE messages (
 message_key TEXT PRIMARY KEY, project_key TEXT NOT NULL, session_key TEXT NOT NULL,
 public_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('user','assistant')),
 content TEXT NOT NULL, run_id TEXT,
 FOREIGN KEY(session_key, project_key) REFERENCES sessions(session_key, project_key),
 FOREIGN KEY(run_id, project_key, session_key) REFERENCES runs(run_id, project_key, session_key)
 DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE runs (
 run_id TEXT PRIMARY KEY, project_key TEXT NOT NULL, session_key TEXT NOT NULL,
 original_message_key TEXT NOT NULL UNIQUE REFERENCES messages(message_key),
 snapshot_id TEXT NOT NULL, lifecycle TEXT NOT NULL CHECK(lifecycle IN ('active','finalized')),
 payload TEXT NOT NULL, parent_run_id TEXT REFERENCES runs(run_id),
 UNIQUE(run_id, project_key, session_key),
 FOREIGN KEY(session_key, project_key) REFERENCES sessions(session_key, project_key),
 FOREIGN KEY(snapshot_id, project_key) REFERENCES snapshots(snapshot_id, project_key)
);
CREATE TABLE events (
 run_id TEXT NOT NULL REFERENCES runs(run_id), sequence INTEGER NOT NULL,
 payload TEXT NOT NULL, PRIMARY KEY(run_id, sequence)
);
CREATE TABLE episodes (
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id), payload TEXT NOT NULL
);
CREATE INDEX message_session ON messages(project_key, session_key);

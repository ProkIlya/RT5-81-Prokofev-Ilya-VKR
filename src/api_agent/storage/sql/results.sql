-- Versioned отчёт и его evidence записываются в транзакции завершения Run.
CREATE TABLE reports (
 report_id TEXT PRIMARY KEY,
 run_id TEXT NOT NULL UNIQUE REFERENCES runs(run_id),
 project_key TEXT NOT NULL,
 session_key TEXT NOT NULL,
 payload_hash TEXT NOT NULL,
 payload TEXT NOT NULL,
 FOREIGN KEY(run_id, project_key, session_key) REFERENCES runs(run_id, project_key, session_key)
);
CREATE INDEX report_scope ON reports(project_key, session_key, run_id);

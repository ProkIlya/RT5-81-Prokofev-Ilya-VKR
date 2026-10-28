-- S09a: отдельная транзакционная миграция не меняет исторические Run payloads.
CREATE TABLE fact_projects (
 project_key TEXT PRIMARY KEY REFERENCES projects(project_key),
 reset_epoch INTEGER NOT NULL DEFAULT 0 CHECK(reset_epoch >= 0),
 response_observation INTEGER NOT NULL DEFAULT 3600 CHECK(response_observation > 0),
 create_read_link INTEGER NOT NULL DEFAULT 3600 CHECK(create_read_link > 0)
);
INSERT INTO fact_projects(project_key) SELECT project_key FROM projects;
CREATE TABLE fact_runs (
 run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
 reset_epoch INTEGER NOT NULL,
 started_at REAL NOT NULL
);
CREATE TABLE project_facts (
 fact_id TEXT PRIMARY KEY,
 project_key TEXT NOT NULL REFERENCES projects(project_key),
 run_id TEXT NOT NULL REFERENCES runs(run_id),
 payload TEXT NOT NULL,
 payload_hash TEXT NOT NULL
);
CREATE INDEX facts_by_project ON project_facts(project_key);

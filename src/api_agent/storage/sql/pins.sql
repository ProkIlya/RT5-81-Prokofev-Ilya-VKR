-- Закладка ссылается на неизменный отчёт, не создаёт finding или новый verdict.
CREATE TABLE report_pins (
 project_key TEXT NOT NULL REFERENCES projects(project_key),
 report_id TEXT NOT NULL REFERENCES reports(report_id),
 label TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(project_key,report_id)
);

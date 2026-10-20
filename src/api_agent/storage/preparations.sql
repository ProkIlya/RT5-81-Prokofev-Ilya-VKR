-- До generator вызова команда уже durable; незавершённая подготовка не replay.
CREATE TABLE goal_preparations (
 message_key TEXT PRIMARY KEY,
 project_key TEXT NOT NULL,
 session_key TEXT NOT NULL,
 goal_hash TEXT NOT NULL,
 payload TEXT NOT NULL
);

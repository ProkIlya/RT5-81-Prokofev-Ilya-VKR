"""SQLite-история: хранение безопасных снимков, без исполнения HTTP/LLM."""

from api_agent.storage.history import SQLiteHistory, StorageError

__all__ = ["SQLiteHistory", "StorageError"]

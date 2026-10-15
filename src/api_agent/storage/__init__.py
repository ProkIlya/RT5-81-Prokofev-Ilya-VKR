"""SQLite-история: хранение безопасных снимков, без исполнения HTTP/LLM."""

from .history import SQLiteHistory, StorageError

__all__ = ["SQLiteHistory", "StorageError"]

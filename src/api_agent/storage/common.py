"""Безопасные ошибки, ключи и bounded SQL JSON."""
import hashlib
import json

from ..bounded_json import validate_tree

class StorageError(ValueError):
    """Безопасная причина отказа: SQL/пути/исходные значения не публикуются."""


def _key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def _json(value):
    validate_tree(value, max_depth=96, max_nodes=100000, max_bytes=4194304)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

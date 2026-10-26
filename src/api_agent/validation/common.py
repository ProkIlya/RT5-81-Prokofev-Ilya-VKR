"""Общие идентификаторы и безопасные значения проверки."""
from copy import deepcopy
import hashlib

from api_agent.openapi.parser import canonical

PREDICATES = {
    "status_in",
    "content_type_is_json",
    "json_valid",
    "required_fields_present",
    "supported_schema_types",
    "json_value_equals",
    "json_collection_matches",
}
SOURCES = {"CONTRACT", "USER_DEFINED", "MODEL_HYPOTHESIS"}


def fingerprint(value):
    """Канонический отпечаток защищает неизменные условия и результаты."""
    return hashlib.sha256(canonical(value)).hexdigest()


def display_value(value):
    """Ограничить диагностическое значение, сохранив сравнение на полном JSON.

    Большие значения не пересказываются и не получают сырого префикса:
    это сохраняет предел отчёта и не обходит последующее маскирование.
    """
    size = len(canonical(value))
    if size > 1024:
        return {"omitted": True, "bytes_total": size}
    return deepcopy(value)

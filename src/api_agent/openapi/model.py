"""Immutable каталог, профиль и безопасные JSON Pointer helpers."""
from dataclasses import dataclass
import hashlib
import json
import re

PROFILE_VERSION = "rest-json-v1"
PARSER_VERSION = "1"
MAX_BYTES = 262144
MAX_DEPTH = 32
MAX_NODES = 20000
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
KINDS = {"schemas", "parameters", "requestBodies", "responses", "securitySchemes"}
TYPES = {"object", "array", "string", "integer", "number", "boolean"}


class OpenApiError(ValueError):
    """Безопасная диагностика: никаких значений, body или exception исходного I/O."""

    def __init__(self, code, pointer=""):
        self.code, self.pointer = code, pointer
        super().__init__(f"{code} at {pointer or '/'}")


def canonical(value):
    """Одинаковые декларации имеют одинаковые bytes независимо от порядка keys."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _annotation(text):
    """Hash полного описания отличает изменения за пределами видимого фрагмента."""
    return {'text': text[:2048], 'characters_total': len(text), 'truncated': len(text) > 2048,
            'text_hash': hashlib.sha256(text.encode()).hexdigest()}


def pointer_part(value):
    return value.replace("~", "~0").replace("/", "~1")


def pointer_get(value, pointer):
    """Закрытый JSON Pointer: неверные escapes и неканонические индексы запрещены."""
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise KeyError
    for part in pointer[1:].split("/"):
        if re.search(r"~(?![01])", part):
            raise KeyError
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", part):
                raise KeyError
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise KeyError
    return value


@dataclass(frozen=True)
class Catalog:
    """Immutable bytes защищают основания истории от изменения выданного dict.

    Это снимок parser, не разрешение исполнения. HTTP policy принимает решение
    отдельно; наличие операции в каталоге не даёт модели прав на неё.
    """

    _snapshot: bytes
    _metadata: bytes = b"{}"
    _documentation: bytes = b"{}"

    @property
    def documentation(self):
        """Недоверенные описания отдельно от execution snapshot и оснований."""
        return json.loads(self._documentation)

    @property
    def search_metadata(self):
        """Недоверенные bounded аннотации: вне validation basis и hashes."""
        return json.loads(self._metadata)

    @property
    def snapshot(self):
        return json.loads(self._snapshot)

    @property
    def operations(self):
        return self.snapshot["operations"]

    @property
    def spec_hash(self):
        return hashlib.sha256(self._snapshot).hexdigest()

    @property
    def operation_hashes(self):
        return {
            name: hashlib.sha256(canonical(op)).hexdigest()
            for name, op in self.operations.items()
        }

    def resolve_basis(self, operation_id, pointer, operation_hash):
        """Проверить версию и наличие поддержанного основания до создания expectation."""
        try:
            if self.operation_hashes[operation_id] != operation_hash:
                raise KeyError
            return pointer_get(self.operations[operation_id], pointer)
        except (KeyError, IndexError, ValueError, TypeError):
            raise OpenApiError("invalid_basis") from None


def object_keys(value, allowed, required, path, code="invalid_document"):
    """Unknown keys — unsupported; неверная форма — отдельная точная диагностика."""
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise OpenApiError(code, path)
    if set(value) - set(allowed):
        raise OpenApiError("unsupported", path)
    if set(required) - set(value):
        raise OpenApiError(code, path)
    for key in ("description", "summary"):
        if key in value and not isinstance(value[key], str):
            raise OpenApiError(code, path)


def nonempty(value):
    return (
        isinstance(value, str)
        and bool(value)
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def sensitive(value):
    """Консервативные имена credentials; произвольные неизвестные secrets не угадываются."""
    return any(
        word in re.sub(r"[^a-z0-9]", "", value.lower())
        for word in (
            "token",
            "password",
            "secret",
            "credential",
            "apikey",
            "authorization",
        )
    )

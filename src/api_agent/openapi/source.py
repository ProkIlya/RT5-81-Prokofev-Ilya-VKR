"""Ограниченная загрузка источника; недоверенный URL не расширяет target.

Не используются urllib redirects, системные proxies и DNS. SocketDeadline
защищает целиком headers/body, включая медленную передачу маленькими порциями.
Refs разрешает только parser в памяти: здесь нет рекурсивного загрузчика.
"""

import http.client
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from ..socket_deadline import SocketDeadline
from .parser import MAX_BYTES, OpenApiError, parse_document


def unique_pairs(pairs):
    """JSON с повторённым ключом неоднозначен: не принимать last-wins."""
    value = {}
    for key, item in pairs:
        if key in value:
            raise OpenApiError("invalid_document")
        value[key] = item
    return value


def source_address(source, target):
    """Target доверен приложению, но его форма тоже проверяется до socket connect."""
    try:
        if not isinstance(target, str) or not re.fullmatch(
            r"http://127\.0\.0\.1:[1-9][0-9]{0,4}/?", target
        ):
            raise ValueError
        allowed = urlsplit(target)
        parts = urlsplit(source)
        if not allowed.port or allowed.port > 65535:
            raise ValueError
        origin = target.rstrip("/")
        if (
            parts.scheme != "http"
            or parts.netloc != allowed.netloc
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or not parts.path.startswith("/")
            or not re.fullmatch(r"/[A-Za-z0-9_./-]*", parts.path)
            or any(seg in (".", "..") for seg in parts.path.split("/"))
            or "//" in parts.path
            or source != origin + parts.path
        ):
            raise ValueError
        return allowed.port, parts.path
    except (ValueError, TypeError, AttributeError):
        raise OpenApiError("source_denied") from None


def read_http(source, target, timeout):
    """Один GET: redirects/неполное тело/другой Content-Type не дают контракт."""
    port, path = source_address(source, target)
    if (
        type(timeout) not in (int, float)
        or not math.isfinite(timeout)
        or not 0 < timeout <= 5
    ):
        raise OpenApiError("source_denied")
    deadline = time.monotonic() + timeout
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    response = None
    try:
        connection.connect()
        with SocketDeadline(connection.sock, deadline) as guard:
            connection.request("GET", path, headers={"Accept": "application/json"})
            response = connection.getresponse()
            guard.check()
            if 300 <= response.status < 400:
                raise OpenApiError("source_denied")
            if (
                response.status != 200
                or response.getheader("Content-Type", "").split(";")[0].strip().lower()
                != "application/json"
            ):
                raise OpenApiError("source_error")
            # Не принимать неоднозначный framing, даже если http.client выбрал один.
            lengths = response.headers.get_all("Content-Length", [])
            encodings = response.headers.get_all("Transfer-Encoding", [])
            if (
                len(lengths) > 1
                or len(encodings) > 1
                or (lengths and encodings)
                or (lengths and not re.fullmatch(r"[0-9]+", lengths[0]))
                or (encodings and encodings[0].lower() != "chunked")
            ):
                raise OpenApiError("source_error")
            if lengths and int(lengths[0]) > MAX_BYTES:
                raise OpenApiError("limits")
            chunks, size = [], 0
            while size <= MAX_BYTES:
                guard.check()
                chunk = response.read1(min(4096, MAX_BYTES + 1 - size))
                guard.check()
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > MAX_BYTES:
                raise OpenApiError("limits")
            if response.length is not None and response.length > 0:
                raise OpenApiError("source_error")
            guard.check()
            return b"".join(chunks)
    except OpenApiError:
        raise
    except (OSError, http.client.HTTPException, ValueError):
        raise OpenApiError("source_error") from None
    finally:
        # HTTP/1.0 и Connection: close отделяют response от connection. Поэтому
        # connection.close() недостаточно при отказе до чтения body: закрываем
        # также настоящий file/socket владельца ответа, включая timeout ветки.
        if response is not None:
            response.close()
        connection.close()


def load_source(source, *, target=None, timeout=5):
    """Файл/URL возвращает только полностью проверенный очищенный Catalog.

    Ошибка чтения никогда не включает имя файла, URL или response body, которые
    могут содержать секрет. Ограничение bytes проверяется до JSON decoding.
    """
    if isinstance(source, str) and re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", source):
        raw = read_http(source, target, timeout)
    else:
        # UNC/device prefixes означают потенциальный сетевой файловый источник.
        # Проверка выполняется до Path.open, а URL имеют отдельный HTTP guard.
        try:
            name = str(Path(source))
            if name.startswith((chr(92) * 2, "//")):
                raise OpenApiError("source_denied")
        except (TypeError, ValueError):
            raise OpenApiError("source_denied") from None
        try:
            with Path(source).open("rb") as stream:
                raw = stream.read(MAX_BYTES + 1)
        except (OSError, TypeError, ValueError):
            raise OpenApiError("source_error") from None
    if len(raw) > MAX_BYTES:
        raise OpenApiError("limits")
    try:
        value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_pairs)
    except (ValueError, UnicodeError, RecursionError):
        raise OpenApiError("invalid_document") from None
    return parse_document(value)

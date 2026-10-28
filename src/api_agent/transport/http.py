"""Общие сетевые границы: канонический target, policy denial и JSON числа.

HTTP-исполнение находится в execution/transport_session.py.
"""
import json
from urllib.parse import urlsplit


def reject_json_constant(_value):
    """NaN/Infinity не являются JSON: не путать их с пределом приложения."""
    raise json.JSONDecodeError("nonfinite JSON constant", "", 0)


class PolicyDenied(ValueError):
    """Действие отклонено до побочного эффекта или исчерпан бюджет запуска."""

    pass


def loopback_origin(target):
    """Проверить канонический origin без DNS, credentials и скрытых компонентов.

    Числовой 127.0.0.1 исключает DNS rebinding в пределах этого локального среза.
    Имена хостов и внешние стенды здесь намеренно не поддерживаются.
    """
    try:
        parts = urlsplit(target)
        port = parts.port
    except (TypeError, ValueError) as error:
        raise PolicyDenied("invalid target") from error
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or port is None
        or port < 1
        or parts.username
        or parts.password
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
        or target not in (f"http://127.0.0.1:{port}", f"http://127.0.0.1:{port}/")
    ):
        raise PolicyDenied("S02 requires numeric loopback HTTP with explicit port")
    return f"http://127.0.0.1:{port}", port

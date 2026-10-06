"""Ограничить недоверенное JSON-дерево до рекурсивного копирования и маскирования."""

import json
import math


def validate_tree(value, *, max_depth=64, max_nodes=10000, max_bytes=1048576):
    """Итеративно проверить типы, глубину, число узлов и размер JSON.

    Цикл также достигает depth budget: обход не использует стек Python и не
    вызывает пользовательские методы. Разрешены только точные JSON-типы;
    NaN/Infinity, объекты, tuple и нестроковые ключи не проходят эту границу.
    Проверка строк до json.dumps ограничивает промежуточные выделения памяти.
    """
    stack = [(value, 0)]
    nodes = size = 0
    while stack:
        item, depth = stack.pop()
        nodes += 1
        if depth > max_depth or nodes > max_nodes:
            raise ValueError("JSON structure limit")
        kind = type(item)
        if kind is dict:
            if len(item) > max_nodes - nodes:
                raise ValueError("JSON node limit")
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("JSON key type")
                stack.extend(((key, depth + 1), (child, depth + 1)))
        elif kind is list:
            if len(item) > max_nodes - nodes:
                raise ValueError("JSON node limit")
            stack.extend((child, depth + 1) for child in item)
        elif kind is str:
            if len(item) > max_bytes:
                raise ValueError("JSON string limit")
            size += len(item.encode("utf-8"))
        elif kind is float:
            if not math.isfinite(item):
                raise ValueError("JSON nonfinite number")
            size += len(json.dumps(item, allow_nan=False))
        elif kind is int:
            if item.bit_length() > 4096:
                raise ValueError("JSON integer limit")
            size += len(str(item))
        elif kind in (bool, type(None)):
            size += 5
        else:
            raise ValueError("JSON value type")
        if size > max_bytes:
            raise ValueError("JSON size limit")
    if (
        len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        > max_bytes
    ):
        raise ValueError("JSON encoded size limit")


def numeric_metadata(value, fields):
    """Принять только известные конечные неотрицательные числовые показатели."""
    if type(value) is not dict or set(value) - set(fields):
        raise ValueError("invalid model metadata")
    for field, number in value.items():
        # TTFT отсутствует, если поток оборвался до первого токена; остальные
        # показатели либо отсутствуют целиком, либо содержат действительное число.
        if number is None and field != "ttft_seconds":
            raise ValueError("missing model metric")
        if number is not None and (
            type(number) not in (int, float)
            or (type(number) is float and not math.isfinite(number))
            or number < 0
        ):
            raise ValueError("invalid model metric")
    return dict(value)

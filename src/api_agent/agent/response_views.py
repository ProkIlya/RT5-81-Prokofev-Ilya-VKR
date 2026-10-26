"""Ограниченные проекции сохранённого JSON: неполнота явная, сеть не нужна."""

from copy import deepcopy
import json
from api_agent.openapi.parser import pointer_get


def _bytes(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode())


def _child(path, key):
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _preview(value, budget, path, depth=0):
    if _bytes(value) <= budget:
        return deepcopy(value), []
    if isinstance(value, str):
        end = min(len(value), 128)
        while end and _bytes(value[:end]) > budget:
            end //= 2
        return value[:end], [path]
    if not isinstance(value, (dict, list)):
        return None, [path]
    output = {} if isinstance(value, dict) else []
    if depth >= 4:
        return output, [path]
    omitted = []
    entries = list(value.items()) if isinstance(value, dict) else list(enumerate(value))
    for index, (key, item) in enumerate(entries):
        if index >= 8:
            omitted.append(path)
            break
        available = budget - _bytes(output) - (_bytes(str(key)) + 2 if isinstance(value, dict) else 1)
        if available < 16:
            omitted.append(path)
            break
        projected, missing = _preview(item, min(available, 512), _child(path, key), depth + 1)
        candidate = {**output, key: projected} if isinstance(value, dict) else output + [projected]
        if _bytes(candidate) > budget:
            omitted.append(path)
            break
        output = candidate
        omitted.extend(missing)
    return output, list(dict.fromkeys(omitted))[:16]


def response_page(value, *, json_pointer="", offset=0, limit=8, max_bytes=2048):
    """Pointer выбирается после redaction; offset означает элементы/поля/символы.

    complete относится ко всему выбранному узлу, page_complete — к этой странице.
    omitted_paths позволяют раскрывать усечённые дочерние узлы тем же инструментом.
    """
    if not isinstance(json_pointer, str) or len(json_pointer) > 200 or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 16:
        raise ValueError("invalid_response_page")
    try:
        node = value if json_pointer == "" else pointer_get(value, json_pointer)
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError("unknown_response_pointer") from None
    if isinstance(node, (list, dict)):
        kind = "array" if isinstance(node, list) else "object"
        entries = list(enumerate(node)) if isinstance(node, list) else list(node.items())
        total = len(entries)
        if offset > total:
            raise ValueError("response_offset_limit")
        output = [] if kind == "array" else {}
        omitted = []
        returned = 0
        for key, item in entries[offset:offset + limit]:
            available = max_bytes - _bytes(output) - (_bytes(str(key)) + 2 if kind == "object" else 1)
            if available < 16:
                break
            part, missing = _preview(item, available, _child(json_pointer, key))
            candidate = output + [part] if kind == "array" else {**output, key: part}
            if _bytes(candidate) > max_bytes:
                break
            output = candidate
            omitted.extend(missing)
            returned += 1
        next_offset = offset + returned if offset + returned < total else None
    elif isinstance(node, str):
        kind, total = "string", len(node)
        if offset > total:
            raise ValueError("response_offset_limit")
        # Поле читается целиком, если помещается; длинная строка идёт по символам.
        output = node[offset:offset + max_bytes // 4]
        returned = len(output)
        next_offset = offset + returned if offset + returned < total else None
        omitted = []
    else:
        if offset:
            raise ValueError("response_offset_limit")
        kind, total, output, returned, next_offset, omitted = "scalar", 1, deepcopy(node), 1, None, []
    return {"json_pointer": json_pointer, "kind": kind, "total": total,
            "offset": offset, "returned_count": returned, "next_offset": next_offset,
            "value": output, "page_complete": not omitted,
            "complete": offset == 0 and next_offset is None and not omitted,
            "omitted_paths": list(dict.fromkeys(omitted))[:16]}

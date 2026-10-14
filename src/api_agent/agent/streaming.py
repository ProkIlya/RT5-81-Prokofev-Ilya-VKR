"""Собрать ограниченный SSE поток в решение без накопления сырых событий.

Транспортный объём и отдельные JSON events ограничены независимо от финального
message. Diagnostics содержат только счётчик событий; debug payload не становится
частью решения и не расходует его лимит узлов. [DONE]/finish проверяет LocalModel.
"""

import json
from ..bounded_json import validate_tree


def collect_stream(lines, on_token=None):
    parts, reasoning, calls = [], [], {}
    finish_reason = None
    saw_token = False
    size = event_count = 0
    for line in lines:
        size += len(line)
        if len(line) > 65536 or size > 1048576:
            raise ValueError("stream_size_limit")
        if not line.startswith(b"data: "):
            continue
        data = line[6:].strip()
        if data == b"[DONE]":
            break
        event = json.loads(data)
        validate_tree(event)
        choice = event["choices"][0]
        delta = choice.get("delta", {})
        part = delta.get("content")
        event_count += 1
        finish_reason = choice.get("finish_reason") or finish_reason
        token_piece = part or delta.get("reasoning_content") or delta.get("tool_calls")
        if token_piece and not saw_token:
            saw_token = True
            if on_token:
                on_token()
        if part:
            parts.append(part)
        if delta.get("reasoning_content"):
            reasoning.append(delta["reasoning_content"])
        for fragment in delta.get("tool_calls", []):
            index = fragment["index"]
            if type(index) is not int or not 0 <= index < 16:
                raise ValueError("invalid_tool_index")
            call = calls.setdefault(
                index, {"type": "function", "function": {"arguments": ""}}
            )
            if "id" in fragment:
                call["id"] = fragment["id"]
            function = fragment.get("function", {})
            if "name" in function:
                call["function"]["name"] = function["name"]
            call["function"]["arguments"] += function.get("arguments", "")
    message = {
        "role": "assistant",
        "content": "".join(parts),
        "reasoning_content": "".join(reasoning),
    }
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    return {
        "choices": [{"message": message, "finish_reason": finish_reason}],
        "_stream": {"event_count": event_count},
    }

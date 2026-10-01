import json


class InvalidAction(ValueError):
    pass


def collect_stream(lines, on_token=None):
    parts, reasoning, events, calls = [], [], [], {}
    finish_reason = None
    saw_token = False
    for line in lines:
        if not line.startswith(b"data: "):
            continue
        data = line[6:].strip()
        if data == b"[DONE]":
            break
        try:
            event = json.loads(data)
            choice = event["choices"][0]
            delta = choice.get("delta", {})
            part = delta.get("content")
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
            raise InvalidAction("malformed stream event") from error
        events.append(event)
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
            call = calls.setdefault(index, {"type": "function", "function": {"arguments": ""}})
            if "id" in fragment:
                call["id"] = fragment["id"]
            function = fragment.get("function", {})
            if "name" in function:
                call["function"]["name"] = function["name"]
            call["function"]["arguments"] += function.get("arguments", "")
    message = {"role": "assistant", "content": "".join(parts), "reasoning_content": "".join(reasoning)}
    if calls:
        message["tool_calls"] = [calls[index] for index in sorted(calls)]
    return {"choices": [{"message": message, "finish_reason": finish_reason}], "_events": events}


def parse_decision(response):
    try:
        choices = response["choices"]
        if len(choices) != 1:
            raise InvalidAction("expected one choice")
        message = choices[0]["message"]
        calls = message.get("tool_calls") or []
        if calls:
            if len(calls) != 1 or calls[0].get("type") != "function":
                raise InvalidAction("expected exactly one function call")
            function = calls[0]["function"]
            action = {"name": function["name"], "arguments": json.loads(function["arguments"])}
        else:
            action = json.loads(message["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as error:
        raise InvalidAction("malformed model response") from error
    return validate_action(action)


def make_decider(post_json, model, raw):
    def decide(messages, allowed):
        arguments = {
            "inspect_operation": '{"operation_id":"getPet"}',
            "finish_run": '{"summary":"short factual summary"}',
        }
        instruction = (
            "Return exactly one JSON object with keys name and arguments, no prose or markdown. "
            f"Allowed action names for this turn: {', '.join(allowed)}. "
            + " ".join(f"{name} arguments must be {arguments[name]}." for name in allowed)
            + " Do not invent observations."
        )
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": instruction}, *messages],
            "temperature": 0,
            "max_tokens": 128,
            "response_format": {"type": "json_object"},
        }
        response = post_json(payload)
        raw.append({"request": payload, "response": response})
        action = parse_decision(response)
        if action["name"] not in allowed:
            raise InvalidAction("action not offered in this turn")
        return action
    return decide


def run_native_round_trip(post_json, model, raw):
    tools = {
        "inspect_operation": {
            "type": "function", "function": {
                "name": "inspect_operation",
                "description": "Inspect the only permitted operation",
                "parameters": {"type": "object", "properties": {
                    "operation_id": {"type": "string", "enum": ["getPet"]}},
                    "required": ["operation_id"], "additionalProperties": False},
            },
        },
        "finish_run": {
            "type": "function", "function": {
                "name": "finish_run",
                "description": "Finish with a factual summary of the inspected operation",
                "parameters": {"type": "object", "properties": {"summary": {"type": "string"}},
                               "required": ["summary"], "additionalProperties": False},
            },
        },
    }
    messages = [
        {"role": "system", "content": "Inspect getPet using the offered tool, then finish with a factual summary. Do not invent observations."},
        {"role": "user", "content": "Inspect getPet now."},
    ]
    actions = []
    observation = {"operation_id": "getPet", "method": "GET", "path": "/pets/{id}", "responses": {"200": "pet JSON"}}
    for expected in ("inspect_operation", "finish_run"):
        payload = {
            "model": model, "messages": messages,
            "tools": [tools[expected]], "tool_choice": "required",
            "temperature": 0.0, "top_p": 0.95, "min_p": 0.0, "max_tokens": 384,
        }
        response = post_json(payload)
        raw.append({"request": payload, "response": response})
        action = parse_decision(response)
        try:
            call = response["choices"][0]["message"]["tool_calls"][0]
            call_id = call["id"]
        except (KeyError, IndexError, TypeError) as error:
            raise InvalidAction("native tool call required") from error
        if len(response["choices"][0]["message"]["tool_calls"]) != 1 or action["name"] != expected:
            raise InvalidAction("model selected an unoffered action")
        actions.append(action)
        if expected == "inspect_operation":
            messages = [*messages, {"role": "assistant", "content": "", "tool_calls": [call]},
                        {"role": "tool", "tool_call_id": call_id,
                         "content": json.dumps(observation, ensure_ascii=False, separators=(",", ":"))}]
    return {"actions": actions, "observation": observation}


def validate_action(action):
    if not isinstance(action, dict) or set(action) != {"name", "arguments"}:
        raise InvalidAction("action must have exactly name and arguments")
    name, arguments = action["name"], action["arguments"]
    if not isinstance(arguments, dict):
        raise InvalidAction("arguments must be an object")
    if name == "inspect_operation":
        if arguments != {"operation_id": "getPet"}:
            raise InvalidAction("operation is not in the S01 allowlist")
    elif name == "finish_run":
        if set(arguments) != {"summary"} or not isinstance(arguments["summary"], str) or not arguments["summary"].strip():
            raise InvalidAction("finish_run requires a nonempty summary")
    else:
        raise InvalidAction("unknown tool")
    return action


def run_round_trip(decide):
    messages = [
        {"role": "system", "content": "Inspect getPet before finishing. Use only the offered structured actions."},
        {"role": "user", "content": "Inspect the permitted getPet operation and summarize its expected response."},
    ]
    first = validate_action(decide(messages, ("inspect_operation",)))
    if first["name"] != "inspect_operation":
        raise InvalidAction("the first action must inspect getPet")
    observation = {"operation_id": "getPet", "method": "GET", "path": "/pets/{id}", "responses": {"200": "pet JSON"}}
    messages.append({"role": "user", "content": "Fake-tool observation: " + json.dumps(observation, ensure_ascii=False)})
    second = validate_action(decide(messages, ("finish_run",)))
    if second["name"] != "finish_run":
        raise InvalidAction("the second action must finish")
    return {"actions": [first, second], "observation": observation}

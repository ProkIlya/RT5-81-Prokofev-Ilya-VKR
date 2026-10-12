"""Контролируемые native решения для испытаний; не часть продукта."""

import json


def reply(name, arguments):
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "call-" + name,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(arguments),
                            },
                        }
                    ]
                }
            }
        ]
    }


class ObserverModel:
    """Одна цель; только observation определяет ветвь продолжения."""

    def __call__(self, payload):
        observations = [
            json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"
        ]
        observations = [
            (
                o.get("data", o)
                if o.get("data", o) is not None
                else {
                    "exchange_id": o["evidence_ids"][0],
                    "outcome": o["provenance"]["outcome"],
                }
            )
            for o in observations
            if o.get("data", o) is not None or o.get("evidence_ids")
        ]
        exchanges = [o for o in observations if "exchange_id" in o]
        if not exchanges:
            return reply("execute_operation", {"operation_id": "getPet"})
        if exchanges[-1]["outcome"] != "received" and not any(
            "operation" in o for o in observations
        ):
            return reply("inspect_operation", {"operation_id": "getPet"})
        return reply(
            "finish_run",
            {
                "summary": "Observed result",
                "evidence_ids": [exchanges[-1]["exchange_id"]],
            },
        )


def count_test_tokens(payload):
    """Явный double tokenizer только для изолированных тестов.

    Число не является замером реального GGUF и не используется production
    CLI. Реальные gates передают LocalModel.count_tokens и проверяют usage.
    """
    return 100

"""Локальный SSE adapter: фиксированный origin, конечный размер и deadline.

Модель не получает право выбрать endpoint. Адаптер прерывает блокирующий
socket I/O; после ответа loop повторно проверяет отмену и общий бюджет.
"""

import http.client
import json
import os
import time
from copy import deepcopy
from api_agent.transport.http import loopback_origin
from api_agent.transport.socket_deadline import SocketDeadline
from api_agent.agent.streaming import collect_stream
from api_agent.bounded_json import validate_tree, numeric_metadata


def configured_goal_model():
    """Явная конфигурация испытания; отсутствие ответа не переключает модель."""
    return os.environ.get('API_AGENT_MODEL_NAME', 'MiniCPM5-2B-Q4_K_M')


def qwen35_4b(model_name=None):
    """Семейство 4B не привязано к конкретной GGUF-квантизации."""
    name = model_name or configured_goal_model()
    return name == 'Qwen3.5-4B' or name.startswith('Qwen3.5-4B-')


def goal_sampling(model_name=None):
    """Профиль основной модели; resource caps задаются отдельно."""
    if qwen35_4b(model_name):
        return {'temperature': 0.0, 'top_p': 1.0, 'top_k': 0, 'min_p': 0.0,
                'presence_penalty': 0.0, 'repeat_penalty': 1.0}
    return {'temperature': 1.0, 'top_p': 0.95, 'min_p': 0.0}




class ModelError(RuntimeError):
    """Безопасный код отказа; текст исключения и фрагменты SSE наружу не идут."""

    def __init__(self, code, *, finish_reason=None, timing=None):
        super().__init__(code)
        self.code = code
        self.finish_reason = finish_reason
        self.timing = timing or {}


class ModelTimeout(TimeoutError):
    """Срок I/O адаптера; application отдельно проверяет cancel и deadline Run."""

    def __init__(self, timing=None):
        super().__init__("model_timeout")
        self.timing = timing or {}


class LocalModel:
    """Адаптер локального llama-server с ограничениями времени и размера SSE."""


    def __init__(self, base, *, deadline=None, timeout=90):
        self.base, self.port = loopback_origin(base)
        self.deadline = deadline
        self._run_deadline = None
        self.timeout = timeout

    def set_deadline(self, deadline):
        """Начать новый Run; его срок не расширяет доверенный deadline адаптера.

        Предыдущий Run не должен отравлять следующий истёкшим сроком. Общий
        deadline конфигурации сохраняется отдельно и остаётся верхней границей.
        Адаптер используется последовательно одним application loop.
        """
        self._run_deadline = deadline

    @staticmethod
    def prepare_payload(payload):
        """Locked runtime принимает строковый choice, а named object игнорирует.

        Сохраняем смысл forced function через одно опубликованное определение.
        Один и тот же payload используется для tokenizer и generation; никакого
        скрытого расширения tools или изменения аргументов модельного ответа.
        """
        prepared = deepcopy(payload)
        choice = prepared.get("tool_choice")
        if isinstance(choice, dict):
            if set(choice) != {"type", "function"} or choice["type"] != "function" or not isinstance(choice["function"], dict) or set(choice["function"]) != {"name"}:
                raise ValueError("invalid_named_tool_choice")
            selected = [t for t in prepared.get("tools", []) if t.get("function", {}).get("name") == choice["function"]["name"]]
            if len(selected) != 1:
                raise ValueError("unknown_named_tool_choice")
            prepared["tools"] = selected
            prepared["tool_choice"] = "required"
            prepared["messages"].append({"role":"user", "content":"For this turn call " + choice["function"]["name"] + " only. This is one step, not the whole task; other tool types become available on later turns. Return one tool call."})
        return prepared

    def _json_request(self, path, payload):
        """Вызвать только внутренний endpoint того же фиксированного runtime.

        Абсолютный deadline покрывает tokenizer так же, как generation.
        Redirect, proxy и произвольные URL не разрешены; ответ ограничен.
        """
        started = time.monotonic()
        deadline = min(
            v
            for v in (started + self.timeout, self.deadline, self._run_deadline)
            if v is not None
        )
        if deadline <= started:
            raise ModelTimeout()
        validate_tree(payload)
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=deadline - started
        )
        try:
            connection.connect()
            with SocketDeadline(connection.sock, deadline) as watchdog:
                connection.request(
                    "POST",
                    path,
                    json.dumps(payload).encode(),
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                watchdog.check()
                if response.status != 200:
                    raise ModelError("tokenizer_http_error")
                raw = response.read(1048577)
                watchdog.check()
                if len(raw) > 1048576:
                    raise ModelError("tokenizer_response_limit")
                result = json.loads(raw)
                validate_tree(result)
                return result
        except ModelError:
            raise
        except (OSError, http.client.HTTPException, ValueError, RecursionError):
            raise ModelError("tokenizer_error") from None
        finally:
            connection.close()

    def count_tokens(self, payload):
        """Считать полный фактический chat template, включая tools и роли.

        Используется tokenizer именно загруженного GGUF; оценка bytes/4 не
        применяется. BOS уже находится в шаблоне: add_special=false,
        parse_special=true совпадают с разбором сформированного prompt.
        Совпадение с actual prompt_tokens отдельно проверяет TokenBudget.
        """
        payload = self.prepare_payload(payload)
        data = {
            key: payload[key]
            for key in (
                "messages",
                "tools",
                "tool_choice",
                "chat_template_kwargs",
                "parallel_tool_calls",
                "response_format",
            )
            if key in payload
        }
        rendered = self._json_request("/apply-template", data)
        if not isinstance(rendered, dict) or not isinstance(
            rendered.get("prompt"), str
        ):
            raise ModelError("tokenizer_template_error")
        result = self._json_request(
            "/tokenize",
            {
                "content": rendered["prompt"],
                "add_special": False,
                "parse_special": True,
            },
        )
        tokens = result.get("tokens") if isinstance(result, dict) else None
        if (
            not isinstance(tokens, list)
            or not tokens
            or any(type(t) is not int or t < 0 for t in tokens)
        ):
            raise ModelError("tokenizer_invalid_tokens")
        return len(tokens)

    def __call__(self, payload):
        payload = self.prepare_payload(payload)
        started = time.monotonic()
        deadline = min(
            value
            for value in (started + self.timeout, self.deadline, self._run_deadline)
            if value is not None
        )
        remaining = deadline - started
        if remaining <= 0:
            raise ModelTimeout({"wall_seconds": time.monotonic() - started})
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=remaining
        )
        first_token = None

        def mark_token():
            nonlocal first_token
            first_token = time.monotonic() - started

        watchdog = None
        response = None

        def timing():
            return {
                "ttft_seconds": first_token,
                "wall_seconds": time.monotonic() - started,
            }

        try:
            connection.connect()
            # Абсолютный таймер покрывает отправку, медленные заголовки и SSE.
            with SocketDeadline(connection.sock, deadline) as watchdog:
                connection.request(
                    "POST",
                    "/v1/chat/completions",
                    json.dumps(
                        {
                            **payload,
                            "stream": True,
                            "stream_options": {"include_usage": True},
                        }
                    ).encode(),
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                watchdog.check()
                if response.status != 200:
                    raise ModelError("model_http_error", timing=timing())
                size = 0
                saw_done = False
                usage = None

                def lines():
                    # Проверка между строками дополняет таймер, который может
                    # прервать уже ожидающий readline() на уровне сокета.
                    nonlocal size, saw_done, usage
                    while True:
                        watchdog.check()
                        line = response.readline(65537)
                        watchdog.check()
                        if not line:
                            break
                        size += len(line)
                        if len(line) > 65536 or size > 1048576:
                            raise ModelError("model_response_limit", timing=timing())
                        # Ограничиваем каждое событие отдельно от собранного
                        # решения; сырые debug events не накапливаются.
                        if (
                            line.startswith(b"data: ")
                            and line.strip() != b"data: [DONE]"
                        ):
                            event = json.loads(line[6:])
                            validate_tree(event)
                            if event.get("usage") is not None:
                                raw_usage = event["usage"]
                                if not isinstance(raw_usage, dict):
                                    raise ValueError("invalid_usage")
                                usage = numeric_metadata(
                                    {
                                        k: raw_usage[k]
                                        for k in (
                                            "prompt_tokens",
                                            "completion_tokens",
                                            "total_tokens",
                                        )
                                        if k in raw_usage
                                    },
                                    (
                                        "prompt_tokens",
                                        "completion_tokens",
                                        "total_tokens",
                                    ),
                                )
                                if event.get("choices") == []:
                                    continue
                        if line.strip() == b"data: [DONE]":
                            saw_done = True
                        yield line

                result = collect_stream(lines(), mark_token)
                watchdog.check()
                # Валидные arguments ещё не означают полный ответ: ранний EOF
                # или length-stop не должны дать право исполнить часть решения.
                validate_tree(result)
                finish = result["choices"][0]["finish_reason"]
                if not saw_done:
                    raise ModelError(
                        "model_incomplete_stream",
                        finish_reason=(
                            finish
                            if finish in ("tool_calls", "length", "stop")
                            else None
                        ),
                        timing=timing(),
                    )
                if finish not in ("tool_calls", "stop") or (
                    finish == "stop"
                    and result["choices"][0]["message"].get("tool_calls")
                ):
                    raise ModelError(
                        (
                            "model_generation_limit"
                            if finish == "length"
                            else "model_incomplete_stream"
                        ),
                        finish_reason=finish if finish in ("length", "stop") else None,
                        timing=timing(),
                    )
                if usage is not None:
                    result["usage"] = usage
                result["_timing"] = timing()
                return result
        except ModelError:
            raise
        except OSError as error:
            if (watchdog and watchdog.expired) or time.monotonic() >= deadline:
                raise ModelTimeout(timing()) from error
            raise ModelError("model_transport_error", timing=timing()) from error
        except http.client.HTTPException as error:
            raise ModelError("model_transport_error", timing=timing()) from error
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            RecursionError,
            UnicodeError,
        ) as error:
            raise ModelError("model_malformed_stream", timing=timing()) from error
        finally:
            if response is not None:
                response.close()
            connection.close()

"""Локальный SSE adapter: фиксированный origin, конечный размер и deadline.

Модель не получает право выбрать endpoint. Адаптер прерывает блокирующий
socket I/O; после ответа loop повторно проверяет отмену и общий бюджет.
"""

import http.client
import json
import time
from ..first_http import loopback_origin
from ..socket_deadline import SocketDeadline
from experiments.local_model.spike import collect_stream


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

    def __call__(self, payload):
        started = time.monotonic()
        deadline = min(
            value
            for value in (started + self.timeout, self.deadline, self._run_deadline)
            if value is not None
        )
        remaining = deadline - started
        if remaining <= 0:
            raise TimeoutError("LLM/run time budget exhausted")
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=remaining
        )
        first_token = None

        def mark_token():
            nonlocal first_token
            first_token = time.monotonic() - started

        watchdog = None
        response = None
        try:
            connection.connect()
            # Абсолютный таймер покрывает отправку, медленные заголовки и SSE.
            with SocketDeadline(connection.sock, deadline) as watchdog:
                connection.request(
                    "POST",
                    "/v1/chat/completions",
                    json.dumps({**payload, "stream": True}).encode(),
                    {"Content-Type": "application/json"},
                )
                response = connection.getresponse()
                watchdog.check()
                if response.status != 200:
                    raise RuntimeError(f"local runtime HTTP {response.status}")
                size = 0
                saw_done = False

                def lines():
                    # Проверка между строками дополняет таймер, который может
                    # прервать уже ожидающий readline() на уровне сокета.
                    nonlocal size, saw_done
                    while True:
                        watchdog.check()
                        line = response.readline(65537)
                        watchdog.check()
                        if not line:
                            break
                        size += len(line)
                        if len(line) > 65536 or size > 1048576:
                            raise RuntimeError("LLM response budget exhausted")
                        if line.strip() == b"data: [DONE]":
                            saw_done = True
                        yield line

                result = collect_stream(lines(), mark_token)
                watchdog.check()
                # Валидные arguments ещё не означают полный ответ: ранний EOF
                # или length-stop не должны дать право исполнить часть решения.
                if (
                    not saw_done
                    or result["choices"][0]["finish_reason"] != "tool_calls"
                ):
                    raise RuntimeError("incomplete native tool stream")
                result["_timing"] = {
                    "ttft_seconds": first_token,
                    "wall_seconds": time.monotonic() - started,
                }
                return result
        except OSError as error:
            if (watchdog and watchdog.expired) or time.monotonic() >= deadline:
                raise TimeoutError("LLM/run time budget exhausted") from error
            raise
        finally:
            if response is not None:
                response.close()
            connection.close()

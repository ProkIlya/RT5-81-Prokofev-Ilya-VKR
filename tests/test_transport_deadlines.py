"""Регрессии R2: абсолютные сроки и полнота настоящего HTTP-ответа.

Локальный socket-сервер намеренно выдаёт байты медленно или закрывает тело.
Здесь проверяется транспорт, поэтому подмена HTTP-клиента скрыла бы дефект.
"""

import json
import socket
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from api_agent.first_http import Session, load_catalog, run_goal
from scripts.check_http_e2e import LocalModel

CATALOG = load_catalog(ROOT / "tests/fixtures/demo_openapi.json")
PET = b'{"id":1,"name":"Murka"}'


def serve_raw(response_parts, *, reset=False):
    """Вернуть порт и поток однократного управляемого локального сервера.

    Каждый элемент задаёт байты и задержку между ними; разрыв соединения
    после последнего элемента имитирует преждевременный EOF.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(2)

    def serve():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(2)
                request_deadline = time.monotonic() + 2

                def receive(size):
                    remaining = request_deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("fixture request deadline")
                    connection.settimeout(remaining)
                    return connection.recv(size)

                # HTTP POST может прийти несколькими recv. Сначала читаем весь
                # header, затем ровно Content-Length; иначе close даёт RST
                # вместо контролируемого EOF. Размер и ожидание конечны.
                request = bytearray()
                while b"\r\n\r\n" not in request:
                    chunk = receive(4096)
                    if not chunk:
                        return
                    request.extend(chunk)
                    if len(request) > 65536:
                        return
                header, body = bytes(request).split(b"\r\n\r\n", 1)
                length = 0
                for line in header.split(b"\r\n")[1:]:
                    key, _, value = line.partition(b":")
                    if key.lower() == b"content-length":
                        length = int(value.strip())
                if length < 0 or length > 1048576:
                    return
                remaining = length - len(body)
                while remaining > 0:
                    chunk = receive(min(4096, remaining))
                    if not chunk:
                        return
                    remaining -= len(chunk)
                if reset:
                    import struct

                    connection.setsockopt(
                        socket.SOL_SOCKET,
                        socket.SO_LINGER,
                        struct.pack("hh" if sys.platform == "win32" else "ii", 1, 0),
                    )
                    return
                for data, delay in response_parts:
                    for byte in data:
                        try:
                            connection.sendall(bytes([byte]))
                        except OSError:
                            return
                        if delay:
                            time.sleep(delay)
        except OSError:
            pass
        finally:
            listener.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return listener.getsockname()[1], thread


def reply(name, arguments):
    return {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "call-1",
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


class TransportRegressionTests(unittest.TestCase):
    def test_slow_headers_stop_at_absolute_run_deadline(self):
        header = (
            b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: 2\r\n\r\n"
        )
        port, _ = serve_raw([(header, 0.003), (b"{}", 0)])
        session = Session(
            f"http://127.0.0.1:{port}", CATALOG, timeout=1, run_seconds=0.06
        )
        started = time.monotonic()
        exchange = session.execute({"operation_id": "getPet"})
        self.assertLess(time.monotonic() - started, 0.20)
        self.assertEqual(exchange["outcome"], "time_limit")

    def test_slow_body_stops_at_absolute_run_deadline(self):
        header = (
            b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: 1000\r\n\r\n"
        )
        port, _ = serve_raw([(header, 0), (b"x" * 250, 0.003)])
        session = Session(
            f"http://127.0.0.1:{port}", CATALOG, timeout=1, run_seconds=0.06
        )
        started = time.monotonic()
        exchange = session.execute({"operation_id": "getPet"})
        self.assertLess(time.monotonic() - started, 0.20)
        self.assertEqual(exchange["outcome"], "time_limit")

    def test_truncated_body_cannot_complete_run(self):
        header = (
            b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: 100\r\n\r\n"
        )
        port, _ = serve_raw([(header + PET, 0)])
        session = Session(f"http://127.0.0.1:{port}", CATALOG)

        def model(payload):
            if payload["tools"][0]["function"]["name"] == "execute_operation":
                return reply("execute_operation", {"operation_id": "getPet"})
            observation = json.loads(payload["messages"][-1]["content"])
            return reply(
                "finish_run",
                {
                    "summary": "partial HTTP exchange",
                    "evidence_ids": [observation["exchange_id"]],
                },
            )

        trace = run_goal("Get pet 1", session, model)
        self.assertEqual(trace["exchanges"][0]["outcome"], "incomplete_response")
        self.assertIsNone(trace["exchanges"][0]["response"]["json"])
        self.assertEqual(trace["status"], "partial")

    def test_local_model_slow_headers_obey_run_deadline(self):
        header = (
            b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\n"
            b"Content-Length: 2\r\n\r\n"
        )
        port, _ = serve_raw([(header, 0.003), (b"{}", 0)])
        model = LocalModel(
            f"http://127.0.0.1:{port}", deadline=time.monotonic() + 0.06, timeout=1
        )
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            model({"model": "probe", "messages": []})
        self.assertLess(time.monotonic() - started, 0.20)

    def test_local_model_slow_sse_line_obeys_run_deadline(self):
        header = b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
        port, _ = serve_raw([(header, 0), (b"data: " + b"x" * 250, 0.003)])
        model = LocalModel(
            f"http://127.0.0.1:{port}", deadline=time.monotonic() + 0.06, timeout=1
        )
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            model({"model": "probe", "messages": []})
        self.assertLess(time.monotonic() - started, 0.20)


if __name__ == "__main__":
    unittest.main()

"""Сбрасываемый локальный Demo API: питомец, заявки и Bearer login/profile.

Режимы и reset задаёт владелец стенда, а не инструмент тестирующей модели.
Журнал не сохраняет тела credentials или значения Authorization.
"""

import argparse
import json
import threading
import secrets
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class DemoServer:
    """Сбрасываемый сервер и независимые наблюдения за сетевыми обращениями."""

    def __init__(self, port=0):
        self.journal = []
        self.reset_log = []
        self.mode = "good"
        self.redirect_target = ""
        self.pet = {"id": 1, "name": "Murka"}
        self.auth_token = secrets.token_urlsafe(32)
        self.user_id = secrets.randbelow(1000000000) + 1000000
        self.orders = {}
        self._data_lock = threading.Lock()
        self.connections = 0
        owner = self

        class Server(ThreadingHTTPServer):
            def get_request(self):
                # Считаем TCP-соединения, даже если клиент не дошёл до HTTP handler.
                # Это сильнее, чем отсутствие строки в журнале готовых запросов.
                result = super().get_request()
                owner.connections += 1
                return result

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send_json(self, status, data):
                body = json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (ConnectionError, OSError):
                    pass

            def do_POST(self):
                if self.path == '/users/logout':
                    matched = self.headers.get('Authorization') == 'Bearer ' + owner.auth_token
                    owner.journal.append({'method': 'POST', 'path': self.path, 'auth_matched': matched})
                    if matched:
                        owner.auth_token = secrets.token_urlsafe(32)
                    self.send_json(200 if matched else 401, {'logged_out': matched})
                    return
                if self.path == "/users/login":
                    try:
                        length = int(self.headers.get("Content-Length", "0"))
                        if not 1 <= length <= 2048:
                            raise ValueError()
                        data = json.loads(self.rfile.read(length))
                        valid = data == {
                            "login": "fixture",
                            "password": "fixture-password",
                        }
                    except (ValueError, TypeError):
                        valid = False
                    owner.journal.append({"method": "POST", "path": self.path})
                    if not valid:
                        self.send_json(401, {"error": "unauthorized"})
                        return
                    token = (
                        [owner.auth_token]
                        if owner.mode == "invalid_token_type"
                        else owner.auth_token
                    )
                    self.send_json(200, {"id": owner.user_id, "token": token})
                    return
                # Изменяющий fixture изолирован в Demo; ID выдаёт сервер.
                if self.path != "/orders":
                    self.send_json(404, {"error": "not_found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 1 <= length <= 2048:
                        raise ValueError()
                    data = json.loads(self.rfile.read(length))
                    if set(data) != {"name"} or data["name"] not in ("A", "B"):
                        raise ValueError()
                except (ValueError, TypeError):
                    self.send_json(400, {"error": "invalid_body"})
                    return
                with owner._data_lock:
                    identifier = secrets.randbelow(1000000000) + 1000000
                    while identifier in owner.orders:
                        identifier = secrets.randbelow(1000000000) + 1000000
                    order = {"id": identifier, "name": data["name"]}
                    owner.orders[identifier] = order
                    owner.journal.append(
                        {"method": "POST", "path": self.path, "created_id": identifier}
                    )
                if owner.mode == "timeout_after_commit":
                    time.sleep(6)
                self.send_json(201, order)

            def do_GET(self):
                if self.path == "/users/profile":
                    matched = (
                        self.headers.get("Authorization")
                        == "Bearer " + owner.auth_token
                    )
                    owner.journal.append(
                        {"method": "GET", "path": self.path, "auth_matched": matched}
                    )
                    if not matched:
                        self.send_json(401, {"error": "unauthorized"})
                    else:
                        self.send_json(
                            200,
                            {
                                "id": owner.user_id
                                + (1 if owner.mode == "state_id_mismatch" else 0),
                                "name": "Fixture user",
                            },
                        )
                    return
                if self.path.startswith("/orders/"):
                    owner.journal.append({"method": "GET", "path": self.path})
                    try:
                        identifier = int(self.path.removeprefix("/orders/"))
                    except ValueError:
                        identifier = -1
                    with owner._data_lock:
                        order = owner.orders.get(identifier)
                    if order is None:
                        self.send_json(404, {"error": "not_found"})
                    else:
                        answer = dict(order)
                        if owner.mode == "state_id_mismatch":
                            answer["id"] += 1
                        self.send_json(200, answer)
                    return
                # Журнал формируется сервером независимо от trace агента.
                owner.journal.append({"method": "GET", "path": self.path})
                status, headers = 200, {}
                content_type = "application/json"
                body = json.dumps(owner.pet).encode()
                if self.path != "/pets/1":
                    status, body = 404, b'{"error":"not_found"}'
                elif owner.mode == "redirect":
                    # Дефектные режимы включает стенд, а не инструмент модели.
                    status, body = 302, b"{}"
                    headers["Location"] = owner.redirect_target
                elif owner.mode == "oversize":
                    body = b"x" * 65537
                elif owner.mode == "not_found":
                    status, body = 404, b'{"error":"not_found"}'
                elif owner.mode == "schema_mismatch":
                    body = b'{"id":"wrong","name":"Murka"}'
                elif owner.mode == "missing_required":
                    body = b'{"id":1}'
                elif owner.mode == "invalid_content_type":
                    content_type = "text/plain"
                elif owner.mode == "invalid_json":
                    body = b"not-json"
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header(
                    "Content-Length",
                    str(len(body) + (10 if owner.mode == "incomplete_response" else 0)),
                )
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (ConnectionError, OSError):
                    pass

        self.server = Server(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.target = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = None

    def reset(self):
        """Восстановить известные данные и обнулить наблюдения перед сценарием."""
        with self._data_lock:
            self.orders.clear()
            self.auth_token = secrets.token_urlsafe(32)
            self.user_id = secrets.randbelow(1000000000) + 1000000
        self.journal.clear()
        self.connections = 0
        self.mode = "good"
        self.redirect_target = ""
        self.pet = {"id": 1, "name": "Murka"}
        self.reset_log.append({"result": "reset", "state": dict(self.pet)})

    def __enter__(self):
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()
        return self

    def __exit__(self, *args):
        # Контекстный менеджер освобождает порт и поток даже при ошибке gate.
        if self.thread:
            self.server.shutdown()
            self.thread.join()
        self.server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18082)
    args = parser.parse_args()
    with DemoServer(args.port) as demo:
        demo.reset()
        print(demo.target, flush=True)
        try:
            demo.thread.join()
        except KeyboardInterrupt:
            pass

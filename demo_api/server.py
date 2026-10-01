"""Локальный Demo API с одним GET; reset доступен только управляющему стенду."""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class DemoServer:
    """Сбрасываемый сервер и независимые наблюдения за сетевыми обращениями."""
    def __init__(self, port=0):
        self.journal = []
        self.reset_log = []
        self.mode = "good"
        self.redirect_target = ""
        self.pet = {"id": 1, "name": "Murka"}
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

            def do_GET(self):
                # Журнал формируется сервером независимо от trace агента.
                owner.journal.append({"method": "GET", "path": self.path})
                status, headers = 200, {}
                body = json.dumps(owner.pet).encode()
                if self.path != "/pets/1":
                    status, body = 404, b'{"error":"not_found"}'
                elif owner.mode == "redirect":
                    # Дефектные режимы включает стенд, а не инструмент модели.
                    status, body = 302, b'{}'
                    headers["Location"] = owner.redirect_target
                elif owner.mode == "oversize":
                    body = b'x' * 65537
                elif owner.mode == "invalid_json":
                    body = b'not-json'
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
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
        self.journal.clear()
        self.connections = 0
        self.mode = "good"
        self.redirect_target = ""
        self.pet = {"id": 1, "name": "Murka"}
        self.reset_log.append({"result": "reset", "state": dict(self.pet)})

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
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

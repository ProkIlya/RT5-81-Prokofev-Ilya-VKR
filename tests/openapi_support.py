"""Независимый HTTP-стенд для проверки загрузчика, без hooks в production parser."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class SpecServer:
    """TCP-счётчик и журнал принадлежат серверу, а не проверяемому клиенту."""

    def __init__(self, document, mode="good", location=""):
        self.document, self.mode, self.location = document, mode, location
        self.connections, self.journal = 0, []
        owner = self

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def get_request(self):
                result = super().get_request()
                owner.connections += 1
                return result

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                owner.journal.append(self.path)
                body = json.dumps(owner.document).encode()
                if owner.mode == "oversize":
                    body = b" " * 262145
                if owner.mode == "slow_headers":
                    time.sleep(0.25)
                try:
                    self.send_response(302 if owner.mode == "redirect" else 200)
                    self.send_header(
                        "Content-Type",
                        (
                            "text/plain"
                            if owner.mode == "wrong_type"
                            else "application/json"
                        ),
                    )
                    self.send_header(
                        "Content-Length",
                        str(len(body) + (10 if owner.mode == "truncated" else 0)),
                    )
                    if owner.mode == "redirect":
                        self.send_header("Location", owner.location)
                    self.end_headers()
                    if owner.mode == "slow_body":
                        time.sleep(0.25)
                    self.wfile.write(body)
                except OSError:
                    pass

        self.server = Server(("127.0.0.1", 0), Handler)
        self.target = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

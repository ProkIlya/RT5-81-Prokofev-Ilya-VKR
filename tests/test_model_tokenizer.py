"""Локальный tokenizer adapter: реальные HTTP endpoints без LLM inference."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import sys, threading, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]
from api_agent.agent.model import LocalModel
from test_transport_deadlines import serve_raw


class TokenizerTests(unittest.TestCase):
    def test_preflight_applies_tools_template_and_parses_special_tokens(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, data))
                output = (
                    {"prompt": "<s>rendered-tools"}
                    if self.path == "/apply-template"
                    else {"tokens": [1, 2, 3]}
                )
                body = json.dumps(output).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        payload = {
            "messages": [{"role": "user", "content": "check"}],
            "tools": [
                {"type": "function", "function": {"name": "inspect", "parameters": {}}}
            ],
            "tool_choice": "required",
            "chat_template_kwargs": {"enable_thinking": False},
            "parallel_tool_calls": False,
        }
        model = LocalModel("http://127.0.0.1:" + str(server.server_port))
        self.assertEqual(model.count_tokens(payload), 3)
        self.assertEqual(requests[0][0], "/apply-template")
        self.assertEqual(requests[0][1]["tools"], payload["tools"])
        self.assertEqual(
            requests[0][1].get("chat_template_kwargs"), payload["chat_template_kwargs"]
        )
        self.assertIs(requests[0][1].get("parallel_tool_calls"), False)
        self.assertEqual(
            requests[1],
            (
                "/tokenize",
                {
                    "content": "<s>rendered-tools",
                    "add_special": False,
                    "parse_special": True,
                },
            ),
        )

    def test_usage_only_sse_event_is_accounted(self):
        events = [
            {"choices": [{"finish_reason": "tool_calls", "delta": {}}]},
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
            },
        ]
        body = (
            b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)
            + b"data: [DONE]\n\n"
        )
        port, thread = serve_raw([(b"HTTP/1.0 200 OK\r\n\r\n" + body, 0)])
        result = LocalModel("http://127.0.0.1:" + str(port), timeout=2)(
            {"messages": []}
        )
        thread.join(3)
        self.assertEqual(
            result["usage"],
            {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )

    def test_complete_stop_without_call_reaches_empty_counter(self):
        body = b'data: {"choices":[{"finish_reason":"stop","delta":{"content":""}}]}\n\ndata: [DONE]\n\n'
        port, thread = serve_raw([(b"HTTP/1.0 200 OK\r\n\r\n" + body, 0)])
        result = LocalModel("http://127.0.0.1:" + str(port), timeout=2)(
            {"messages": []}
        )
        thread.join(3)
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")

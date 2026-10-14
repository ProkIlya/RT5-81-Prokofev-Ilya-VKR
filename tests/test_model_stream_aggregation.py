"""Разделить транспортные SSE события и размер окончательного решения."""

import sys, json, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from api_agent.agent.model import LocalModel, ModelError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading


def stream(finish="tool_calls", done=True):
    args = json.dumps({"summary": "x" * 400, "evidence_ids": ["exchange-own"]})
    deltas = [
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call-own",
                    "type": "function",
                    "function": {"name": "finish_run", "arguments": ""},
                }
            ]
        }
    ]
    if finish == "length":
        deltas = [{"content": "x"}] * 510
    else:
        deltas += [
            {"tool_calls": [{"index": 0, "function": {"arguments": char}}]}
            for char in args
        ]
    events = [
        {
            "id": "model-reply",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "local",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        for delta in deltas
    ]
    events.append(
        {
            "id": "model-reply",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": "local",
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
        }
    )
    return b"".join(("data: " + json.dumps(e) + "\n\n").encode() for e in events) + (
        b"data: [DONE]\n\n" if done else b""
    )


class StreamAggregationTests(unittest.TestCase):
    def request(self, body):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            return LocalModel(f"http://127.0.0.1:{server.server_port}", timeout=3)(
                {"messages": []}
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_fragmented_valid_native_call_is_not_rejected_by_debug_event_nodes(self):
        result = self.request(stream())
        call = result["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(
            json.loads(call["function"]["arguments"]),
            {"summary": "x" * 400, "evidence_ids": ["exchange-own"]},
        )
        self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")

    def test_long_complete_length_stream_is_generation_limit(self):
        with self.assertRaises(ModelError) as raised:
            self.request(stream("length"))
        self.assertEqual(raised.exception.code, "model_generation_limit")
        self.assertEqual(raised.exception.finish_reason, "length")

    def test_fragmented_call_without_done_is_incomplete(self):
        with self.assertRaises(ModelError) as raised:
            self.request(stream(done=False))
        self.assertEqual(raised.exception.code, "model_incomplete_stream")

    def test_malformed_sse_is_still_rejected(self):
        with self.assertRaises(ModelError) as raised:
            self.request(b"data: {bad}\n\ndata: [DONE]\n\n")
        self.assertEqual(raised.exception.code, "model_malformed_stream")

    def test_oversized_wire_data_is_not_unbounded(self):
        with self.assertRaises(ModelError) as raised:
            self.request(b"data: " + b"x" * 65537 + b"\n")
        self.assertEqual(raised.exception.code, "model_response_limit")

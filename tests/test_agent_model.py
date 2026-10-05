"""Общий локальный LLM adapter обязан сохранять сетевые ограничения S02."""

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))


class AgentModelTests(unittest.TestCase):
    def test_new_run_deadline_does_not_inherit_expired_previous_run(self):
        from api_agent.agent.model import LocalModel
        from test_transport_deadlines import serve_raw

        body = b'data: {"choices":[{"finish_reason":"tool_calls","delta":{}}]}\n\ndata: [DONE]\n\n'
        port, thread = serve_raw([(b"HTTP/1.0 200 OK\r\n\r\n" + body, 0)])
        model = LocalModel(f"http://127.0.0.1:{port}", timeout=2)
        model.set_deadline(time.monotonic() - 1)
        with self.assertRaises(TimeoutError):
            model({"messages": []})
        model.set_deadline(time.monotonic() + 2)
        self.assertEqual(
            model({"messages": []})["choices"][0]["finish_reason"], "tool_calls"
        )
        thread.join(timeout=3)

    def test_incomplete_or_truncated_native_stream_is_rejected(self):
        import json
        from api_agent.agent.model import LocalModel
        from test_transport_deadlines import serve_raw

        event = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "function": {
                                    "name": "execute_operation",
                                    "arguments": '{"operation_id":"getPet"}',
                                },
                            }
                        ]
                    },
                }
            ]
        }
        for finish, done in [
            ("tool_calls", False),
            ("length", True),
            ("tool_calls", True),
        ]:
            event["choices"][0]["finish_reason"] = finish
            body = b"data: " + json.dumps(event).encode() + b"\n\n"
            if done:
                body += b"data: [DONE]\n\n"
            header = b"HTTP/1.0 200 OK\r\nContent-Type: text/event-stream\r\n\r\n"
            port, thread = serve_raw([(header + body, 0)])
            model = LocalModel(f"http://127.0.0.1:{port}", timeout=2)
            if finish == "tool_calls" and done:
                result = model({"messages": []})
                self.assertEqual(result["choices"][0]["finish_reason"], "tool_calls")
            else:
                with self.assertRaises(RuntimeError):
                    model({"messages": []})
            thread.join(timeout=3)

    def test_shared_adapter_and_deadline_can_only_shorten(self):
        from api_agent.agent.model import LocalModel
        from scripts.check_http_e2e import LocalModel as LegacyModel

        self.assertIs(LocalModel, LegacyModel)
        model = LocalModel("http://127.0.0.1:18081", deadline=time.monotonic() - 1)
        original = model.deadline
        model.set_deadline(time.monotonic() + 100)
        self.assertEqual(model.deadline, original)
        with self.assertRaises(TimeoutError):
            model({"messages": []})

    def test_foreign_llm_target_rejected(self):
        from api_agent.agent.model import LocalModel
        from api_agent.first_http import PolicyDenied

        with self.assertRaises(PolicyDenied):
            LocalModel("http://example.com:80")

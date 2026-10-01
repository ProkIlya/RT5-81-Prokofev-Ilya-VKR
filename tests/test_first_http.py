"""Проверки реальной локальной сети и границ первого сквозного сценария.

Подмена используется только для решений LLM: сеть и Demo API настоящие.
Живое взаимодействие с моделью проверяется отдельным good gate.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from api_agent.first_http import PolicyDenied, Session, load_catalog, run_goal
from demo_api.server import DemoServer

FIXTURE = Path(__file__).parent / "fixtures" / "demo_openapi.json"


def reply(name, args):
    # Формируем ответ протокола runtime, чтобы отдельно испытать защиту tools.
    return {"choices": [{"message": {"tool_calls": [{"id": "call-" + name,
        "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}}]}


class FirstHttpTests(unittest.TestCase):
    def setUp(self):
        self.demo = DemoServer()
        self.demo.__enter__()
        self.addCleanup(self.demo.__exit__, None, None, None)
        self.catalog = load_catalog(FIXTURE)
        self.session = Session(self.demo.target, self.catalog)

    def test_real_http_and_reset(self):
        self.demo.reset()
        exchange = self.session.execute({"operation_id": "getPet"})
        self.assertEqual(exchange["response"]["json"], {"id": 1, "name": "Murka"})
        self.assertEqual(exchange["response"]["status"], 200)
        self.assertEqual([r["path"] for r in self.demo.journal], ["/pets/1"])
        self.assertEqual(self.demo.reset_log[-1]["result"], "reset")
        self.demo.reset()
        self.assertEqual(self.demo.journal, [])

    def test_unknown_operation_extra_url_and_free_text_never_reach_network(self):
        for action in ({"operation_id": "deletePet"}, {"operation_id": "getPet", "url": self.demo.target}, "getPet"):
            with self.subTest(action=action), self.assertRaises(PolicyDenied):
                self.session.execute(action)
        self.assertEqual(self.demo.journal, [])

    def test_foreign_origin_and_port_never_receive_request(self):
        with DemoServer() as foreign:
            for url in (foreign.target + "/pets/1", "http://127.0.0.2:12345/pets/1"):
                with self.assertRaises(PolicyDenied):
                    self.session.check_url(url)
            self.assertEqual(foreign.journal, [])

    def test_target_rejects_dns_credentials_path_and_non_http(self):
        for target in ("http://localhost:1234", "http://example.com:80", "https://127.0.0.1:80",
                       "http://u:p@127.0.0.1:80", self.demo.target + "/pets/1", self.demo.target + "?q=1"):
            with self.subTest(target=target), self.assertRaises(PolicyDenied):
                Session(target, self.catalog)

    def test_redirect_is_saved_but_not_followed(self):
        # Второй настоящий сервер выявляет обход policy через Location.
        with DemoServer() as foreign:
            self.demo.mode = "redirect"
            self.demo.redirect_target = foreign.target + "/pets/1"
            exchange = self.session.execute({"operation_id": "getPet"})
            self.assertEqual(exchange["outcome"], "redirect_denied")
            self.assertEqual(exchange["response"]["status"], 302)
            self.assertEqual(foreign.journal, [])

    def test_second_request_is_denied(self):
        self.session.execute({"operation_id": "getPet"})
        with self.assertRaises(PolicyDenied):
            self.session.execute({"operation_id": "getPet"})
        self.assertEqual(len(self.demo.journal), 1)

    def test_expired_run_never_connects(self):
        self.session.deadline = 0
        with self.assertRaises(PolicyDenied):
            self.session.execute({"operation_id": "getPet"})
        self.assertEqual(self.demo.connections, 0)

    def test_catalog_cannot_change_method_or_path(self):
        for catalog in ({"getPet": {"method": "POST", "path": "/pets/1"}},
                        {"getPet": {"method": "GET", "path": "//127.0.0.2:80/"}}):
            with self.assertRaises(PolicyDenied):
                Session(self.demo.target, catalog)

    def test_response_size_and_malformed_json(self):
        for mode, outcome in (("oversize", "response_limit"), ("invalid_json", "invalid_json")):
            self.demo.mode = mode
            session = Session(self.demo.target, self.catalog, max_response_bytes=1024)
            self.assertEqual(session.execute({"operation_id": "getPet"})["outcome"], outcome)

    def test_transport_error_is_not_api_finding(self):
        with DemoServer() as stopped:
            target = stopped.target
        exchange = Session(target, self.catalog).execute({"operation_id": "getPet"})
        self.assertEqual(exchange["outcome"], "transport_error")
        self.assertIsNone(exchange["response"])

    def test_finish_requires_this_runs_real_exchange(self):
        # Нельзя завершить Run со ссылкой на выдуманный или чужой exchange.
        with self.assertRaises(PolicyDenied):
            self.session.finish({"summary": "done", "evidence_ids": ["invented"]})
        exchange = self.session.execute({"operation_id": "getPet"})
        with self.assertRaises(PolicyDenied):
            self.session.finish({"summary": "done", "evidence_ids": ["invented"]})
        result = self.session.finish({"summary": "done", "evidence_ids": [exchange["id"]]})
        self.assertEqual(result["run_id"], self.session.run_id)

    def test_finish_rejects_extra_fields_and_other_runs_evidence(self):
        exchange = self.session.execute({"operation_id": "getPet"})
        for args in ({"summary": "done", "evidence_ids": [exchange["id"]], "url": "foreign"},
                     {"summary": "done", "evidence_ids": exchange["id"]},
                     {"summary": "", "evidence_ids": [exchange["id"]]}):
            with self.assertRaises(PolicyDenied):
                self.session.finish(args)
        other = Session(self.demo.target, self.catalog)
        other.execute({"operation_id": "getPet"})
        with self.assertRaises(PolicyDenied):
            other.finish({"summary": "done", "evidence_ids": [exchange["id"]]})

    def test_model_gets_actual_observation_and_finish_links_exchange(self):
        payloads = []
        def model(payload):
            payloads.append(payload)
            if len(payloads) == 1:
                return reply("execute_operation", {"operation_id": "getPet"})
            observation = json.loads(payload["messages"][-1]["content"])
            self.assertIn("Call finish_run", str(payload["messages"]))
            self.assertEqual(observation["response"]["json"]["name"], "Murka")
            return reply("finish_run", {"summary": "Received Murka", "evidence_ids": [observation["exchange_id"]]})
        trace = run_goal("Get pet 1", self.session, model)
        self.assertEqual(trace["status"], "completed")
        self.assertEqual(len(self.demo.journal), 1)
        self.assertEqual(trace["final"]["evidence_ids"], [trace["exchanges"][0]["id"]])

    def test_free_text_multiple_tools_and_wrong_tool_are_rejected(self):
        response = reply("execute_operation", {"operation_id": "getPet"})
        response["choices"][0]["message"]["tool_calls"] *= 2
        for bad in ({"choices": [{"message": {"content": "done"}}]}, response,
                    reply("fetch_url", {"url": self.demo.target})):
            with self.assertRaises(PolicyDenied):
                run_goal("Get pet", self.session, lambda payload: bad)
        self.assertEqual(self.demo.journal, [])

    def test_external_ref_or_server_fixture_rejected(self):
        import tempfile
        original = json.loads(FIXTURE.read_text())
        for extra in ({"servers": [{"url": "http://example.com"}]}, {"$ref": "http://example.com/spec"}):
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "spec.json"
                path.write_text(json.dumps({**original, **extra}))
                with self.assertRaises(PolicyDenied):
                    load_catalog(path)

    def test_bad_gate_returns_two_and_saves_independent_journal(self):
        # Проверяем внешний CLI и его exit code, а не только вызов функции.
        import os
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "scripts/check_stage.py", "S02", "bad"],
                env={**os.environ, "S02_EVIDENCE_DIR": directory}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            evidence = json.loads((Path(directory) / "bad.json").read_text())
            self.assertEqual(evidence["forbidden_connections"], 0)
            self.assertGreaterEqual(len(evidence["rejected_cases"]), 6)


if __name__ == "__main__":
    unittest.main()

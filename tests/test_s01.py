import unittest
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from experiments.local_model.spike import InvalidAction, collect_stream, make_decider, parse_decision, run_native_round_trip, run_round_trip, validate_action
from scripts.check_stage import peak_resources, runtime_url


class ActionValidationTests(unittest.TestCase):
    def test_accepts_only_known_inspect_operation(self):
        self.assertEqual(
            validate_action({"name": "inspect_operation", "arguments": {"operation_id": "getPet"}}),
            {"name": "inspect_operation", "arguments": {"operation_id": "getPet"}},
        )

    def test_rejects_unknown_tool(self):
        with self.assertRaises(InvalidAction):
            validate_action({"name": "fetch_url", "arguments": {"url": "https://example.com"}})

    def test_rejects_extra_or_unknown_arguments(self):
        for arguments in ({"operation_id": "getPet", "url": "https://example.com"},
                          {"operation_id": "deletePet"}, {}):
            with self.subTest(arguments=arguments), self.assertRaises(InvalidAction):
                validate_action({"name": "inspect_operation", "arguments": arguments})

    def test_rejects_free_text_and_malformed_action(self):
        for action in ("inspect getPet", {"name": "inspect_operation"},
                       {"name": "finish_run", "arguments": {"summary": 4}}):
            with self.subTest(action=action), self.assertRaises(InvalidAction):
                validate_action(action)


class RoundTripTests(unittest.TestCase):
    def test_observation_is_required_before_finish(self):
        prompts = []

        def decide(messages, allowed):
            prompts.append((list(messages), allowed))
            if len(prompts) == 1:
                return {"name": "inspect_operation", "arguments": {"operation_id": "getPet"}}
            self.assertIn("getPet", str(messages))
            self.assertIn("200", str(messages))
            return {"name": "finish_run", "arguments": {"summary": "GET /pets/{id} returns 200"}}

        trace = run_round_trip(decide)
        self.assertEqual([step["name"] for step in trace["actions"]], ["inspect_operation", "finish_run"])
        self.assertEqual(len(prompts), 2)

    def test_rejects_finish_before_inspection(self):
        with self.assertRaises(InvalidAction):
            run_round_trip(lambda messages, allowed: {
                "name": "finish_run", "arguments": {"summary": "done"}
            })


class ModelResponseTests(unittest.TestCase):
    def test_validated_json_fallback_accepts_only_one_action(self):
        response = {"choices": [{"message": {"content": '{"name":"inspect_operation","arguments":{"operation_id":"getPet"}}'}}]}
        self.assertEqual(parse_decision(response)["name"], "inspect_operation")


class GateTests(unittest.TestCase):
    def test_runtime_endpoint_is_loopback_only(self):
        self.assertEqual(runtime_url("http://127.0.0.1:18081"), "http://127.0.0.1:18081/v1/chat/completions")
        with self.assertRaises(ValueError):
            runtime_url("https://example.com")

    def test_resource_peaks_use_largest_observed_value(self):
        self.assertEqual(peak_resources([
            {"system_ram_used_bytes": 10, "server_rss_bytes": 4, "vram_used_mib": 0},
            {"system_ram_used_bytes": 12, "server_rss_bytes": 3, "vram_used_mib": 2},
        ]), {"system_ram_used_bytes": 12, "server_rss_bytes": 4, "vram_used_mib": 2})

    def test_bad_gate_rejects_fixture_with_exit_two_and_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, "S01_EVIDENCE_DIR": directory}
            result = subprocess.run([sys.executable, "scripts/check_stage.py", "S01", "bad"],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertTrue((Path(directory) / "bad.json").is_file())
            import json
            saved = json.loads((Path(directory) / "bad.json").read_text(encoding="utf-8"))
            self.assertEqual(len(saved["rejected_cases"]), 3)

    def test_native_round_trip_sends_observation_by_call_id(self):
        requests, raw = [], []
        replies = [
            {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-1", "type": "function", "function": {"name": "inspect_operation", "arguments": '{"operation_id":"getPet"}'}}
            ]}}]},
            {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "content": "", "tool_calls": [{
                "id": "call-2", "type": "function", "function": {"name": "finish_run", "arguments": '{"summary":"GET /pets/{id} returns 200"}'}}
            ]}}]},
        ]

        def post_json(payload):
            requests.append(payload)
            return replies[len(requests) - 1]

        trace = run_native_round_trip(post_json, "local-minicpm", raw)
        self.assertEqual([action["name"] for action in trace["actions"]], ["inspect_operation", "finish_run"])
        self.assertEqual(requests[1]["messages"][-1]["role"], "tool")
        self.assertEqual(requests[1]["messages"][-1]["tool_call_id"], "call-1")
        self.assertIn('"200"', requests[1]["messages"][-1]["content"])
        self.assertEqual(requests[1]["tools"][0]["function"]["name"], "finish_run")

    def test_native_round_trip_rejects_free_text_instead_of_tool_call(self):
        response = {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "I will inspect getPet"}}]}
        with self.assertRaises(InvalidAction):
            run_native_round_trip(lambda payload: response, "local-minicpm", [])

    def test_native_tool_call_is_parsed_and_validated(self):
        response = {"choices": [{"message": {"content": None, "tool_calls": [{
            "type": "function", "function": {"name": "inspect_operation", "arguments": '{"operation_id":"getPet"}'}}
        ]}}]}
        self.assertEqual(parse_decision(response)["arguments"], {"operation_id": "getPet"})

    def test_multiple_native_calls_are_rejected(self):
        call = {"type": "function", "function": {"name": "inspect_operation", "arguments": '{"operation_id":"getPet"}'}}
        response = {"choices": [{"message": {"tool_calls": [call, call]}}]}
        with self.assertRaises(InvalidAction):
            parse_decision(response)

    def test_unstructured_text_is_rejected(self):
        response = {"choices": [{"message": {"content": "I will inspect getPet"}}]}
        with self.assertRaises(InvalidAction):
            parse_decision(response)

    def test_decider_limits_offered_action_and_keeps_raw_response(self):
        payloads, raw = [], []

        def post_json(payload):
            payloads.append(payload)
            return {"choices": [{"message": {"content": '{"name":"inspect_operation","arguments":{"operation_id":"getPet"}}'}}]}

        decide = make_decider(post_json, "local-minicpm", raw)
        action = decide([{"role": "user", "content": "inspect getPet"}], ("inspect_operation",))
        self.assertEqual(action["name"], "inspect_operation")
        self.assertEqual(payloads[0]["model"], "local-minicpm")
        self.assertIn("inspect_operation", str(payloads[0]["messages"]))
        self.assertNotIn("finish_run", str(payloads[0]["messages"]))
        self.assertEqual(len(raw), 1)

    def test_stream_chunks_reassemble_json_decision(self):
        lines = [
            b'data: {"choices":[{"delta":{"content":"{\\"name\\":\\"inspect_"}}]}\n',
            b'data: {"choices":[{"delta":{"content":"operation\\",\\"arguments\\":{\\"operation_id\\":\\"getPet\\"}}"}}]}\n',
            b'data: [DONE]\n',
        ]
        response = collect_stream(lines)
        self.assertEqual(parse_decision(response)["name"], "inspect_operation")

    def test_stream_chunks_reassemble_native_call(self):
        lines = [
            b'data: {"choices":[{"finish_reason":null,"delta":{"reasoning_content":"Think"}}]}\n',
            b'data: {"choices":[{"finish_reason":null,"delta":{"tool_calls":[{"index":0,"id":"call-1","type":"function","function":{"name":"inspect_operation","arguments":"{"}}]}}]}\n',
            b'data: {"choices":[{"finish_reason":"tool_calls","delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"operation_id\\":\\"getPet\\"}"}}]}}]}\n',
            b'data: [DONE]\n',
        ]
        response = collect_stream(lines)
        self.assertEqual(parse_decision(response)["name"], "inspect_operation")
        self.assertEqual(response["choices"][0]["message"]["tool_calls"][0]["id"], "call-1")


if __name__ == "__main__":
    unittest.main()

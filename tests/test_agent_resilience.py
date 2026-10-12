"""S05b: доказать остановку до действия, бюджеты и сохранение прогресса.

Сеть и журнал приложения реальные. Искусственная модель заменяет внешний
недетерминированный выбор, а счётчик явно имитирует только tokenizer boundary.
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]
from api_agent.agent import StartAgentRun, Limits
from agent_support import ObserverModel, reply
from demo_api.server import DemoServer
from test_response_validation import catalog


def count_tokens(payload):
    return 100


class MeteredModel(ObserverModel):
    def __call__(self, payload):
        response = super().__call__(payload)
        response["usage"] = {
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
        }
        return response


class ResilienceTests(unittest.TestCase):
    def run_case(self, model, **kwargs):
        with DemoServer() as demo:
            app = StartAgentRun(demo.target, catalog(), **kwargs)
            run = app.start("p", "s", "m", "Check pet", model)
            return run, list(demo.journal)

    def test_empty_calls_have_separate_bounded_counter(self):
        run, journal = self.run_case(
            lambda p: {"choices": []}, token_counter=count_tokens
        )
        self.assertEqual(run["stop_reason"], "empty_limit")
        self.assertEqual(run["empty_steps"], 2)
        self.assertEqual(run["malformed_calls"], 0)
        self.assertEqual(journal, [])
        self.assertIn("report", run)

    def test_pre_effect_repeat_guard_stops_before_second_inspect(self):
        run, journal = self.run_case(
            lambda p: reply("inspect_operation", {"operation_id": "getPet"}),
            limits=Limits(max_repeats=1),
            token_counter=count_tokens,
        )
        self.assertEqual(run["stop_reason"], "repeated_action")
        self.assertEqual(
            len([e for e in run["events"] if e["kind"] == "tool_result"]), 1
        )
        self.assertEqual(journal, [])

    def test_token_preflight_blocks_model_and_http(self):
        run, journal = self.run_case(
            MeteredModel(),
            token_counter=count_tokens,
            limits=Limits(max_tokens_total=483),
        )
        self.assertEqual(run["stop_reason"], "token_limit")
        self.assertEqual(run["model_calls"], 0)
        self.assertEqual(journal, [])

    def test_missing_tokenizer_fails_closed_without_claiming_zero_usage(self):
        run, journal = self.run_case(ObserverModel())
        self.assertEqual(run["stop_reason"], "token_preflight_error")
        self.assertEqual(journal, [])
        self.assertEqual(run["resources"]["usage_status"], "unknown")

    def test_context_includes_tools_and_is_preflighted(self):
        payloads = []

        def counter(payload):
            payloads.append(payload)
            return 1700

        run, journal = self.run_case(MeteredModel(), token_counter=counter)
        self.assertEqual(run["stop_reason"], "context_limit")
        self.assertTrue(payloads[0]["tools"])
        self.assertEqual(journal, [])

    def test_unknown_usage_is_preserved_and_reserved_conservatively(self):
        run, _ = self.run_case(ObserverModel(), token_counter=count_tokens)
        self.assertIsNone(run["resources"]["output_tokens"])
        self.assertEqual(run["resources"]["usage_status"], "unknown")
        self.assertEqual(run["resources"]["tokens_upper_bound"], 968)
        self.assertEqual(run["resources"]["provider_fee"]["applicability"], "N/A")

    def test_actual_usage_over_reservation_stops_before_http(self):
        def model(payload):
            result = reply("execute_operation", {"operation_id": "getPet"})
            result["usage"] = {
                "prompt_tokens": 100,
                "completion_tokens": 400,
                "total_tokens": 500,
            }
            return result

        run, journal = self.run_case(model, token_counter=count_tokens)
        self.assertEqual(run["stop_reason"], "token_accounting_error")
        self.assertEqual(journal, [])

    def test_step_soft_warning_reaches_context_before_hard_stop(self):
        payloads = []

        def model(payload):
            payloads.append(payload)
            result = reply(
                "finish_run", {"summary": "no evidence", "evidence_ids": ["foreign"]}
            )
            return result

        run, journal = self.run_case(
            model,
            token_counter=count_tokens,
            limits=Limits(max_steps=5, malformed_retries=9, max_repeats=9),
        )
        self.assertEqual(run["stop_reason"], "step_limit")
        self.assertEqual(len(payloads), 5)
        self.assertTrue(
            any(
                "budget_warning:steps" in m["content"] for m in payloads[-1]["messages"]
            )
        )
        self.assertEqual(journal, [])

    def test_time_soft_warning_and_hard_stop(self):
        now = [0.0]
        payloads = []

        def model(payload):
            payloads.append(payload)
            now[0] = 85 if len(payloads) == 1 else 101
            return reply("inspect_operation", {"operation_id": "getPet"})

        run, journal = self.run_case(
            model,
            clock=lambda: now[0],
            token_counter=count_tokens,
            limits=Limits(max_seconds=100),
        )
        self.assertEqual(run["stop_reason"], "time_limit")
        self.assertTrue(
            any("budget_warning:time" in m["content"] for m in payloads[-1]["messages"])
        )
        self.assertEqual(journal, [])

    def test_token_soft_warning_before_next_generation(self):
        payloads = []

        def model(payload):
            payloads.append(payload)
            return reply("finish_run", {"summary": "bad", "evidence_ids": ["foreign"]})

        run, journal = self.run_case(
            model,
            token_counter=count_tokens,
            limits=Limits(max_tokens_total=1000, malformed_retries=9),
        )
        self.assertEqual(run["stop_reason"], "token_limit")
        self.assertIn("tokens", run["warnings"])
        self.assertTrue(
            any("budget_warning:tokens" in m["content"] for m in run["history"])
        )
        self.assertEqual(journal, [])

    def test_journal_failure_prevents_http_and_duplicate_replay(self):
        with DemoServer() as demo:

            def sink(event):
                if event["kind"] == "http_prepared":
                    raise OSError("CANARY")

            app = StartAgentRun(
                demo.target,
                catalog(),
                token_counter=count_tokens,
                event_sink=sink,
                secrets=("CANARY",),
            )
            run = app.start("p", "s", "m", "Check pet", MeteredModel())
            again = app.start("p", "s", "m", "other", MeteredModel())
            self.assertEqual(run["stop_reason"], "journal_error")
            self.assertEqual(demo.journal, [])
            self.assertEqual(run, again)
            self.assertNotIn("CANARY", json.dumps(run))

    def test_window_is_separate_and_native_pairs_preserved(self):
        payloads = []
        model = MeteredModel()

        def capture(payload):
            payloads.append(payload)
            result = model(payload)
            payload["messages"][0]["content"] = "tampered copy"
            return result

        run, _ = self.run_case(capture, token_counter=count_tokens)
        self.assertEqual(run["status"], "completed")
        self.assertNotIn("tampered copy", json.dumps(run["history"]))
        self.assertNotEqual(
            run["steps"][0]["window_hash"], run["steps"][1]["window_hash"]
        )
        self.assertEqual(run["steps"][0]["prefix_hash"], run["steps"][1]["prefix_hash"])
        self.assertEqual(run["goal"], "Check pet")
        self.assertEqual(run["stop_category"], "done")

    def test_jsonl_journal_contains_complete_ordered_safe_events(self):
        from api_agent.agent.resilience import JsonlJournal

        with tempfile.TemporaryDirectory() as directory:
            sink = JsonlJournal(Path(directory))
            run, journal = self.run_case(
                MeteredModel(), token_counter=count_tokens, event_sink=sink
            )
            path = Path(directory) / (run["run_id"] + ".jsonl")
            events = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(events, run["events"])
            self.assertEqual(
                [e["sequence"] for e in events], list(range(1, len(events) + 1))
            )
            self.assertEqual(events[-1]["kind"], "run_finished")
            self.assertTrue(any(e["kind"] == "http_prepared" for e in events))
            self.assertEqual(len(journal), 1)


if __name__ == "__main__":
    unittest.main()

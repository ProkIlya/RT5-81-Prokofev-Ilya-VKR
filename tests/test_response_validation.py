"""S05a: проверяем реальные ответы, основания и завершение независимо от LLM.

Удаление validator или доверие summary обязано сломать эти проверки.
Модель заменена только на границе выбора действия; HTTP и parser настоящие.
"""

import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]
from api_agent.agent import StartAgentRun
from api_agent.openapi import parse_document
from agent_support import ObserverModel, reply
from demo_api.server import DemoServer


def catalog():
    return parse_document(
        json.loads((ROOT / "tests/fixtures/demo_openapi.json").read_text())
    )


def user_condition(predicate, parameters, intent="positive"):
    return {
        "source": "USER_DEFINED",
        "predicate": predicate,
        "parameters": parameters,
        "intent": intent,
        "basis": {"message_id": "m"},
        "preconditions": "verified",
    }


from agent_support import count_test_tokens


class ValidationRunTests(unittest.TestCase):
    def run_case(
        self, *, pet=None, mode="good", condition=None, model=None, secrets=()
    ):
        with DemoServer() as demo:
            if pet is not None:
                demo.pet = pet
            demo.mode = mode
            snapshots = []
            kwargs = {}
            if condition is not None:
                kwargs["conditions"] = [condition]
            app = StartAgentRun(
                demo.target,
                catalog(),
                token_counter=count_test_tokens,
                secrets=secrets,
                trace_sink=snapshots.append,
            )
            run = app.start(
                "p", "s", "m", "Check pet", model or ObserverModel(), **kwargs
            )
            return run, snapshots, list(demo.journal)

    def test_schema_mismatch_has_path_even_when_model_says_correct(self):
        observer = ObserverModel()

        def model(payload):
            result = observer(payload)
            call = result["choices"][0]["message"]["tool_calls"][0]
            if call["function"]["name"] == "finish_run":
                args = json.loads(call["function"]["arguments"])
                args["summary"] = "Ошибок нет"
                call["function"]["arguments"] = json.dumps(args)
            return result

        run, _, journal = self.run_case(
            pet={"id": "wrong", "name": "Murka"}, model=model
        )
        result = run.get("validation", {})
        self.assertEqual(result.get("contract_verdict"), "mismatch")
        issue = next(i for i in result["issues"] if i["code"] == "schema_type")
        self.assertEqual(
            (issue["path"], issue["expected"], issue["actual"]),
            ("/id", "integer", "wrong"),
        )
        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(journal), 1)
        self.assertNotEqual(run["final"]["summary"], "Ошибок нет")

    def test_missing_required_is_not_accepted(self):
        run, _, _ = self.run_case(pet={"id": 1})
        self.assertEqual(run.get("validation", {}).get("contract_verdict"), "mismatch")
        self.assertTrue(
            any(
                i["code"] == "required" and i["path"] == "/name"
                for i in run["validation"]["issues"]
            )
        )

    def test_expected_negative_does_not_hide_independent_contract_mismatch(self):
        condition = user_condition("status_in", {"values": [404]}, "negative")
        run, snapshots, _ = self.run_case(mode="not_found", condition=condition)
        self.assertEqual(run["validation"]["intent_verdict"], "pass")
        self.assertEqual(run["validation"]["contract_verdict"], "mismatch")
        self.assertEqual(run["status"], "completed")
        before = next(r for r in snapshots if r["expectations"] and not r["exchanges"])
        self.assertEqual(before["expectations"], run["expectations"])

    def test_user_mismatch_is_not_contract_violation(self):
        condition = user_condition(
            "json_value_equals", {"path": "/name", "value": "Milk"}
        )
        run, _, _ = self.run_case(condition=condition)
        self.assertEqual(run["validation"]["contract_verdict"], "pass")
        self.assertEqual(run["validation"]["intent_verdict"], "fail")
        self.assertEqual(run["validation"]["confirmed_contract_violations"], 0)

    def test_model_hypothesis_never_confirms_defect_or_goal(self):
        condition = user_condition(
            "json_value_equals", {"path": "/name", "value": "Milk"}
        )
        condition.update(source="MODEL_HYPOTHESIS", basis=None)
        run, _, _ = self.run_case(condition=condition)
        self.assertEqual(run["validation"]["intent_verdict"], "needs_review")
        self.assertEqual(run["validation"]["confirmed_contract_violations"], 0)
        self.assertEqual(run["status"], "partial")

    def test_forged_basis_and_unknown_predicate_stop_before_http(self):
        for condition in [
            user_condition("eval", {"code": "1+1"}),
            {
                **user_condition("status_in", {"values": [200]}),
                "basis": {"message_id": "foreign"},
            },
        ]:
            with self.subTest(condition=condition):
                run, _, journal = self.run_case(condition=condition)
                self.assertNotEqual(run["status"], "completed")
                self.assertEqual(journal, [])
                self.assertEqual(run["model_calls"], 0)

    def test_unverified_positive_setup_is_not_api_defect(self):
        condition = user_condition("status_in", {"values": [200]})
        condition["preconditions"] = "unknown"
        run, _, _ = self.run_case(mode="not_found", condition=condition)
        self.assertEqual(run["validation"]["intent_verdict"], "setup_error")
        self.assertEqual(run["validation"]["confirmed_contract_violations"], 0)
        self.assertEqual(run["status"], "partial")

    def test_missing_validator_blocks_verified_finish(self):
        with patch(
            "api_agent.agent.runtime.validate_exchange", return_value=None, create=True
        ):
            run, _, _ = self.run_case()
        self.assertNotEqual(run["status"], "completed")

    def test_transport_failure_has_no_confirmed_contract_defect(self):
        from api_agent.agent import Limits

        app = StartAgentRun(
            "http://127.0.0.1:1",
            catalog(),
            token_counter=count_test_tokens,
            limits=Limits(max_seconds=3),
        )
        run = app.start("p", "s", "m", "Check pet", ObserverModel())
        self.assertEqual(
            run.get("validation", {}).get("contract_verdict"), "not_checked"
        )
        self.assertEqual(run["validation"]["confirmed_contract_violations"], 0)
        self.assertEqual(run["status"], "partial")

    def test_validation_runs_before_masking(self):
        condition = user_condition(
            "json_value_equals", {"path": "/name", "value": "CANARY"}
        )
        run, snapshots, _ = self.run_case(
            pet={"id": 1, "name": "CANARY"}, condition=condition, secrets=("CANARY",)
        )
        self.assertEqual(run["validation"]["intent_verdict"], "pass")
        self.assertNotIn("CANARY", json.dumps([run, snapshots]))

    def test_boolean_is_not_integer_and_nested_schema_is_checked(self):
        run, _, _ = self.run_case(pet={"id": True, "name": "Murka"})
        self.assertEqual(run.get("validation", {}).get("contract_verdict"), "mismatch")


if __name__ == "__main__":
    unittest.main()

"""Границы чисел, проекции схемы и доверия результату validator.

HTTP и OpenAPI настоящие. Подмена внутреннего компонента используется только
для проверки отказа интеграции: модель не получает доступ к этому объекту.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]
from api_agent.agent import StartAgentRun
from api_agent.openapi import parse_document
from api_agent.validation import validate_exchange
from agent_support import ObserverModel, count_test_tokens
from demo_api.server import DemoServer
from test_response_validation import catalog, user_condition

SCHEMA_ROOT = "/responses/200/content/application~1json/schema"


def schema_condition(cat, pointer, source="CONTRACT"):
    basis = {
        "pointer": pointer,
        "operation_hash": cat.operation_hashes["getPet"],
        "spec_hash": cat.spec_hash,
    }
    if source == "USER_DEFINED":
        basis = {"message_id": "m"}
    elif source == "MODEL_HYPOTHESIS":
        basis = None
    return {
        "source": source,
        "predicate": "supported_schema_types",
        "parameters": {"schema_pointer": pointer},
        "intent": "positive",
        "basis": basis,
        "preconditions": "verified",
    }


class NumericValidationTests(unittest.TestCase):
    def check_large_number(self, typ):
        doc = json.loads((ROOT / "tests/fixtures/demo_openapi.json").read_text())
        doc["paths"]["/pets/1"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["properties"]["id"]["type"] = typ
        cat = parse_document(doc)
        for exponent in (10, 308, 309, 400):
            with self.subTest(typ=typ, exponent=exponent), DemoServer() as demo:
                demo.pet = {"id": 10**exponent, "name": "Murka"}
                run = StartAgentRun(
                    demo.target, cat, token_counter=count_test_tokens
                ).start("p", "s", "m", "Check pet", ObserverModel())
                self.assertEqual(run["status"], "completed")
                self.assertEqual(run["validation"]["contract_verdict"], "pass")
                self.assertEqual(run["validation"]["intent_verdict"], "pass")
                self.assertEqual(run["requests"], len(demo.journal), 1)

    def test_large_integer_inside_json_limits_is_valid_integer(self):
        self.check_large_number("integer")

    def test_large_integer_inside_json_limits_is_valid_number(self):
        self.check_large_number("number")


class SchemaProjectionTests(unittest.TestCase):
    def test_nested_schema_is_rejected_before_http_for_every_source(self):
        cat = catalog()
        for source in ("CONTRACT", "USER_DEFINED", "MODEL_HYPOTHESIS"):
            with self.subTest(source=source), DemoServer() as demo:
                run = StartAgentRun(
                    demo.target, cat, token_counter=count_test_tokens
                ).start(
                    "p",
                    "s",
                    "m",
                    "Check pet",
                    ObserverModel(),
                    conditions=[
                        schema_condition(cat, SCHEMA_ROOT + "/properties/id", source)
                    ],
                )
                self.assertEqual(run["stop_reason"], "invalid_expectation")
                self.assertEqual(run["requests"], 0)
                self.assertEqual(demo.journal, [])
                self.assertEqual(run["model_calls"], 0)

    def test_response_schema_root_still_checks_whole_body(self):
        cat = catalog()
        with DemoServer() as demo:
            run = StartAgentRun(
                demo.target, cat, token_counter=count_test_tokens
            ).start(
                "p",
                "s",
                "m",
                "Check pet",
                ObserverModel(),
                conditions=[schema_condition(cat, SCHEMA_ROOT)],
            )
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["validation"]["contract_verdict"], "pass")
        self.assertEqual(run["validation"]["intent_verdict"], "pass")


class ValidatorBindingTests(unittest.TestCase):
    def run_fault(self, field, value):
        def fault(*args):
            result = validate_exchange(*args)
            result[field] = value
            return result

        with DemoServer() as demo, patch(
            "api_agent.agent.runtime.validate_exchange", side_effect=fault
        ):
            snapshots = []
            run = StartAgentRun(
                demo.target,
                catalog(),
                token_counter=count_test_tokens,
                trace_sink=snapshots.append,
            ).start("p", "s", "m", "Check pet", ObserverModel())
            self.assertEqual(run["requests"], 1)
            self.assertEqual(len(demo.journal), 1)
            self.assertEqual(run["model_calls"], 1)
            self.assertEqual(snapshots[-1], run)
        return run

    def test_every_foreign_binding_is_rejected_before_stamp_and_finish(self):
        for field in (
            "spec_hash",
            "operation_hash",
            "expectations_hash",
            "operation_id",
            "exchange_id",
            "run_id",
        ):
            with self.subTest(field=field):
                run = self.run_fault(field, "foreign")
                self.assertEqual(run["status"], "failed")
                self.assertEqual(run["stop_reason"], "validation_binding_error")
                self.assertIsNone(run["validation"])
                self.assertIsNone(run["final"])
                self.assertNotEqual(run["stop_category"], "done")

    def test_malformed_validator_shape_cannot_create_verified_completion(self):
        for field, value in (
            ("task_postcondition", "true"),
            ("complete_response", 1),
            ("schema_version", True),
            ("checks", {}),
            ("checks", [{}]),
            ("issues", None),
        ):
            with self.subTest(field=field):
                run = self.run_fault(field, value)
                self.assertEqual(run["status"], "failed")
                self.assertEqual(run["stop_reason"], "validation_binding_error")
                self.assertIsNone(run["validation"])

    def test_secret_condition_uses_private_hash_and_masked_report(self):
        with DemoServer() as demo:
            demo.pet = {"id": 1, "name": "SECRET_VALUE"}
            run = StartAgentRun(
                demo.target,
                catalog(),
                token_counter=count_test_tokens,
                secrets=("SECRET_VALUE",),
            ).start(
                "p",
                "s",
                "m",
                "Check pet",
                ObserverModel(),
                conditions=[
                    user_condition(
                        "json_value_equals", {"path": "/name", "value": "SECRET_VALUE"}
                    )
                ],
            )
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["validation"]["intent_verdict"], "pass")
        self.assertNotIn("SECRET_VALUE", json.dumps(run))

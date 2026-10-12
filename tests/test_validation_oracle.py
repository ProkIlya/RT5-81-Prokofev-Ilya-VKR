"""Дополнительные границы S05a: основание сравнения и профиль схемы."""

import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]
from api_agent.validation import schema_issues, prepare_expectations
from test_response_validation import catalog


class SchemaOracleTests(unittest.TestCase):
    def test_nullable_does_not_bypass_enum(self):
        issues = schema_issues(
            None, {"type": "string", "nullable": True, "enum": ["yes"]}
        )
        self.assertTrue(any(i["code"] == "enum" for i in issues))

    def test_nested_arrays_required_additional_and_escaped_path(self):
        schema = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"a/b": {"type": "integer"}},
                "required": ["a/b"],
                "additionalProperties": False,
            },
        }
        issues = schema_issues([{"a/b": "wrong", "extra": True}, {}], schema)
        self.assertEqual(
            {(i["code"], i["path"]) for i in issues},
            {
                ("schema_type", "/0/a~1b"),
                ("additional_property", "/0/extra"),
                ("required", "/1/a~1b"),
            },
        )

    def test_matching_pointer_does_not_authorize_wrong_expectation(self):
        cat = catalog()
        e = {
            "source": "CONTRACT",
            "intent": "positive",
            "predicate": "status_in",
            "parameters": {"values": [404]},
            "preconditions": "verified",
            "basis": {
                "pointer": "/responses/200",
                "operation_hash": cat.operation_hashes["getPet"],
                "spec_hash": cat.spec_hash,
            },
        }
        with self.assertRaises(ValueError):
            prepare_expectations(cat, [e], "m", "Check pet")

    def test_schema_predicate_cannot_reference_response_object(self):
        e = {
            "source": "USER_DEFINED",
            "intent": "positive",
            "predicate": "supported_schema_types",
            "parameters": {"schema_pointer": "/responses/200"},
            "preconditions": "verified",
            "basis": {"message_id": "m"},
        }
        with self.assertRaises(ValueError):
            prepare_expectations(catalog(), [e], "m", "Check pet")

    def test_unknown_condition_field_cannot_sneak_in_code(self):
        e = {
            "source": "MODEL_HYPOTHESIS",
            "intent": "positive",
            "predicate": "json_valid",
            "parameters": {},
            "preconditions": "verified",
            "basis": None,
            "code": "eval",
        }
        with self.assertRaises(ValueError):
            prepare_expectations(catalog(), [e], "m", "Check pet")


class BasisTypeTests(unittest.TestCase):
    def test_contract_enum_cannot_authorize_boolean_as_integer(self):
        from api_agent.openapi import parse_document

        doc = json.loads((ROOT / "tests/fixtures/demo_openapi.json").read_text())
        doc["paths"]["/pets/1"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["properties"]["id"]["enum"] = [1]
        cat = parse_document(doc)
        e = {
            "source": "CONTRACT",
            "intent": "positive",
            "predicate": "json_value_equals",
            "parameters": {"path": "/id", "value": True},
            "preconditions": "verified",
            "basis": {
                "pointer": "/responses/200/content/application~1json/schema/properties/id",
                "operation_hash": cat.operation_hashes["getPet"],
                "spec_hash": cat.spec_hash,
            },
        }
        with self.assertRaises(ValueError):
            prepare_expectations(cat, [e], "m", "Check pet")

"""R3: сохранённое основание неизменно, либо Run отказан до побочного действия."""

import hashlib
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]
from api_agent.agent import StartAgentRun
from api_agent.openapi import parse_document
from demo_api.server import DemoServer
from agent_support import ObserverModel

CANARY = "REVIEW-CONTRACT-CANARY"


def digest(value):
    """Независимая формула SHA-256: не используем canonical/Catalog для assertions."""
    data = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


class ContractHistoryTests(unittest.TestCase):
    def document(self):
        return json.loads((ROOT / "tests/fixtures/demo_openapi.json").read_text())

    def run_case(self, document, secrets=()):
        catalog = parse_document(document)
        with DemoServer() as demo:
            snapshots, payloads = [], []
            observer = ObserverModel()

            def model(payload):
                payloads.append(payload)
                return observer(payload)

            def sink(run):
                snapshots.append((run, len(demo.journal)))

            app = StartAgentRun(demo.target, catalog, trace_sink=sink, secrets=secrets)
            run = app.start("p", "s", "m", "Check pet", model)
            counts = (len(payloads), len(demo.journal))
            duplicate = app.start("p", "s", "m", "Changed goal", model)
            self.assertEqual(duplicate, run)
            self.assertEqual(counts, (len(payloads), len(demo.journal)))
            self.assertEqual(snapshots[-1][0], run)
            return run, snapshots, payloads, list(demo.journal)

    def assert_blocked(self, document, secret):
        run, snapshots, payloads, journal = self.run_case(document, [secret])
        self.assertEqual(run["status"], "blocked")
        self.assertEqual(run["stop_reason"], "contract_redaction_conflict")
        self.assertEqual(run["model_calls"], 0)
        self.assertEqual(run["requests"], 0)
        self.assertEqual(payloads, [])
        self.assertEqual(journal, [])
        for snapshot, count in snapshots:
            self.assertEqual(count, 0)
            self.assertIsNone(snapshot["contract"])
            self.assertIsNone(snapshot["spec_hash"])
            self.assertIsNone(snapshot["operation_hash"])
            self.assertEqual(snapshot["expectations"], [])
        # Проверяем строковые значения/ключи, а не цифры в сериализации других
        # типов: secret="200" не делает budget=12000 утечкой строки контракта.
        stack = [run, snapshots, payloads]
        while stack:
            value = stack.pop()
            if isinstance(value, str):
                self.assertNotIn(secret, value)
            elif isinstance(value, dict):
                stack.extend(value.keys())
                stack.extend(value.values())
            elif isinstance(value, (list, tuple)):
                stack.extend(value)

    def assert_history(self, run, snapshots):
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["requests"], 1)
        first_basis = next((r, count) for r, count in snapshots if r["expectations"])
        self.assertEqual(first_basis[1], 0)
        for snapshot, count in snapshots:
            self.assertEqual(snapshot["contract"], run["contract"])
            self.assertEqual(digest(snapshot["contract"]), snapshot["spec_hash"])
            operation = snapshot["contract"]["operations"]["getPet"]
            self.assertEqual(digest(operation), snapshot["operation_hash"])
            for expectation in snapshot["expectations"]:
                self.assertEqual(expectation["operation_id"], "getPet")
                self.assertEqual(expectation["source"], "CONTRACT")
                self.assertEqual(expectation["basis"]["pointer"], "/responses/200")
                self.assertEqual(
                    expectation["basis"]["spec_hash"], snapshot["spec_hash"]
                )
                self.assertEqual(
                    expectation["basis"]["operation_hash"], digest(operation)
                )
                self.assertIsInstance(operation["responses"]["200"], dict)

    def test_secret_enum_blocks_before_model_and_http(self):
        doc = self.document()
        doc["paths"]["/pets/1"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["properties"]["name"]["enum"] = [CANARY]
        self.assert_blocked(doc, CANARY)

    def test_secret_structural_fields_never_become_masked_contract(self):
        for field in (
            "property",
            "type",
            "status",
            "method",
            "path",
            "media",
            "operation_id",
            "parameter",
        ):
            with self.subTest(field=field):
                doc = self.document()
                operation = doc["paths"]["/pets/1"]["get"]
                schema = operation["responses"]["200"]["content"]["application/json"][
                    "schema"
                ]
                secret = CANARY
                if field == "property":
                    schema["properties"][CANARY] = schema["properties"].pop("name")
                    schema["required"] = ["id", CANARY]
                elif field == "type":
                    secret = "string"
                elif field == "status":
                    secret = "200"
                elif field == "method":
                    secret = "GET"
                elif field == "path":
                    secret = "/pets/1"
                elif field == "media":
                    secret = "application/json"
                else:
                    other = json.loads(json.dumps(operation))
                    other["operationId"] = (
                        CANARY if field == "operation_id" else "otherPet"
                    )
                    if field == "parameter":
                        other["parameters"] = [
                            {
                                "name": CANARY,
                                "in": "query",
                                "schema": {"type": "string"},
                            }
                        ]
                    doc["paths"]["/other"] = {"get": other}
                self.assert_blocked(doc, secret)

    def test_hash_and_pointer_identity_cannot_be_masked(self):
        doc = self.document()
        catalog = parse_document(doc)
        for secret in (
            catalog.spec_hash,
            catalog.operation_hashes["getPet"],
            "/responses/200",
            "CONTRACT",
        ):
            with self.subTest(secret_kind=len(secret)):
                self.assert_blocked(doc, secret)

    def test_unmatched_secret_preserves_hashes_basis_at_every_checkpoint(self):
        doc = self.document()
        doc["paths"]["/pets/1"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["properties"]["name"]["enum"] = [CANARY]
        for secrets in ((), ("UNRELATED-CONFIGURED-VALUE",)):
            run, snapshots, _, journal = self.run_case(doc, secrets)
            self.assert_history(run, snapshots)
            self.assertEqual(len(journal), 1)
            name = run["contract"]["operations"]["getPet"]["responses"]["200"][
                "content"
            ]["application/json"]["schema"]["properties"]["name"]
            self.assertEqual(name["enum"], [CANARY])

    def test_profile_annotations_removed_before_hash_allow_execution(self):
        doc = self.document()
        operation = doc["paths"]["/pets/1"]["get"]
        operation["summary"] = CANARY
        operation["responses"]["200"]["description"] = CANARY
        operation["responses"]["200"]["content"]["application/json"]["schema"][
            "properties"
        ]["name"]["description"] = CANARY
        run, snapshots, payloads, _ = self.run_case(doc, [CANARY])
        self.assert_history(run, snapshots)
        self.assertNotIn(CANARY, json.dumps([run, snapshots, payloads]))


if __name__ == "__main__":
    unittest.main()

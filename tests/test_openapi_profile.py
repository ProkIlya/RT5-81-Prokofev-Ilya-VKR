"""Тесты ловят потерю схемы/основания, молчаливый unsupported и расширение сети."""

import copy
import importlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def document():
    """Независимый контракт, не построенный parser."""
    return {
        "openapi": "3.0.3",
        "info": {"title": "Pets", "version": "1"},
        "paths": {
            "/pets/{id}": {
                "get": {
                    "operationId": "getPet",
                    "parameters": [
                        {
                            "name": "id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "pet",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "type": "object",
                                        "required": ["id"],
                                        "properties": {"id": {"type": "integer"}},
                                    }
                                }
                            },
                        }
                    },
                }
            }
        },
    }


class OpenApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = (
            importlib.import_module("api_agent.openapi")
            if importlib.util.find_spec("api_agent.openapi")
            else None
        )

    def setUp(self):
        self.assertIsNotNone(self.api, "S03 parser is not implemented")

    def reject(self, spec, code):
        with self.assertRaises(self.api.OpenApiError) as caught:
            self.api.parse_document(spec)
        self.assertEqual(caught.exception.code, code)

    def test_normalized_parameters_and_schema(self):
        catalog = self.api.parse_document(document())
        self.assertIn("parser_version", catalog.snapshot)
        op = catalog.operations["getPet"]
        self.assertEqual((op["method"], op["path"]), ("GET", "/pets/{id}"))
        self.assertEqual(op["parameters"][0]["schema"], {"type": "integer"})
        self.assertEqual(
            op["responses"]["200"]["content"]["application/json"]["schema"]["required"],
            ["id"],
        )

    def test_hash_stable_on_description_but_changes_with_schema(self):
        first = self.api.parse_document(document())
        changed = document()
        changed["info"]["description"] = "token=CANARY"
        self.assertEqual(first.spec_hash, self.api.parse_document(changed).spec_hash)
        changed["paths"]["/pets/{id}"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["properties"]["id"]["type"] = "string"
        second = self.api.parse_document(changed)
        self.assertNotEqual(
            first.operation_hashes["getPet"], second.operation_hashes["getPet"]
        )
        self.assertNotEqual(first.spec_hash, second.spec_hash)

    def test_snapshot_redacted_detached_and_basis_checked(self):
        spec = document()
        spec["paths"]["/pets/{id}"]["get"]["description"] = "CANARY_SECRET"
        catalog = self.api.parse_document(spec)
        self.assertNotIn("CANARY_SECRET", json.dumps(catalog.snapshot))
        detached = catalog.snapshot
        detached["operations"].clear()
        digest = catalog.operation_hashes["getPet"]
        self.assertEqual(
            catalog.resolve_basis(
                "getPet", "/responses/200/content/application~1json/schema/type", digest
            ),
            "object",
        )
        for pointer, h in (("/responses/404", digest), ("/responses/200", "wrong")):
            with self.assertRaises(self.api.OpenApiError) as caught:
                catalog.resolve_basis("getPet", pointer, h)
            self.assertEqual(caught.exception.code, "invalid_basis")

    def test_local_refs_surrogate_and_versions(self):
        for version in ("3.0.3", "3.1.0", "3.1.1"):
            spec = document()
            spec["openapi"] = version
            del spec["paths"]["/pets/{id}"]["get"]["operationId"]
            spec["components"] = {"schemas": {"Id": {"type": "integer"}}}
            spec["paths"]["/pets/{id}"]["get"]["parameters"][0]["schema"] = {
                "$ref": "#/components/schemas/Id"
            }
            self.assertIn("GET /pets/{id}", self.api.parse_document(spec).operations)

    def test_unsupported_duplicates_external_cycles(self):
        cases = []
        spec = document()
        spec["servers"] = [{"url": "http://evil"}]
        cases.append((spec, "unsupported"))
        spec = document()
        spec["paths"]["/other"] = copy.deepcopy(spec["paths"]["/pets/{id}"])
        cases.append((spec, "duplicate_operation_id"))
        for ref, code in (
            ("http://127.0.0.1:1/spec", "external_ref"),
            ("#/components/schemas/Missing", "invalid_ref"),
        ):
            spec = document()
            spec["paths"]["/pets/{id}"]["get"]["parameters"][0]["schema"] = {
                "$ref": ref
            }
            cases.append((spec, code))
        spec = document()
        spec["components"] = {
            "schemas": {"Loop": {"$ref": "#/components/schemas/Loop"}}
        }
        cases.append((spec, "cyclic_ref"))
        spec = document()
        spec["paths"]["/pets/{id}"]["get"]["responses"]["200"]["headers"] = {}
        cases.append((spec, "unsupported"))
        for spec, code in cases:
            with self.subTest(code=code):
                self.reject(spec, code)

    def test_parameter_types_and_missing_placeholder(self):
        for key, value in (
            ("required", "yes"),
            ("required", False),
            ("name", "missing"),
        ):
            spec = document()
            spec["paths"]["/pets/{id}"]["get"]["parameters"][0][key] = value
            self.reject(spec, "invalid_parameter")
        spec = document()
        spec["paths"]["/pets/{id}"]["get"]["parameters"][0]["schema"] = {
            "type": "integer",
            "enum": [True],
        }
        self.reject(spec, "invalid_schema")

    def test_security_inheritance_and_sensitive_literal(self):
        spec = document()
        spec["components"] = {
            "securitySchemes": {"Auth": {"type": "http", "scheme": "bearer"}}
        }
        spec["security"] = [{"Auth": []}]
        self.assertEqual(
            self.api.parse_document(spec).operations["getPet"]["security"][0]["Auth"],
            {"type": "http", "scheme": "bearer"},
        )
        spec["paths"]["/pets/{id}"]["get"]["security"] = []
        self.assertEqual(
            self.api.parse_document(spec).operations["getPet"]["security"], []
        )
        spec["components"]["schemas"] = {
            "Credentials": {
                "type": "object",
                "properties": {"token": {"type": "string", "enum": ["CANARY"]}},
            }
        }
        self.reject(spec, "invalid_schema")

    def test_file_duplicate_keys_size_and_invalid_json(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "spec.json"
            for raw, code in (
                (b'{"openapi":"3.0.3","openapi":"3.1.0"}', "invalid_document"),
                (b"not json", "invalid_document"),
                (b" " * 262145, "limits"),
            ):
                path.write_bytes(raw)
                with self.assertRaises(self.api.OpenApiError) as caught:
                    self.api.load_source(path)
                self.assertEqual(caught.exception.code, code)
            path.write_text(json.dumps(document()), encoding="utf-8")
            self.assertIn("getPet", self.api.load_source(path).operations)

    def test_malformed_shapes_and_depth(self):
        bad = []
        spec = document()
        spec["openapi"] = None
        bad.append((spec, "unsupported_version"))
        spec = document()
        spec["paths"]["/pets/{id}"]["get"]["parameters"][0]["in"] = []
        bad.append((spec, "invalid_parameter"))
        spec = document()
        spec["openapi"] = "3.1.0"
        spec["paths"]["/pets/{id}"]["get"]["parameters"][0]["schema"] = {
            "type": ["null", "null"]
        }
        bad.append((spec, "invalid_schema"))
        spec = document()
        spec["paths"]["/bad path"] = spec["paths"].pop("/pets/{id}")
        bad.append((spec, "invalid_operation"))
        nested = {"type": "string"}
        for _ in range(34):
            nested = {"type": "array", "items": nested}
        spec = document()
        spec["components"] = {"schemas": {"Deep": nested}}
        bad.append((spec, "limits"))
        for spec, code in bad:
            with self.subTest(code=code):
                self.reject(spec, code)

    def test_ambiguous_paths_and_reserved_headers(self):
        spec = document()
        spec["paths"]["/pets/{other}"] = copy.deepcopy(spec["paths"]["/pets/{id}"])
        spec["paths"]["/pets/{other}"]["get"]["operationId"] = "otherPet"
        spec["paths"]["/pets/{other}"]["get"]["parameters"][0]["name"] = "other"
        self.reject(spec, "invalid_operation")
        spec = document()
        spec["paths"]["/pets/{id}"]["get"]["parameters"].append(
            {"name": "Authorization", "in": "header", "schema": {"type": "string"}}
        )
        self.reject(spec, "unsupported")

    def test_nullable_enum_and_parameter_override(self):
        for version, schema in (
            ("3.0.3", {"type": "string", "nullable": True, "enum": ["ok", None]}),
            ("3.1.0", {"type": ["null", "string"], "enum": [None, "ok"]}),
        ):
            spec = document()
            spec["openapi"] = version
            spec["paths"]["/pets/{id}"]["parameters"] = [
                {"name": "filter", "in": "query", "schema": {"type": "string"}}
            ]
            spec["paths"]["/pets/{id}"]["get"]["parameters"].append(
                {"name": "filter", "in": "query", "required": True, "schema": schema}
            )
            params = self.api.parse_document(spec).operations["getPet"]["parameters"]
            self.assertEqual(len(params), 2)
            self.assertTrue(params[1]["required"])
            self.assertEqual(params[1]["schema"]["enum"], ["ok", None])

    def test_components_of_all_supported_kinds_and_expansion_limit(self):
        spec = document()
        parameter = spec["paths"]["/pets/{id}"]["get"]["parameters"][0]
        response = spec["paths"]["/pets/{id}"]["get"]["responses"]["200"]
        spec["components"] = {
            "parameters": {"Id": parameter},
            "responses": {"Pet": response},
            "requestBodies": {
                "Pet": {"content": {"application/json": {"schema": {"type": "string"}}}}
            },
            "securitySchemes": {
                "Key": {"type": "apiKey", "in": "header", "name": "X-API-Key"}
            },
        }
        spec["paths"]["/pets/{id}"]["get"]["parameters"] = [
            {"$ref": "#/components/parameters/Id"}
        ]
        spec["paths"]["/pets/{id}"]["get"]["responses"]["200"] = {
            "$ref": "#/components/responses/Pet"
        }
        spec["paths"]["/pets/{id}"]["get"]["requestBody"] = {
            "$ref": "#/components/requestBodies/Pet"
        }
        spec["security"] = [{"Key": []}]
        catalog = self.api.parse_document(spec)
        self.assertEqual(
            catalog.operations["getPet"]["requestBody"]["content"]["application/json"][
                "schema"
            ]["type"],
            "string",
        )
        self.assertEqual(
            catalog.operations["getPet"]["security"][0]["Key"]["name"], "x-api-key"
        )
        schemas = {"Leaf": {"type": "string"}}
        previous = "Leaf"
        for i in range(14):
            name = "Node" + str(i)
            schemas[name] = {
                "type": "object",
                "properties": {
                    k: {"$ref": "#/components/schemas/" + previous}
                    for k in ("left", "right")
                },
            }
            previous = name
        spec["components"]["schemas"] = schemas
        self.reject(spec, "limits")


if __name__ == "__main__":
    unittest.main()

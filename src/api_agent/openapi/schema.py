"""Разбор и раскрытие поддержанных OpenAPI schemas."""
import json
import math

from api_agent.openapi.model import OpenApiError, canonical, pointer_part, pointer_get, object_keys, nonempty, sensitive, MAX_DEPTH, MAX_NODES, TYPES
from api_agent.openapi.model import _annotation

class SchemaParser:
    def walk(self, value, kind, path, stack=(), depth=0, secret=False):
        """Проверить boundary до рекурсии; раскрытая ссылка наследует secret scope."""
        self.nodes += 1
        if depth > MAX_DEPTH or self.nodes > MAX_NODES:
            raise OpenApiError("limits", path)
        if isinstance(value, dict) and "$ref" in value:
            object_keys(value, {"$ref"}, {"$ref"}, path)
            ref = value["$ref"]
            if not isinstance(ref, str):
                raise OpenApiError("invalid_ref", path)
            if not ref.startswith("#/"):
                raise OpenApiError("external_ref", path)
            # Указатель разрешает ровно declaration нужного типа, не соседний объект.
            parts = ref[2:].split("/")
            if len(parts) != 3 or parts[:2] != ["components", kind]:
                raise OpenApiError("invalid_ref", path)
            if ref in stack:
                raise OpenApiError("cyclic_ref", path)
            try:
                resolved = pointer_get(self.document, ref[1:])
            except (KeyError, IndexError, ValueError, TypeError):
                raise OpenApiError("invalid_ref", path) from None
            return self.walk(resolved, kind, path, (*stack, ref), depth + 1, secret)
        handlers = {
            "schemas": self.schema,
            "parameters": self.parameter,
            "requestBodies": self.body,
            "responses": self.response,
            "securitySchemes": self.security_scheme,
        }
        result = handlers[kind](value, path, stack, depth, secret)
        if isinstance(value.get('description'), str):
            text = value['description']
            self.annotations[path + '/description'] = _annotation(text)
        return result

    def schema(self, value, path, stack, depth, secret):
        object_keys(
            value,
            {
                "type",
                "properties",
                "required",
                "additionalProperties",
                "items",
                "enum",
                "nullable",
                "description",
            },
            {"type"},
            path,
            "invalid_schema",
        )
        typ = value["type"]
        nullable = False
        if isinstance(typ, list):
            if (
                not self.version.startswith("3.1.")
                or len(typ) != 2
                or typ.count("null") != 1
            ):
                raise OpenApiError("invalid_schema", path)
            typ = next(t for t in typ if t != "null")
            nullable = True
        if not isinstance(typ, str) or typ not in TYPES:
            raise OpenApiError("invalid_schema", path)
        if "nullable" in value:
            if not self.version.startswith("3.0."):
                raise OpenApiError("unsupported", path)
            if type(value["nullable"]) is not bool:
                raise OpenApiError("invalid_schema", path)
            nullable = value["nullable"]
        result = {
            "type": (
                [typ, "null"] if nullable and self.version.startswith("3.1.") else typ
            )
        }
        if nullable and self.version.startswith("3.0."):
            result["nullable"] = True
        if typ == "object":
            if "items" in value or "enum" in value:
                raise OpenApiError("invalid_schema", path)
            properties = value.get("properties", {})
            if not isinstance(properties, dict) or not all(
                nonempty(k) for k in properties
            ):
                raise OpenApiError("invalid_schema", path)
            result["properties"] = {
                k: self.walk(
                    v,
                    "schemas",
                    path + "/properties/" + pointer_part(k),
                    stack,
                    depth + 1,
                    secret or sensitive(k),
                )
                for k, v in properties.items()
            }
            required = value.get("required", [])
            if (
                not isinstance(required, list)
                or not all(isinstance(k, str) for k in required)
                or len(set(required)) != len(required)
                or not set(required) <= set(properties)
            ):
                raise OpenApiError("invalid_schema", path)
            if required:
                result["required"] = sorted(required)
            additional = value.get("additionalProperties", True)
            if type(additional) is not bool:
                raise OpenApiError("unsupported", path)
            result["additionalProperties"] = additional
        elif typ == "array":
            if "items" not in value or set(value) & {
                "properties",
                "required",
                "additionalProperties",
                "enum",
            }:
                raise OpenApiError("invalid_schema", path)
            result["items"] = self.walk(
                value["items"], "schemas", path + "/items", stack, depth + 1, secret
            )
        elif set(value) & {"properties", "required", "additionalProperties", "items"}:
            raise OpenApiError("invalid_schema", path)
        if "enum" in value:
            enum = value["enum"]
            if secret or not isinstance(enum, list) or not enum:
                raise OpenApiError("invalid_schema", path)
            matches = {
                "string": lambda x: isinstance(x, str),
                "integer": lambda x: type(x) is int,
                "number": lambda x: type(x) in (int, float) and math.isfinite(x),
                "boolean": lambda x: type(x) is bool,
            }
            if not all((x is None and nullable) or matches[typ](x) for x in enum):
                raise OpenApiError("invalid_schema", path)
            encoded = [canonical(x) for x in enum]
            if len(set(encoded)) != len(encoded):
                raise OpenApiError("invalid_schema", path)
            result["enum"] = [json.loads(x) for x in sorted(encoded)]
        return result

"""Проверка JSON по поддержанному профилю схем."""
import math
import re

from ..openapi.parser import canonical, pointer_part


def schema_issues(value, schema, path="", schema_path=""):
    """Проверить все ограничения профиля S03, включая nested/enum/null.

    bool в Python является подклассом int, но в JSON это отдельный тип.
    Отсутствие поля и явный null также различаются. Пути — JSON Pointer.
    """
    typ = schema["type"]
    types = typ if isinstance(typ, list) else [typ]
    nullable = "null" in types or schema.get("nullable", False)
    base = next((t for t in types if t != "null"), "null")
    if value is None and nullable:
        if "enum" in schema and None not in schema["enum"]:
            return [
                {
                    "code": "enum",
                    "path": path or "/",
                    "schema_path": schema_path,
                    "expected": schema["enum"],
                    "actual": None,
                }
            ]
        return []
    matches = {
        "null": False,
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": type(value) is int,
        # JSON integer конечен в пределах bounded_json; перевод в float
        # здесь недопустим: корректное большое целое вызовет OverflowError.
        "number": type(value) is int or (type(value) is float and math.isfinite(value)),
        "boolean": type(value) is bool,
    }

    def issue(code, expected, actual, data_path=path, declaration_path=schema_path):
        return {
            "code": code,
            "path": data_path or "/",
            "schema_path": declaration_path,
            "expected": expected,
            "actual": actual,
        }

    if not matches[base]:
        return [issue("schema_type", typ, value)]
    result = []
    # oneOf относится к внутренним tool schemas; внешний OpenAPI profile
    # по-прежнему не разрешает композиции. Не принимать смешение ветвей.
    if "oneOf" in schema:
        count = sum(not schema_issues(value, branch, path, schema_path + "/oneOf/" + str(index))
                    for index, branch in enumerate(schema["oneOf"]))
        if count != 1:
            result.append(issue("oneOf", 1, count))
    # Внутренние схемы tools могут задавать пределы. Parser OpenAPI
    # по-прежнему отклоняет эти расширения: профиль внешнего контракта не меняется.
    if base == "string":
        for key, rejected in (
            ("minLength", len(value) < schema.get("minLength", 0)),
            ("maxLength", len(value) > schema.get("maxLength", len(value))),
        ):
            if rejected:
                result.append(issue(key, schema[key], len(value)))
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            result.append(issue("pattern", schema["pattern"], value))
    if "enum" in schema and not any(
        canonical(value) == canonical(v) for v in schema["enum"]
    ):
        result.append(issue("enum", schema["enum"], value))
    if base == "object":
        for name in schema.get("required", []):
            if name not in value:
                result.append(
                    issue(
                        "required",
                        "present",
                        "missing",
                        path + "/" + pointer_part(name),
                        schema_path + "/required",
                    )
                )
        properties = schema.get("properties", {})
        for name, item in value.items():
            item_path = path + "/" + pointer_part(name)
            if name in properties:
                result.extend(
                    schema_issues(
                        item,
                        properties[name],
                        item_path,
                        schema_path + "/properties/" + pointer_part(name),
                    )
                )
            elif schema.get("additionalProperties") is False:
                result.append(
                    issue(
                        "additional_property",
                        "absent",
                        item,
                        item_path,
                        schema_path + "/additionalProperties",
                    )
                )
    elif base == "array":
        for key, rejected in (
            ("minItems", len(value) < schema.get("minItems", 0)),
            ("maxItems", len(value) > schema.get("maxItems", len(value))),
        ):
            if rejected:
                result.append(issue(key, schema[key], len(value)))
        for index, item in enumerate(value):
            result.extend(
                schema_issues(
                    item,
                    schema["items"],
                    path + "/" + str(index),
                    schema_path + "/items",
                )
            )
    return result

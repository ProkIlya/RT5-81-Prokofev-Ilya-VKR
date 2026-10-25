"""Фиксация и проверка оснований ожиданий до HTTP."""
from copy import deepcopy
import re
import uuid

from ..bounded_json import validate_tree
from ..openapi.parser import canonical

from .common import PREDICATES, SOURCES

def prepare_expectations(
    catalog, conditions, message_id, goal, *, operation_id="getPet", expected_status=200
):
    """Зафиксировать условия доверенной команды до HTTP.

    conditions передаёт application/UI, не tool модели. USER_DEFINED — явно
    нормализованное условие исходного Message; его исходный текст сохраняется.
    Автоматическое толкование произвольного естественного языка не заявляется.
    Неподдержанное или неоднозначное условие отклоняется до сетевого действия.
    """
    op_hash = catalog.operation_hashes[operation_id]
    if conditions is None:
        conditions = [
            {
                "source": "CONTRACT",
                "predicate": "status_in",
                "parameters": {"values": [expected_status]},
                "intent": "positive",
                "basis": {
                    "pointer": f"/responses/{expected_status}",
                    "operation_hash": op_hash,
                    "spec_hash": catalog.spec_hash,
                },
                "preconditions": "verified",
            }
        ]
    validate_tree(conditions, max_bytes=16384)
    if not isinstance(conditions, list) or not 1 <= len(conditions) <= 7:
        raise ValueError("invalid_conditions")
    prepared = []
    for descriptor in deepcopy(conditions):
        if not isinstance(descriptor, dict) or set(descriptor) != {
            "source",
            "predicate",
            "parameters",
            "intent",
            "basis",
            "preconditions",
        }:
            raise ValueError("invalid_condition")
        source, predicate, params = (
            descriptor["source"],
            descriptor["predicate"],
            descriptor["parameters"],
        )
        if (
            source not in SOURCES
            or predicate not in PREDICATES
            or descriptor["intent"] not in {"positive", "negative"}
            or descriptor["preconditions"] not in {"verified", "unknown"}
            or not isinstance(params, dict)
        ):
            raise ValueError("unsupported_condition")
        if predicate == "status_in":
            if (
                set(params) != {"values"}
                or not isinstance(params["values"], list)
                or not 1 <= len(params["values"]) <= 10
                or any(
                    type(v) is not int or not 100 <= v <= 599 for v in params["values"]
                )
            ):
                raise ValueError("invalid_status_condition")
        elif predicate == "json_value_equals":
            if set(params) != {"path", "value"} or not isinstance(params["path"], str):
                raise ValueError("invalid_value_condition")
            # Синтаксис pointer проверяется отдельно от наличия значения в ответе.
            if not params["path"].startswith("/") or len(params["path"]) > 500:
                raise ValueError("invalid_pointer")
            for part in params["path"][1:].split("/"):
                if re.search(r"~(?![01])", part):
                    raise ValueError("invalid_pointer")
        elif predicate == "json_collection_matches":
            if set(params) != {"path", "rows", "mode"} or not isinstance(params["mode"], str) or params["mode"] not in {"contains", "exact", "absent"} or not isinstance(params["path"], str) or len(params["path"]) > 500 or (params["path"] and not params["path"].startswith("/")):
                raise ValueError("invalid_collection_condition")
            rows = params["rows"]
            minimum_rows = 0 if params["mode"] == "exact" else 1
            if not isinstance(rows, list) or not minimum_rows <= len(rows) <= 16:
                raise ValueError("collection_condition_limit")
            for row in rows:
                if not isinstance(row, dict) or not 1 <= len(row) <= 16 or any(not isinstance(path, str) or not path.startswith("/") or len(path) > 500 or isinstance(value, (dict, list)) for path, value in row.items()):
                    raise ValueError("invalid_collection_row")
            for path in [params["path"]] + [path for row in rows for path in row]:
                if re.search(r"~(?![01])", path):
                    raise ValueError("invalid_pointer")
            if source == "CONTRACT":
                raise ValueError("business_condition_requires_user_basis")
        elif predicate == "required_fields_present":
            if (
                set(params) != {"fields"}
                or not isinstance(params["fields"], list)
                or not params["fields"]
                or len(params["fields"]) > 100
                or any(not isinstance(v, str) or not v for v in params["fields"])
            ):
                raise ValueError("invalid_required_condition")
        elif predicate == "supported_schema_types":
            if set(params) != {"schema_pointer"} or not isinstance(
                params["schema_pointer"], str
            ):
                raise ValueError("invalid_schema_condition")
            # Predicate сравнивает весь JSON ответа. Вложенная декларация
            # поля не обосновывает это сравнение; отдельного data path в S05 нет.
            response_roots = {
                "/responses/" + status + "/content/application~1json/schema"
                for status in catalog.operations[operation_id]["responses"]
            }
            if params["schema_pointer"] not in response_roots:
                raise ValueError("unsupported_schema_projection")
            schema = catalog.resolve_basis(
                operation_id, params["schema_pointer"], op_hash
            )
            if not isinstance(schema, dict) or "type" not in schema:
                raise ValueError("invalid_schema_basis")
        elif params:
            raise ValueError("unexpected_parameters")
        basis = descriptor["basis"]
        if source == "CONTRACT":
            if (
                not isinstance(basis, dict)
                or set(basis) != {"pointer", "operation_hash", "spec_hash"}
                or basis["spec_hash"] != catalog.spec_hash
                or basis["operation_hash"] != op_hash
            ):
                raise ValueError("invalid_basis")
            declaration = catalog.resolve_basis(operation_id, basis["pointer"], op_hash)
            pointer = basis["pointer"]
            # Наличие pointer недостаточно: декларация обязана обосновывать
            # именно это сравнение. Нельзя сослаться на 200 и ожидать 404.
            if predicate == "status_in":
                status_text = pointer.rsplit("/", 1)[-1]
                valid = (
                    pointer.startswith("/responses/")
                    and pointer.count("/") == 2
                    and status_text.isdigit()
                    and params["values"] == [int(status_text)]
                )
            elif predicate in {"content_type_is_json", "json_valid"}:
                valid = (
                    isinstance(declaration, dict)
                    and "application/json" in declaration
                    and pointer.endswith("/content")
                )
            elif predicate == "required_fields_present":
                valid = (
                    isinstance(declaration, dict)
                    and sorted(params["fields"])
                    == sorted(declaration.get("required", []))
                    and pointer.endswith("/schema")
                )
            elif predicate == "supported_schema_types":
                valid = (
                    pointer == params["schema_pointer"]
                    and isinstance(declaration, dict)
                    and "type" in declaration
                )
            else:
                # Равенство выводимо из singleton enum; свободная строка не
                # превращается в конкретное ожидаемое значение от имени API.
                expected_pointer = "/responses/200/content/application~1json/schema"
                for part in params["path"][1:].split("/"):
                    expected_pointer += "/properties/" + part
                valid = (
                    pointer == expected_pointer
                    and isinstance(declaration, dict)
                    and canonical(declaration.get("enum"))
                    == canonical([params["value"]])
                )
            if not valid:
                raise ValueError("basis_does_not_support_condition")
        elif source == "USER_DEFINED":
            if basis != {"message_id": message_id}:
                raise ValueError("invalid_user_basis")
            descriptor["original_message"] = {"message_id": message_id, "text": goal}
        elif basis is not None:
            raise ValueError("hypothesis_has_no_verified_basis")
        descriptor.update(id=str(uuid.uuid4()), operation_id=operation_id)
        if predicate == "status_in":
            descriptor["values"] = list(params["values"])
        prepared.append(descriptor)
    return prepared

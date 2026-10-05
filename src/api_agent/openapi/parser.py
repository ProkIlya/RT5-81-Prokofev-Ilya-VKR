"""Детерминированное преобразование ограниченного REST/JSON контракта.

Проверяются даже неиспользованные components: иначе unsupported мог бы скрыться
до следующей операции. Refs раскрываются без I/O и с отдельным stack циклов.
"""

from dataclasses import dataclass
import hashlib
import json
import math
import re

PROFILE_VERSION = "rest-json-v1"
PARSER_VERSION = "1"
MAX_BYTES = 262144
MAX_DEPTH = 32
MAX_NODES = 20000
METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}
KINDS = {"schemas", "parameters", "requestBodies", "responses", "securitySchemes"}
TYPES = {"object", "array", "string", "integer", "number", "boolean"}


class OpenApiError(ValueError):
    """Безопасная диагностика: никаких значений, body или exception исходного I/O."""

    def __init__(self, code, pointer=""):
        self.code, self.pointer = code, pointer
        super().__init__(f"{code} at {pointer or '/'}")


def canonical(value):
    """Одинаковые декларации имеют одинаковые bytes независимо от порядка keys."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def pointer_part(value):
    return value.replace("~", "~0").replace("/", "~1")


def pointer_get(value, pointer):
    """Закрытый JSON Pointer: неверные escapes и неканонические индексы запрещены."""
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise KeyError
    for part in pointer[1:].split("/"):
        if re.search(r"~(?![01])", part):
            raise KeyError
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not re.fullmatch(r"0|[1-9][0-9]*", part):
                raise KeyError
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise KeyError
    return value


@dataclass(frozen=True)
class Catalog:
    """Immutable bytes защищают основания истории от изменения выданного dict.

    Это снимок parser, не разрешение исполнения. HTTP policy принимает решение
    отдельно; наличие операции в каталоге не даёт модели прав на неё.
    """

    _snapshot: bytes

    @property
    def snapshot(self):
        return json.loads(self._snapshot)

    @property
    def operations(self):
        return self.snapshot["operations"]

    @property
    def spec_hash(self):
        return hashlib.sha256(self._snapshot).hexdigest()

    @property
    def operation_hashes(self):
        return {
            name: hashlib.sha256(canonical(op)).hexdigest()
            for name, op in self.operations.items()
        }

    def resolve_basis(self, operation_id, pointer, operation_hash):
        """Проверить версию и наличие поддержанного основания до создания expectation."""
        try:
            if self.operation_hashes[operation_id] != operation_hash:
                raise KeyError
            return pointer_get(self.operations[operation_id], pointer)
        except (KeyError, IndexError, ValueError, TypeError):
            raise OpenApiError("invalid_basis") from None


def object_keys(value, allowed, required, path, code="invalid_document"):
    """Unknown keys — unsupported; неверная форма — отдельная точная диагностика."""
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise OpenApiError(code, path)
    if set(value) - set(allowed):
        raise OpenApiError("unsupported", path)
    if set(required) - set(value):
        raise OpenApiError(code, path)
    for key in ("description", "summary"):
        if key in value and not isinstance(value[key], str):
            raise OpenApiError(code, path)


def nonempty(value):
    return (
        isinstance(value, str)
        and bool(value)
        and not any(ord(c) < 32 or ord(c) == 127 for c in value)
    )


def sensitive(value):
    """Консервативные имена credentials; произвольные неизвестные secrets не угадываются."""
    return any(
        word in re.sub(r"[^a-z0-9]", "", value.lower())
        for word in (
            "token",
            "password",
            "secret",
            "credential",
            "apikey",
            "authorization",
        )
    )


class Parser:
    """Один документ, ограниченный счётчик и refs только из его components."""

    def __init__(self, document):
        self.document = document
        self.nodes = 0
        self.version = document.get("openapi")
        self.components = document.get("components", {})

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
        return handlers[kind](value, path, stack, depth, secret)

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

    def parameter(self, value, path, stack, depth, secret):
        object_keys(
            value,
            {"name", "in", "required", "schema", "description"},
            {"name", "in", "schema"},
            path,
            "invalid_parameter",
        )
        name, location, required = (
            value["name"],
            value["in"],
            value.get("required", False),
        )
        if (
            not nonempty(name)
            or location not in ("path", "query", "header")
            or type(required) is not bool
            or (location == "path" and not required)
        ):
            raise OpenApiError("invalid_parameter", path)
        if location == "header":
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise OpenApiError("invalid_parameter", path)
            name = name.lower()
            # OpenAPI считает эти header parameters игнорируемыми; не превращаем
            # их в исполняемые входы. Authentication описывается securitySchemes.
            if name in {"authorization", "accept", "content-type"}:
                raise OpenApiError("unsupported", path)
        schema = self.walk(
            value["schema"],
            "schemas",
            path + "/schema",
            stack,
            depth + 1,
            sensitive(name),
        )
        typ = schema["type"]
        if (typ[0] if isinstance(typ, list) else typ) in ("object", "array"):
            raise OpenApiError("unsupported", path)
        return {"name": name, "in": location, "required": required, "schema": schema}

    def content(self, value, path, stack, depth):
        object_keys(value, {"application/json"}, {"application/json"}, path)
        media = value["application/json"]
        object_keys(media, {"schema"}, {"schema"}, path)
        return {
            "application/json": {
                "schema": self.walk(
                    media["schema"],
                    "schemas",
                    path + "/application~1json/schema",
                    stack,
                    depth + 1,
                )
            }
        }

    def body(self, value, path, stack, depth, secret):
        object_keys(value, {"required", "content", "description"}, {"content"}, path)
        required = value.get("required", False)
        if type(required) is not bool:
            raise OpenApiError("invalid_document", path)
        return {
            "required": required,
            "content": self.content(value["content"], path + "/content", stack, depth),
        }

    def response(self, value, path, stack, depth, secret):
        object_keys(value, {"description", "content"}, {"description"}, path)
        result = {}
        if "content" in value:
            result["content"] = self.content(
                value["content"], path + "/content", stack, depth
            )
        return result

    def security_scheme(self, value, path, stack, depth, secret):
        object_keys(
            value,
            {"type", "scheme", "in", "name", "description"},
            {"type"},
            path,
            "invalid_security",
        )
        if (
            value["type"] == "http"
            and set(value) <= {"type", "scheme", "description"}
            and value.get("scheme") in ("basic", "bearer")
        ):
            return {"type": "http", "scheme": value["scheme"]}
        if (
            value["type"] == "apiKey"
            and set(value) <= {"type", "in", "name", "description"}
            and value.get("in") == "header"
            and nonempty(value.get("name"))
            and re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value["name"])
        ):
            return {"type": "apiKey", "in": "header", "name": value["name"].lower()}
        raise OpenApiError("invalid_security", path)

    def security(self, value, path):
        if not isinstance(value, list):
            raise OpenApiError("invalid_security", path)
        result = []
        for item in value:
            if not isinstance(item, dict) or len(item) > 1:
                raise OpenApiError("unsupported", path)
            alternative = {}
            for name, scopes in item.items():
                if scopes != [] or name not in self.components.get(
                    "securitySchemes", {}
                ):
                    raise OpenApiError("invalid_security", path)
                alternative[name] = self.walk(
                    self.components["securitySchemes"][name], "securitySchemes", path
                )
            result.append(alternative)
        return sorted(result, key=canonical)

    def parameters(self, values, path):
        if not isinstance(values, list):
            raise OpenApiError("invalid_parameter", path)
        result = {}
        for index, value in enumerate(values):
            param = self.walk(value, "parameters", path + f"/{index}")
            key = (param["in"], param["name"])
            if key in result:
                raise OpenApiError("invalid_parameter", path)
            result[key] = param
        return result

    def parse(self):
        """Нормализовать целиком; ни одна неудачная операция не пропускается тихо."""
        doc = self.document
        object_keys(
            doc,
            {"openapi", "info", "paths", "components", "security"},
            {"openapi", "info", "paths"},
            "",
        )
        if self.version not in {"3.0.0", "3.0.1", "3.0.2", "3.0.3", "3.1.0", "3.1.1"}:
            raise OpenApiError("unsupported_version")
        object_keys(
            doc["info"],
            {"title", "version", "description"},
            {"title", "version"},
            "/info",
        )
        if not nonempty(doc["info"]["title"]) or not nonempty(doc["info"]["version"]):
            raise OpenApiError("invalid_document", "/info")
        object_keys(self.components, KINDS, set(), "/components")
        for kind, values in self.components.items():
            if not isinstance(values, dict):
                raise OpenApiError("invalid_document", "/components")
            for name, value in values.items():
                if not nonempty(name):
                    raise OpenApiError("invalid_document", "/components")
                self.walk(
                    value,
                    kind,
                    "/components/" + kind + "/" + pointer_part(name),
                    secret=sensitive(name),
                )
        inherited_security = self.security(doc.get("security", []), "/security")
        paths = doc["paths"]
        if not isinstance(paths, dict) or not paths:
            raise OpenApiError("invalid_operation", "/paths")
        operations = {}
        templates = set()
        for path, item in paths.items():
            p = "/paths/" + pointer_part(path)
            if (
                not nonempty(path)
                or not path.startswith("/")
                or any(c.isspace() for c in path)
                or any(c in path for c in ("%", "?", "#", "\\"))
                or "//" in path
                or any(seg in (".", "..") for seg in path.split("/"))
            ):
                raise OpenApiError("invalid_operation", p)
            placeholders = re.findall(r"\{([A-Za-z_][A-Za-z0-9_-]*)\}", path)
            stripped = re.sub(r"\{[A-Za-z_][A-Za-z0-9_-]*\}", "", path)
            if (
                "{" in stripped
                or "}" in stripped
                or len(set(placeholders)) != len(placeholders)
            ):
                raise OpenApiError("invalid_operation", p)
            template = re.sub(r"\{[A-Za-z_][A-Za-z0-9_-]*\}", "{}", path)
            if template in templates:
                raise OpenApiError("invalid_operation", p)
            templates.add(template)
            object_keys(
                item, METHODS | {"parameters", "summary", "description"}, set(), p
            )
            base_params = self.parameters(item.get("parameters", []), p + "/parameters")
            if not set(item) & METHODS:
                raise OpenApiError("invalid_operation", p)
            for method in sorted(set(item) & METHODS):
                value = item[method]
                op_path = p + "/" + method
                object_keys(
                    value,
                    {
                        "operationId",
                        "summary",
                        "description",
                        "parameters",
                        "requestBody",
                        "responses",
                        "security",
                    },
                    {"responses"},
                    op_path,
                )
                name = value.get("operationId", method.upper() + " " + path)
                if not nonempty(name):
                    raise OpenApiError("invalid_operation", op_path)
                if name in operations:
                    raise OpenApiError("duplicate_operation_id", op_path)
                params = {
                    **base_params,
                    **self.parameters(
                        value.get("parameters", []), op_path + "/parameters"
                    ),
                }
                if {k[1] for k in params if k[0] == "path"} != set(placeholders):
                    raise OpenApiError("invalid_parameter", op_path)
                responses = value["responses"]
                if not isinstance(responses, dict) or not responses:
                    raise OpenApiError("invalid_operation", op_path)
                if not all(
                    isinstance(code, str)
                    and (code == "default" or re.fullmatch(r"[1-5][0-9]{2}", code))
                    for code in responses
                ):
                    raise OpenApiError("invalid_operation", op_path)
                normalized = {
                    "method": method.upper(),
                    "path": path,
                    "parameters": [params[k] for k in sorted(params)],
                    "responses": {
                        code: self.walk(
                            response, "responses", op_path + "/responses/" + code
                        )
                        for code, response in responses.items()
                    },
                    "security": (
                        self.security(value["security"], op_path + "/security")
                        if "security" in value
                        else inherited_security
                    ),
                }
                if "requestBody" in value:
                    normalized["requestBody"] = self.walk(
                        value["requestBody"], "requestBodies", op_path + "/requestBody"
                    )
                operations[name] = normalized
                if len(operations) > 128:
                    raise OpenApiError("limits", "/paths")
        return Catalog(
            canonical(
                {
                    "profile_version": PROFILE_VERSION,
                    "parser_version": PARSER_VERSION,
                    "openapi": self.version,
                    "operations": operations,
                }
            )
        )


def parse_document(document):
    """API для уже декодированного документа; ограничение bytes действует и здесь."""
    if not isinstance(document, dict):
        raise OpenApiError("invalid_document")
    try:
        if len(canonical(document)) > MAX_BYTES:
            raise OpenApiError("limits")
        return Parser(document).parse()
    except OpenApiError:
        raise
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise OpenApiError("invalid_document") from None

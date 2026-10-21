"""Детерминированное преобразование ограниченного REST/JSON контракта.

Проверяются даже неиспользованные components: иначе unsupported мог бы скрыться
до следующей операции. Refs раскрываются без I/O и с отдельным stack циклов.
"""

import re

from .model import OpenApiError, canonical, pointer_part, pointer_get, Catalog, object_keys, nonempty, sensitive, PROFILE_VERSION, PARSER_VERSION, MAX_BYTES, MAX_DEPTH, MAX_NODES, METHODS, KINDS, TYPES
from .model import _annotation
from .schema import SchemaParser

class Parser(SchemaParser):
    """Один документ, ограниченный счётчик и refs только из его components."""

    def __init__(self, document):
        self.document = document
        self.nodes = 0
        self.version = document.get("openapi")
        self.components = document.get("components", {})
        self.annotations = {}

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
        search_metadata = {}
        documentation = {}
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
                summary = value.get("summary", "")
                if isinstance(summary, str):
                    search_metadata[name] = {"summary": " ".join(summary.split())[:160]}
                annotations = {key: val for key, val in self.annotations.items()
                    if key.startswith(op_path + '/') or key.startswith(p + '/parameters/')}
                for key in ('summary', 'description'):
                    if key in item: annotations[p + '/' + key] = _annotation(item[key])
                for key in ('summary', 'description'):
                    if key in value:
                        text = value[key]
                        annotations[op_path + '/' + key] = _annotation(text)
                documentation[name] = annotations
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
            ),
            canonical(search_metadata),
            canonical(documentation),
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

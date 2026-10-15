"""Проверка ограниченного REST/JSON ответа без полномочий модели.

На вход поступает полный ограниченный exchange до маскирования. Контракт
проверяется независимо от намерения теста. Указатель и hash связывают вывод
с сохранённым профилем S03; произвольные выражения и код не исполняются.
"""

from copy import deepcopy
import hashlib
import json
import math
import re
import uuid

from ..bounded_json import validate_tree
from ..openapi.parser import canonical, pointer_get, pointer_part

PREDICATES = {
    "status_in",
    "content_type_is_json",
    "json_valid",
    "required_fields_present",
    "supported_schema_types",
    "json_value_equals",
    "json_collection_matches",
}
SOURCES = {"CONTRACT", "USER_DEFINED", "MODEL_HYPOTHESIS"}


def fingerprint(value):
    """Канонический отпечаток защищает неизменные условия и результаты."""
    return hashlib.sha256(canonical(value)).hexdigest()


def display_value(value):
    """Ограничить диагностическое значение, сохранив сравнение на полном JSON.

    Большие значения не пересказываются и не получают сырого префикса:
    это сохраняет предел отчёта и не обходит последующее маскирование.
    """
    size = len(canonical(value))
    if size > 1024:
        return {"omitted": True, "bytes_total": size}
    return deepcopy(value)


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
            if not isinstance(rows, list) or not 1 <= len(rows) <= 16:
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


def schema_issues(value, schema, path="", schema_path=""):
    """Проверить все ограничения профиля S03, включая nested/enum/null.

    bool в Python является подклассом int, но в JSON это отдельный тип.
    Отсутствие поля и явный null также различаются. Пути — JSON Pointer.
    """
    typ = schema["type"]
    types = typ if isinstance(typ, list) else [typ]
    nullable = "null" in types or schema.get("nullable", False)
    base = next(t for t in types if t != "null")
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


def validate_exchange(catalog, exchange, expectations):
    """Проверить ответ до redaction и вернуть независимые contract/intent.

    Полное невалидное JSON — контрактный результат. Неполное/слишком большое
    тело и инфраструктурный отказ не дают права диагностировать отсутствующие
    поля. Сырые bytes и secrets не сохраняются; caller маскирует итог.
    """
    operation_id = exchange["operation_id"]
    outcome, response = exchange["outcome"], exchange["response"]
    complete = outcome in {"received", "invalid_json", "invalid_content_type"}
    issues = []
    contract = "not_checked"
    op = catalog.operations[operation_id]
    schema_path = None
    if complete:
        status = response["status"]
        declared = op["responses"].get(str(status), op["responses"].get("default"))
        if declared is None:
            issues.append(
                {
                    "code": "status",
                    "path": "/status",
                    "schema_path": "/responses",
                    "expected": sorted(op["responses"]),
                    "actual": status,
                }
            )
        else:
            response_key = str(status) if str(status) in op["responses"] else "default"
            content = declared.get("content")
            if content:
                media = response["content_type"].split(";")[0].strip().lower()
                if media != "application/json":
                    issues.append(
                        {
                            "code": "content_type",
                            "path": "/content_type",
                            "schema_path": "/responses/" + response_key + "/content",
                            "expected": "application/json",
                            "actual": media,
                        }
                    )
                if not response.get("json_valid", outcome == "received"):
                    issues.append(
                        {
                            "code": "json",
                            "path": "/",
                            "schema_path": "/responses/" + response_key + "/content",
                            "expected": "valid JSON",
                            "actual": "invalid JSON",
                        }
                    )
                else:
                    schema_path = (
                        "/responses/"
                        + response_key
                        + "/content/application~1json/schema"
                    )
                    schema = catalog.resolve_basis(
                        operation_id,
                        schema_path,
                        catalog.operation_hashes[operation_id],
                    )
                    issues.extend(
                        schema_issues(response["json"], schema, schema_path=schema_path)
                    )
        contract = "mismatch" if issues else "pass"
    checks = []
    for expectation in expectations:
        predicate, params, source = (
            expectation["predicate"],
            expectation["parameters"],
            expectation["source"],
        )
        observed = None
        details = {}
        from .selection_scope import scope_result
        selection = scope_result(expectation, exchange)
        if complete and (selection is None or selection['applicable']):
            if predicate == "status_in":
                observed = response["status"] in params["values"]
            elif predicate == "content_type_is_json":
                observed = (
                    response["content_type"].split(";")[0].strip().lower()
                    == "application/json"
                )
            elif predicate == "json_valid":
                observed = response.get("json_valid", outcome == "received")
            elif predicate == "required_fields_present":
                observed = (
                    response.get("json_valid", outcome == "received")
                    and isinstance(response["json"], dict)
                    and all(f in response["json"] for f in params["fields"])
                )
            elif predicate == "supported_schema_types":
                schema = catalog.resolve_basis(
                    operation_id,
                    params["schema_pointer"],
                    catalog.operation_hashes[operation_id],
                )
                observed = response.get(
                    "json_valid", outcome == "received"
                ) and not schema_issues(response["json"], schema)
            elif predicate == "json_value_equals":
                try:
                    actual = pointer_get(response["json"], params["path"])
                    observed = response.get(
                        "json_valid", outcome == "received"
                    ) and canonical(actual) == canonical(params["value"])
                    details = {
                        "path": params["path"],
                        "expected": display_value(params["value"]),
                        "actual": display_value(actual),
                        "actual_present": True,
                    }
                except (KeyError, IndexError, TypeError):
                    observed = False
                    details = {
                        "path": params["path"],
                        "expected": display_value(params["value"]),
                        "actual": None,
                        "actual_present": False,
                    }
            elif predicate == "json_collection_matches":
                try:
                    collection = response["json"] if params["path"] == "" else pointer_get(response["json"], params["path"])
                    matched = []
                    if isinstance(collection, list):
                        for template in params["rows"]:
                            candidates = []
                            for index, item in enumerate(collection):
                                try:
                                    if all(canonical(pointer_get(item, path)) == canonical(expected) for path, expected in template.items()):
                                        candidates.append(index)
                                except (KeyError, IndexError, TypeError):
                                    pass
                            matched.append(candidates)
                    observed = isinstance(collection, list) and len(matched) == len(params["rows"]) and all(len(indices) == 1 for indices in matched) and len({indices[0] for indices in matched if len(indices) == 1}) == len(params["rows"])
                    if params["mode"] == "exact":
                        observed = observed and len(collection) == len(params["rows"])
                    elif params["mode"] == "absent":
                        observed = isinstance(collection, list) and len(matched) == len(params["rows"]) and all(not indices for indices in matched)
                    details = {"path":params["path"], "expected":display_value(params["rows"]),
                               "actual":{"unique_match_counts":[len(v) for v in matched],"collection_size":len(collection) if isinstance(collection,list) else None}, "actual_present":True}
                except (KeyError, IndexError, TypeError):
                    observed = False
                    details = {"path":params["path"], "expected":display_value(params["rows"]), "actual":None,"actual_present":False}
        if (
            predicate
            in {
                "required_fields_present",
                "supported_schema_types",
                "json_value_equals",
                "json_collection_matches",
            }
            and complete
            and not response.get("json_valid", outcome == "received")
        ):
            observed = None
        if selection is not None:
            details['selection_scope'] = selection
        verdict = (
            "not_checked" if observed is None else ("pass" if observed else "fail")
        )
        if (
            expectation["preconditions"] != "verified"
            and expectation["intent"] == "positive"
        ):
            verdict = "setup_error"
        elif source == "MODEL_HYPOTHESIS":
            verdict = "needs_review"
        checks.append(
            {
                "expectation_id": expectation["id"],
                "source": source,
                "predicate": predicate,
                "verdict": verdict,
                "comparison": observed,
                "basis": deepcopy(expectation["basis"]),
                **details,
            }
        )
    verdicts = {check["verdict"] for check in checks}
    intent = next(
        (
            v
            for v in ("setup_error", "needs_review", "not_checked", "fail")
            if v in verdicts
        ),
        "pass",
    )
    confirmed = len(issues) if complete and intent != "setup_error" else 0
    issues_total = len(issues)
    # Вердикт и счётчик учитывают все нарушения; отображение имеет отдельный предел.
    displayed_issues = [
        {
            k: display_value(v) if k in {"actual", "expected"} else v
            for k, v in issue.items()
        }
        for issue in issues[:128]
    ]
    return {
        "schema_version": 1,
        "exchange_id": exchange["id"],
        "run_id": exchange["run_id"],
        "operation_id": exchange["operation_id"],
        "spec_hash": catalog.spec_hash,
        "operation_hash": catalog.operation_hashes[operation_id],
        "expectations_hash": fingerprint(expectations),
        "complete_response": complete,
        "contract_verdict": contract,
        "intent_verdict": intent,
        "checks": checks,
        "issues": displayed_issues,
        "issues_total": issues_total,
        "issue_details_truncated": issues_total > len(displayed_issues),
        "confirmed_contract_violations": confirmed,
        "task_postcondition": complete and bool(checks) and intent in {"pass", "fail"},
        "completion_scope": "supported_checks_only",
    }


def report(run):
    """Собрать человеческий итог из проверок, не из свободного текста LLM.

    Это передаваемая сводка; автоматическое восстановление и повтор эффектов
    здесь отсутствуют. Свободная цель не объявляется полностью формализованной.
    """
    result = deepcopy(run.get("validation") or {})
    intent_exchange = result.get('exchange_id')
    if run.get('chain_verdict') in {'pass','fail'} and any(
        c.get('selection_scope',{}).get('applicable') is False for c in result.get('checks',[])):
        from .selection_scope import applicable
        matched = next((v for v in reversed(run.get('validations',[])) if applicable(v) and v['task_postcondition']),None)
        if matched:
            result.update(checks=deepcopy(matched['checks']),intent_verdict=run['chain_verdict'],task_postcondition=True)
            intent_exchange = matched['exchange_id']
    if run.get("chain_verdict") == "fail":
        result["intent_verdict"] = "fail"
        result["task_postcondition"] = True
    elif run.get('chain_verdict') == 'not_checked' and result.get('intent_verdict') == 'pass':
        # Успешный login не завершает ещё не выполненную бизнес-цепочку.
        result['intent_verdict'] = 'not_checked'
        result['task_postcondition'] = False
    labels = {
        "pass": "соответствует",
        "fail": "не соответствует",
        "mismatch": "найдены нарушения",
        "not_checked": "не проверено",
        "needs_review": "требует разбора",
        "setup_error": "не подтверждены условия подготовки",
    }
    summary = (
        "Контракт API: "
        + labels.get(result.get("contract_verdict"), "не проверено")
        + ". Условие проверки: "
        + labels.get(result.get("intent_verdict"), "не проверено")
        + "."
    )
    if run.get('stop_reason') == 'cancelled':
        summary = 'Выполнение Run отменено. Результаты уже выполненных операций сохранены.'
    elif run.get('stop_reason') == 'approval_rejected':
        summary = 'Действие отменено: подтверждение отклонено. Ранее выполненные операции сохранены.'
    unverified = []
    if not result.get("task_postcondition"):
        unverified.append("Поддержанные условия проверки выполнены не полностью.")
    unverified.append(
        "Произвольный текст цели оценивается только в рамках явно зафиксированных условий."
    )
    explanations = {
        "schema_type": "Тип значения отличается от объявленного в OpenAPI.",
        "required": "В полном ответе отсутствует обязательное поле.",
        "status": "Полученный HTTP-статус не объявлен в контракте.",
        "content_type": "Заголовок ответа не обозначает JSON.",
        "json": "Полное тело ответа не удалось разобрать как JSON.",
        "enum": "Значение отсутствует в объявленном перечне допустимых.",
        "additional_property": "Контракт запрещает это дополнительное поле.",
    }
    report_issues = [
        {
            **i,
            "explanation": explanations.get(
                i["code"], "Условие проверки не выполнено."
            ),
        }
        for i in result.get("issues", [])
    ]
    if result.get("intent_verdict") == "setup_error":
        unverified.append(
            "Расхождение не подтверждает дефект API: условия подготовки запроса неизвестны."
        )
    layer = {
        "trace_error": "storage",
        "journal_error": "storage",
        "runtime_error": "application",
        "invalid_expectation": "validation",
        "validation_binding_error": "validation",
        "clarification_required": "validation",
        "token_preflight_error": "tokenizer",
        "token_accounting_error": "tokenizer",
    }
    reason = run["stop_reason"] or "unclassified"
    diagnostic_layer = layer.get(
        reason,
        (
            "llm"
            if reason.startswith("model_")
            or reason in {"empty_limit", "malformed_limit"}
            else "budget"
        ),
    )
    if reason == "finished" and not result.get("task_postcondition"):
        diagnostic_layer = (
            "transport" if not result.get("complete_response") else "validation"
        )
    if reason == "clarification_required":
        summary = "Нужно уточнить цель: доступны проверки ответа getPet. Задайте явные условия или выберите проверку контракта. Запрос не отправлен."
        if "chain_verdict" in run:
            summary = "Нужно уточнить цель: выбранный профиль поддерживает только свою короткую цепочку. Запрос не отправлен."
    elif reason == "invalid_expectation":
        summary = "Условие не поддерживается или не имеет проверенного основания. Уточните его до запроса; HTTP не отправлен."
    diagnostic = (
        None
        if run["status"] == "completed"
        else {
            "failure_code": reason,
            "layer": diagnostic_layer,
            "first_step": next(
                (
                    step["index"]
                    for step in run["steps"]
                    if step.get("decision_kind") in {"empty", "malformed"}
                ),
                len(run["steps"]),
            ),
            "basis": "deterministic_rule",
            "recoverable": False,
            "recovery": "Явный новый Run после проверки причины; автоматического повторения действий нет.",
        }
    )
    checks_verdict = {'pass': 'PASS', 'fail': 'FAIL'}.get(result.get('intent_verdict'), 'UNKNOWN')
    goal_verdict = 'FAIL' if checks_verdict == 'FAIL' else 'PASS' if checks_verdict == 'PASS' and run['status'] == 'completed' and run.get('goal_coverage') == 'complete' else 'UNKNOWN'
    return {
        "summary": summary,
        "checks_verdict": checks_verdict,
        "goal_verdict": goal_verdict,
        "goal_coverage": run.get('goal_coverage', 'partial'),
        "intent_exchange_id": intent_exchange,
        **({'summary_review':deepcopy(run['summary_review']['assessment'])} if 'summary_review' in run else {}),
        "goal": run["goal"],
        "goal_hash": run.get("goal_hash"),
        "execution_status": run["status"],
        "stop_reason": run["stop_reason"],
        "contract_verdict": result.get("contract_verdict", "not_checked"),
        "intent_verdict": result.get("intent_verdict", "not_checked"),
        **(
            {
                "chain_verdict": run["chain_verdict"],
                "chain_basis": "runtime_reference_binding",
            }
            if "chain_verdict" in run
            else {}
        ),
        "issues": report_issues,
        "issues_total": result.get("issues_total", len(report_issues)),
        "issue_details_truncated": result.get("issue_details_truncated", False),
        "intent_results": deepcopy(result.get("checks", [])),
        "unverified": unverified,
        "confirmed_contract_violations": result.get("confirmed_contract_violations", 0),
        "diagnostic": diagnostic,
        "failed_attempts": [
            {"step": step["index"], "kind": step.get("decision_kind")}
            for step in run["steps"]
            if step.get("decision_kind") in {"empty", "malformed"}
        ],
        "evidence_ids": [e["id"] for e in run["exchanges"]],
        "continuation": "Новый явный Run; состояние API и допустимость действий проверяются заново.",
        "resources": deepcopy(run.get("resources", {})),
    }

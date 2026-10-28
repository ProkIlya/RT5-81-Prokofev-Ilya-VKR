"""Проверка HTTP exchange: независимые contract и intent verdict."""
from copy import deepcopy

from api_agent.openapi.parser import canonical, pointer_get

from api_agent.validation.common import fingerprint, display_value
from api_agent.validation.schema import schema_issues

def validate_exchange(catalog, exchange, expectations):
    """Проверить host-факты ответа и вернуть независимые contract/intent.

    Полное невалидное JSON — контрактный результат. Неполное/слишком большое
    тело и инфраструктурный отказ не дают права диагностировать отсутствующие
    поля. Сырые bytes и secrets не сохраняются; caller маскирует итог.
    """
    operation_id = exchange["operation_id"]
    outcome, response = exchange["outcome"], exchange["response"]
    complete = outcome in {"received", "invalid_json", "invalid_content_type"}
    issues = []
    content_type_is_json=False
    contract = "not_checked"
    op = catalog.operations[operation_id]
    schema_path = None
    if complete:
        media=response.get('content_type','').split(';')[0].strip().lower()
        # Этот bool вычисляет transport из raw header, не JSON сервера/модель.
        # Legacy exchange без флага сохраняет прежнюю проверку публичного MIME.
        content_type_is_json=response.get('content_type_is_json')
        if type(content_type_is_json) is not bool:content_type_is_json=media=='application/json'
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
                if not content_type_is_json:
                    issues.append(
                        {
                            "code": "content_type",
                            "path": "/content_type",
                            "schema_path": "/responses/" + response_key + "/content",
                            "expected": "application/json",
                            "actual": response['content_type'] if media=='[redacted]' else media,
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
        from api_agent.validation.selection_scope import scope_result
        selection = scope_result(expectation, exchange)
        principal_scope = expectation.get('request_principal_scope')
        principal_matches = principal_scope is None or all((exchange.get('request_principal') or {}).get(k) == v
            for k, v in principal_scope.items())
        if complete and principal_matches and (selection is None or selection['applicable']):
            if predicate == "status_in":
                observed = response["status"] in params["values"]
            elif predicate == "content_type_is_json":
                observed = content_type_is_json
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

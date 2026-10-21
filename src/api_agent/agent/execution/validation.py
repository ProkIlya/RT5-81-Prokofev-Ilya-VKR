"""Связывание validation DTO с неизменным основанием и evidence Run."""

from ...openapi.parser import canonical

def validation_checks(self, trusted_validation_basis, trusted_check_identity):
    def validation_bound(value, evidence_id, *, redacted=False):
        """Связать результат с исходным Run/контрактом до stamp и при finish.

        Fingerprint результата один не доказывает корректное происхождение:
        ошибочный компонент может вернуть чужое основание до его фиксации.
        При finish сравниваются безопасные представления исходных связей.
        """
        if not isinstance(value, dict):
            return False
        expected = {**trusted_validation_basis, "exchange_id": evidence_id}
        if redacted:
            expected = self.safe(expected)
        actual = {key: value.get(key) for key in expected}
        return canonical(actual) == canonical(expected)

    def validation_shape(value, prepared):
        """Отклонить повреждённый DTO до публикации и создания stamp.

        Проверяется закрытая структура и типы управляющих полей; verdict
        по содержанию ответа по-прежнему вычисляет отдельный validator.
        """
        required = set(trusted_validation_basis) | {
            "exchange_id",
            "complete_response",
            "contract_verdict",
            "intent_verdict",
            "checks",
            "issues",
            "issues_total",
            "issue_details_truncated",
            "confirmed_contract_violations",
            "task_postcondition",
            "completion_scope",
        }
        if set(value) != required:
            return False
        if any(
            type(value[k]) is not bool
            for k in (
                "complete_response",
                "task_postcondition",
                "issue_details_truncated",
            )
        ):
            return False
        if (
            value["contract_verdict"] not in {"pass", "mismatch", "not_checked"}
            or value["intent_verdict"]
            not in {"pass", "fail", "not_checked", "needs_review", "setup_error"}
            or value["completion_scope"] != "supported_checks_only"
        ):
            return False
        if not isinstance(value["checks"], list) or len(value["checks"]) != len(
            prepared
        ):
            return False
        if not isinstance(value["issues"], list) or len(value["issues"]) > 128:
            return False
        if any(not isinstance(v, dict) for v in value["checks"] + value["issues"]):
            return False
        check_fields = {
            "expectation_id",
            "source",
            "predicate",
            "verdict",
            "comparison",
            "basis",
        }
        for check, identity in zip(value["checks"], trusted_check_identity):
            if (
                not check_fields <= set(check)
                or set(check)
                - check_fields
                - {"path", "expected", "actual", "actual_present", "selection_scope"}
                or canonical({k: check[k] for k in identity}) != canonical(identity)
                or check["verdict"]
                not in {
                    "pass",
                    "fail",
                    "not_checked",
                    "setup_error",
                    "needs_review",
                }
                or (
                    check["comparison"] is not None
                    and type(check["comparison"]) is not bool
                )
            ):
                return False
        issue_fields = {"code", "path", "schema_path", "expected", "actual"}
        for issue in value["issues"]:
            if (
                set(issue) != issue_fields
                or issue["code"]
                not in {
                    "status",
                    "content_type",
                    "json",
                    "schema_type",
                    "required",
                    "enum",
                    "additional_property",
                }
                or any(
                    not isinstance(issue[k], str) for k in ("path", "schema_path")
                )
            ):
                return False
        if any(
            type(value[k]) is not int or value[k] < 0
            for k in ("issues_total", "confirmed_contract_violations")
        ):
            return False
        return (
            value["issues_total"] >= len(value["issues"])
            and value["confirmed_contract_violations"] <= value["issues_total"]
        )

    return validation_bound, validation_shape

"""Описание операций, discovery и начальный контекст модели."""
from copy import deepcopy
import json
from ...policy import decision
from ...validation import fingerprint


class CatalogMetadata:
    def inspect(self, catalog, op, section="inputs"):
        operation = catalog.operations[op]
        if section not in {"inputs", "responses", "all"}:
            raise ValueError("unknown_schema_section")
        if section == "inputs":
            selected = {
                k: deepcopy(v) for k, v in operation.items() if k != "responses"
            }
            selected["response_statuses"] = sorted(operation["responses"])
            omitted = ["responses"]
        elif section == "responses":
            selected = {"responses": deepcopy(operation["responses"])}
            omitted = ["inputs"]
        else:
            selected, omitted = deepcopy(operation), []
        data = {
            "catalog_operation": {"operation_id": op, **selected},
            "sections": [section],
            "omitted_sections": omitted,
        }
        private_body = bool(self.auth and op == self.auth["login_operation"])
        authentication = ('credential_ref_required' if self.auth_mode(op) == 'authenticated'
                          else self.auth_mode(op))
        data["input_requirements"] = {
            "parameters": [p["name"] for p in operation["parameters"] if p["required"]],
            "body": "application_supplied" if private_body else "user_required" if operation.get("requestBody", {}).get("required") else "none",
            "authentication": authentication,
        }
        if private_body:
            data["next_hint"] = "Call execute_operation with operation_id only. Application supplies the private login body; never generate credentials."
        elif data["input_requirements"]["body"] == "user_required":
            data["next_hint"] = "Put requestBody fields in top-level body, NOT parameters. Use values from the goal; ask if required values are missing."
        if authentication == "credential_ref_required":
            data["input_requirements"]["credential_source"] = self.auth["login_operation"]
        no_inputs = (
            not any(p["required"] for p in operation["parameters"])
            and (private_body or not operation.get("requestBody", {}).get("required"))
            and self.auth_mode(op) != 'authenticated'
        )
        data["input_readiness"] = "ready" if no_inputs else "needs_inputs_or_auth"
        if no_inputs and not private_body:
            data["next_hint"] = (
                "execute_operation needs only operation_id; no more inspect required."
            )
        if len(json.dumps(data, ensure_ascii=False).encode()) > 8192:
            raise ValueError("schema_too_large")
        return data

    def _effective(self):
        current = self.current_config().snapshot
        fixed = self.config.snapshot
        if any(current[k] != fixed[k] for k in ("project_id", "target", "spec_hash")):
            raise ValueError("foreign_current_config")
        policies = {}
        for name in self.allowed:
            op_hash = self.catalog.operation_hashes[name]
            old, new = decision(fixed, name, op_hash), decision(current, name, op_hash)
            if "DENY" not in {old, new}:
                policies[name] = "CONFIRM" if "CONFIRM" in {old, new} else "ALLOW"
        return current, policies

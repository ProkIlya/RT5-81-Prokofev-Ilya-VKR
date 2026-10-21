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

    def _count(self, run, history, content, counter, native_call=None):
        if counter is None:
            return None
        payload = {
            "model": run.get("model", {}).get("name", "MiniCPM5-2B-Q4_K_M"),
            "messages": deepcopy(history) + [{"role": "user", "content": content}],
            "tools": self.registry(self.catalog),
            "tool_choice": "required",
            "temperature": 0.0,
            "top_p": 0.95,
            "min_p": 0.0,
            "max_tokens": run.get("limits", {}).get("max_output_tokens", 512),
            "parallel_tool_calls": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if native_call is not None:
            payload["messages"] = deepcopy(history) + [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [deepcopy(native_call)],
                },
                {"role": "tool", "tool_call_id": native_call["id"], "content": content},
            ]
        redact = self._redactors.get(run["run_id"], lambda value: value)
        return counter(redact(payload))

    def initial_context(self, run, counter, history=None, redactor=None):
        self._redactors[run["run_id"]] = redactor or (lambda value: value)
        current, policies = self._effective()
        ranked = self._discovery.ranked(self.goal, list(policies), max_characters=2000)
        # Малый scope можно показать целиком. В большом каталоге нулевая
        # релевантность не даёт основания рекомендовать первые ID по алфавиту.
        # Вместо случайных карточек остаётся scoped vocabulary для поиска.
        if len(policies) <= 8:
            names = {name for name, _ in ranked}
            ranked += [(name, score) for name, score in self._discovery.ranked('', list(policies)) if name not in names]
        selected = ranked[:8]
        index = self._discovery.index(list(policies)) if len(selected) < len(policies) else None
        history = history or [
            {"role": "system", "content": self.system()},
            {"role": "user", "content": self.goal},
        ]
        count = None
        while True:
            content = (
                "All permitted operations shown; no search needed: "
                if len(selected) == len(policies)
                else "Starting cards; remaining "
                + str(len(policies) - len(selected))
                + " permitted operations via search_operations: "
            ) + json.dumps(
                [
                    self._discovery.card(name, policies[name])
                    for name, score in selected
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            )
            if index and index["groups"]:
                content += " API vocabulary (translate goal; search with these query words):" + json.dumps(index, ensure_ascii=False, separators=(",", ":"))
            count = self._count(run, history, content, counter)
            if (
                (not selected and not (index and index["groups"]))
                or count is None
                or count + run.get("limits", {}).get("max_output_tokens", 512) + 32
                <= run.get("limits", {}).get("max_context_tokens", 2048)
            ):
                break
            if selected:
                selected.pop()
            elif index and index["groups"]:
                index["groups"].pop()
                index["more"] = True
            else:
                break
        run["selection_manifest"] = {
            "selected_ids": [n for n, s in selected],
            "scores": {n: s for n, s in selected},
            "omitted_count": len(policies) - len(selected),
            "input_tokens": count,
            "output_reserve": run.get("limits", {}).get("max_output_tokens", 512),
            "max_context_tokens": run.get("limits", {}).get("max_context_tokens", 2048),
            "safety_margin": 32,
            "spec_hash": self.catalog.spec_hash,
            "config_version": self.config.snapshot["version"],
        }
        self._contexts[run["run_id"]] = (run, counter)
        return self._redactors[run["run_id"]](content)

    def close_run(self, run_id):
        """Освободить приватные projections и callbacks завершённого Run."""
        self._contexts.pop(run_id, None)
        self._redactors.pop(run_id, None)

    def discovery(self, run_id, args, call=None):
        current, policies = self._effective()
        context = self._contexts.get(run_id)
        size = 8
        while True:
            page = self._discovery.page(
                run_id, args, list(policies), policies, fingerprint(current), size
            )
            if context is None:
                return page
            run, counter = context
            count = self._count(
                run,
                run.get("history", []),
                json.dumps({"status": "ok", "data": page}, ensure_ascii=False),
                counter,
                native_call=call,
            )
            if (
                count is None
                or count + run.get("limits", {}).get("max_output_tokens", 512) + 32
                <= run.get("limits", {}).get("max_context_tokens", 2048)
                or not page["items"]
            ):
                run.setdefault("discovery_pages", []).append(
                    {
                        "query": args["query"],
                        "ids": [x["operation_id"] for x in page["items"]],
                        "input_tokens": count,
                        "more": page["more"],
                    }
                )
                return page
            if size == 1:
                raise ValueError("search_page_too_large")
            size -= 1

"""Реестр инструментов, проверка аргументов и выбор разрешённой фазы."""
from copy import deepcopy

from .types import PhaseDecision

class CatalogTools:
    def registry(self, catalog):
        scalar = {"type": "string", "minLength": 1, "maxLength": 200}
        op = {
            "type": "object",
            "properties": {"operation_id": scalar},
            "required": ["operation_id"],
            "additionalProperties": False,
        }
        execution = deepcopy(op)
        execution["properties"].update(
            parameters={"type": "object"},
            body={"type": "object"},
            bindings={"type": "object"},
            credential_ref=scalar,
        )
        if self.occurrences:
            execution['properties']['occurrence_id'] = scalar
            execution['required'].append('occurrence_id')
        # Публикуем только применимые к pinned allowlist виды входов. Иначе
        # модель заполняет body/auth даже для анонимного GET без параметров.
        # Union сохраняет поля, нужные хотя бы одной разрешённой операции;
        # конкретные значения и policy по-прежнему проверяет session.
        operations = [catalog.operations[name] for name in self.allowed]
        applicable = {
            "parameters": any(op["parameters"] for op in operations),
            "body": any("requestBody" in catalog.operations[name]
                        and not (self.auth and name == self.auth["login_operation"])
                        for name in self.allowed),
            "bindings": any(self.binding_rules.get(name) for name in self.allowed),
            "credential_ref": any(self.auth_mode(name) == 'authenticated'
                                  for name in self.allowed),
        }
        for field, needed in applicable.items():
            if not needed:
                execution["properties"].pop(field)
        inspect = deepcopy(op)
        inspect["properties"]["section"] = {
            "type": "string",
            "enum": ["inputs", "responses", "all"],
        }
        finish = {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "minLength": 1, "maxLength": 500},
                "evidence_ids": {
                    "type": "array",
                    "items": scalar,
                    "minItems": 1,
                    "maxItems": 128,
                },
            },
            "required": ["summary", "evidence_ids"],
            "additionalProperties": False,
        }
        search = {
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 200},
                "cursor": {"type": "string", "maxLength": 2000},
            },
            "required": ["query"],
            "additionalProperties": False,
        }
        saved = {"type":"object", "properties":{
            "exchange_id":scalar, "json_pointer":{"type":"string","maxLength":200},
            "offset":{"type":"integer","minimum":0}, "limit":{"type":"integer","minimum":1,"maximum":16}},
            "required":["exchange_id"], "additionalProperties":False}
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": schema,
                },
            }
            for name, description, schema in (
                (
                    "execute_operation",
                    "Call allowed API.",
                    execution,
                ),
                (
                    "inspect_operation",
                    "Read schema.",
                    inspect,
                ),
                (
                    "finish_run",
                    "Finalize checked task.",
                    finish,
                ),
                (
                    "search_operations",
                    'Browse permitted operation names with query="" and cursor, or search API metadata. Then inspect selected operation_id for its exact schema.',
                    search,
                ),
                ("read_response", "Read saved JSON page.", saved),
            )
        ]

    def decision(self, name, args):
        if (
            name in {"execute_operation", "inspect_operation"}
            and args["operation_id"] not in self.allowed
        ):
            raise ValueError("operation_denied")
        if name not in {
            "execute_operation",
            "inspect_operation",
            "finish_run",
            "search_operations",
            "read_response",
        }:
            raise ValueError("unknown_tool")

    def dispatch_name(self, name):
        return name

    def next_tool(self, run):
        """Ограничить ненужный поиск только при полном наборе стартовых карточек.

        Выбор operation/аргументов остаётся у модели, policy выполняется обычно.
        Фактические registry/prefix учитываются tokenizer; choice не даёт права HTTP.
        При неполном каталоге и неполном body нужен обычный выбор инструмента.
        """
        manifest = run.get("selection_manifest", {})
        steps = [s for s in run.get("steps", []) if s.get("action")]
        if not steps and set(manifest.get("selected_ids", [])) != set(self.allowed):
            if not manifest.get("selected_ids"):
                return {"type": "function", "function": {"name": "search_operations"}}
            return "required"
        chosen = "inspect_operation"
        if not steps and all(not any(p["required"] for p in self.catalog.operations[name]["parameters"]) and not self.catalog.operations[name].get("requestBody", {}).get("required") and (not self.catalog.operations[name]["security"] or name in self.anonymous_operations) for name in self.allowed):
            chosen = "execute_operation"
        if steps:
            last = steps[-1]
            data = (last.get("observation") or {}).get("data") or {}
            if data.get("checks_complete") and data.get("response_body_complete", True):
                chosen = "finish_run"
            elif data.get("verify_unfiltered"):
                chosen = "execute_operation"
            elif last["action"]["name"] == "search_operations" and "items" in data:
                chosen = "inspect_operation" if data["items"] else "search_operations"
            elif last["action"]["name"] == "inspect_operation" or any(h.get("readiness") == "ready" for h in data.get("continuation_hints", [])):
                chosen = "execute_operation"
            else:
                return "required"
        return {"type": "function", "function": {"name": chosen}}

    def next_occurrence(self, exchanges):
        """Последовательный manifest принят приложением до первого HTTP."""
        done = {e.get('occurrence_id') for e in exchanges if e.get('outcome') == 'received'}
        return next((s for s in self.occurrences if s['id'] not in done), None)

    def phase_decision(self, run, registry):
        offered = self.phase_registry(run, registry)
        choice = self.next_tool(run)
        names = {t['function']['name'] for t in offered}
        if isinstance(choice, dict) and choice['function']['name'] not in names:
            # Checks уже оценены; повторный inspect не возвращает HTTP права.
            partial = any(((s.get('observation') or {}).get('data') or {}).get('response_body_complete') is False
                          for s in run.get('steps', []))
            preferred = 'read_response' if partial and 'read_response' in names else 'finish_run'
            choice = {'type': 'function', 'function': {'name': preferred}} if preferred in names else 'required'
        return PhaseDecision(offered, choice)

    def run_registry(self, run, registry):
        """Зафиксировать инструменты после preflight, без лишнего поиска.

        Если весь scoped каталог показан, search не публикуется. Набор стабилен
        по ходам, не зависит от языка и не расширяет HTTP-права.
        """
        shown = set(run.get("selection_manifest", {}).get("selected_ids", []))
        return [t for t in registry if t["function"]["name"] != "search_operations"] if shown == set(self.allowed) else registry

    def phase_registry(self, run, registry):
        """После полного оценивания остаются чтение evidence и явный finish.

        Усечённая проекция не принуждает закончить: read_response остаётся.
        Новые HTTP/search/schema не нужны для уже выполненных условий Run.
        Схема execute сужается по OpenAPI/refs; фактический prefix записывает loop.
        """
        done = any(((step.get("observation") or {}).get("data") or {}).get("checks_complete") is True for step in run.get("steps", []))
        if done:
            return [t for t in registry if t["function"]["name"] in {"read_response", "finish_run"}]
        from ..catalog_inputs import execution_schema
        selected = execution_schema(self, run)
        registry = deepcopy(registry)
        if selected is not None:
            for tool in registry:
                if tool['function']['name'] == 'execute_operation':
                    tool['function']['parameters'] = selected
                    tool['function']['description'] = 'Execute the shown operation. Required prerequisites are selected from current Run state. Application supplies private credentials; use only issued references.'
        return registry

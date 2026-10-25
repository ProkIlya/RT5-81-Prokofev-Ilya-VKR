"""Проекции ответов, runtime refs и проверка завершения цепочки."""
from copy import deepcopy
import json
from ...validation import display_value


class CatalogObservations:
    def observation(self, session, exchange, validation):
        values = []
        login = bool(self.auth and exchange['operation_id'] == self.auth['login_operation'])
        status = (exchange.get('response') or {}).get('status')
        if (
            exchange["outcome"] == "received"
            and validation["contract_verdict"] == "pass"
            and (not login or type(status) is int and 200 <= status < 300)
        ):
            if login:
                # Actor задаёт приложение; token никогда не является его именем.
                values.append(session.state.remember(exchange, self.auth['token_path'],
                    authenticate_as='configured_account'))
                session.run['auth_state'] = {'auth_epoch': session.state.auth_epoch,
                                            'principal': session.state.principal}
            for path in self.extraction.get(exchange["operation_id"], {}):
                if login and path == self.auth['token_path']: continue
                values.append(session.state.remember(exchange, path))
        data = {
            "exchange_id": exchange["id"],
            "outcome": exchange["outcome"],
            "status": (exchange["response"] or {}).get("status"),
            "validation": {
                k: validation[k]
                for k in ("contract_verdict", "intent_verdict", "task_postcondition")
            },
            "runtime_values": values,
            "finish_evidence_ids": [e["id"] for e in session.exchanges],
        }
        # Малый ответ полезен для выбора следующего шага; секретные пути маскируются.
        expected = next((e["parameters"]["values"] for e in session.run["expectations"]
                         if e["operation_id"] == exchange["operation_id"] and e["predicate"] == "status_in"), None)
        if expected is not None:
            data["validation"]["expected_status_codes"] = deepcopy(expected)
        raw = (exchange["response"] or {}).get("json")
        if raw is not None or (exchange["response"] or {}).get("json_valid") is True:
            public_body = session.state.redact(raw)
            if len(json.dumps(public_body, ensure_ascii=False).encode()) <= 1024:
                data["response_json"] = public_body
            else:
                from ..response_views import response_page
                view = response_page(public_body)
                data["response_json"] = view.pop("value")
                data["response_body_complete"] = view["complete"]
                data["response_view"] = view
                data["response_read_hint"] = "Use read_response with exchange_id, json_pointer and offset to inspect saved data without HTTP."
        public = session.state.public()
        _, effective = self._effective()
        hints = []
        pending = self.next_occurrence(session.exchanges)
        for name in self.allowed:
            if name not in effective or name == exchange["operation_id"]:
                continue
            op = self.catalog.operations[name]
            mode = self.request_mode(name, pending if pending and pending['operation_id'] == name else None)
            bindings = {}
            for destination, rule in self.bindings_for(name, pending['id'] if pending and pending['operation_id'] == name else None).items():
                refs = [
                    v["ref"]
                    for v in public
                    if not v["secret"]
                    and v["operation_id"] == rule["source_operation"]
                    and v["path"] == rule["source_path"]
                    and ('source_occurrence' not in rule or v.get('occurrence_id') == rule['source_occurrence'])
                ]
                if refs:
                    bindings[destination] = refs
            auth_refs = [
                v["ref"]
                for v in public
                if v["secret"]
                and self.auth
                and v["operation_id"] == self.auth["login_operation"]
                and v["path"] == self.auth["token_path"]
            ]
            auth_refs = [v['ref'] for v in public if v.get('auth_epoch') == session.state.auth_epoch
                         and v.get('principal') == session.state.principal and v['ref'] in auth_refs]
            if not bindings and not (mode == 'authenticated' and auth_refs):
                continue
            ready = (
                all(p["name"] in bindings for p in op["parameters"] if p["required"])
                and not op.get("requestBody", {}).get("required")
                and (mode != 'authenticated' or bool(auth_refs))
            )
            hint = {
                "operation_id": name,
                "readiness": "ready" if ready else "needs_inputs",
                "policy": effective[name],
            }
            if bindings:
                hint["bindings"] = bindings
            if mode == 'authenticated' and auth_refs:
                hint["credential_refs"] = auth_refs
            hints.append(hint)
            if len(hints) == 3:
                break
        if hints:
            data["continuation_hints"] = hints
        from ...validation.selection_scope import applicable
        chain_check = self.binding_check(session, exchange) if applicable(validation) else None
        if chain_check is not None:
            data["chain_check"] = chain_check
        if self.requires_unfiltered(exchange):
            data["verify_unfiltered"] = True
            data["selection_scope"] = "The configured check requires an unfiltered request. This filtered response is discovery only; its contents do not decide that check."
        else:
            scoped = [c['selection_scope'] for c in validation.get('checks',[]) if 'selection_scope' in c]
            if scoped:
                data['selection_scope'] = deepcopy(scoped[0])
        if self.completion(session, session.run["validations"]) != "not_checked":
            # Завершённая проверка может обнаружить дефект API. Подсказка не
            # означает pass и не выполняет finish за модель; критерии и IDs
            # всё равно проверяются при её явном native вызове.
            data["checks_complete"] = True
            data["next_hint"] = "Call finish_run with all finish_evidence_ids."
        return data




    def binding_check(self, session, exchange):
        """Проверить настроенные связи по данным обмена, без знания домена API.

        Сравниваем исходный ответ; в diagnostics попадает только redacted body.
        Один результат используется и моделью, и завершающей проверкой цепочки.
        """
        from ...openapi.parser import pointer_get, canonical

        comparisons = []
        for name, expected in exchange.get("bound_inputs", {}).items():
            rule = self.bindings_for(exchange["operation_id"], exchange.get('occurrence_id')).get(name, {})
            path = rule.get("response_path")
            if not path:
                continue
            body = (exchange.get("response") or {}).get("json")
            present, actual = True, None
            try:
                actual = pointer_get(body, path)
            except (ValueError, KeyError, IndexError, TypeError):
                present = False
            public_actual = None
            if present:
                try:
                    public_actual = pointer_get(session.state.redact(body), path)
                except (ValueError, KeyError, IndexError, TypeError):
                    public_actual = "[REDACTED]"
            comparisons.append({
                "parameter": name,
                "response_path": path,
                "expected": display_value(expected),
                "actual": display_value(public_actual),
                "actual_present": present,
                "matches": present and canonical(actual) == canonical(expected),
            })
        if not comparisons:
            return None
        return {
            "verdict": "pass" if all(c["matches"] for c in comparisons) else "fail",
            "basis": "runtime_reference_binding",
            "exchange_id": exchange["id"],
            "comparisons": comparisons,
        }

    def requires_unfiltered(self, exchange):
        """Только явное ожидание без query допускает предложение снять фильтр.

        Только модель выбирает следующий HTTP. Для обязательных query inputs
        полный просмотр недоступен: область цели остаётся явно ограниченной.
        """
        from urllib.parse import urlsplit, parse_qsl
        op = self.catalog.operations[exchange['operation_id']]
        return (self.selection_scopes.get(exchange['operation_id'],{}).get('mode') == 'unfiltered'
            and exchange.get('outcome') == 'received'
            and op['method'] == 'GET'
            and bool(parse_qsl(urlsplit(exchange.get('request', {}).get('url', '')).query))
            and not any(p['in'] == 'query' and p['required'] for p in op['parameters']))

    def completion(self, session, validations):
        from ...validation.selection_scope import applicable
        latest = {v["operation_id"]: v for v in validations if applicable(v)}
        checked = {name for name, v in latest.items() if v["task_postcondition"]}
        if self.occurrences:
            by_exchange = {v['exchange_id']: v for v in validations}
            for slot in self.occurrences:
                matching = [e for e in session.exchanges if e.get('occurrence_id') == slot['id']]
                if not matching or any(not by_exchange.get(e['id'], {}).get('task_postcondition') for e in matching):
                    return 'not_checked'
        if not set(self.conditions) <= checked or len(validations) != len(
            session.exchanges
        ):
            return "not_checked"
        for exchange in session.exchanges:
            if exchange["outcome"] != "received":
                return "not_checked"
            assessed = next((v for v in validations if v.get('exchange_id')==exchange['id']),None)
            if assessed is not None and not applicable(assessed):
                continue
            check = self.binding_check(session, exchange)
            if check is not None and check["verdict"] == "fail":
                return "fail"
        return (
            "fail"
            if any(v['operation_id'] in self.conditions and applicable(v) and v['intent_verdict'] == 'fail' for v in validations)
            else "pass"
        )

"""Проекции ответов, runtime refs и проверка завершения цепочки."""
from copy import deepcopy
from api_agent.validation import display_value


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
        return {'exchange_id':exchange['id'],'runtime_values':values}





    def binding_check(self, session, exchange):
        """Проверить настроенные связи по данным обмена, без знания домена API.

        Сравниваем исходный ответ; в diagnostics попадает только redacted body.
        Один результат используется и моделью, и завершающей проверкой цепочки.
        """
        from api_agent.openapi.parser import pointer_get, canonical

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


    def completion(self, session, validations):
        from api_agent.validation.selection_scope import applicable
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

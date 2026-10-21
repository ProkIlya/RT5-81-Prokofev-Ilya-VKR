"""Канонический отчёт по сохранённым проверкам и evidence."""
from copy import deepcopy

from ..openapi.parser import canonical


def report(run):
    """Собрать человеческий итог из проверок, не из свободного текста LLM.

    Это передаваемая сводка; автоматическое восстановление и повтор эффектов
    здесь отсутствуют. Свободная цель не объявляется полностью формализованной.
    """
    result = deepcopy(run.get("validation") or {})
    # Один exchange оценивается один раз; read_response и копии DTO не
    # увеличивают число нарушений. Поздний PASS не стирает прежний mismatch.
    assessed = {}
    for value in [*run.get('validations', []), *([run['validation']] if run.get('validation') else [])]:
        assessed.setdefault(value['exchange_id'], value)
    if assessed:
        values = list(assessed.values())
        verdicts = {v.get('contract_verdict') for v in values}
        result['contract_verdict'] = ('mismatch' if 'mismatch' in verdicts else
            'pass' if verdicts == {'pass'} else 'not_checked')
        issues = [{**deepcopy(i), 'exchange_id': v['exchange_id'], 'operation_id': v['operation_id']}
                  for v in values for i in v.get('issues', [])]
        result.update(issues=issues[:128], issues_total=sum(v.get('issues_total', 0) for v in values),
            confirmed_contract_violations=sum(v.get('confirmed_contract_violations', 0) for v in values))
        result['issue_details_truncated'] = result['issues_total'] > len(result['issues'])
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
    from .selection_scope import applicable
    failed = next((v for v in assessed.values() if v.get('intent_verdict') == 'fail' and applicable(v)), None)
    if failed is not None:
        result.update(intent_verdict='fail', task_postcondition=True, checks=deepcopy(failed['checks']))
        intent_exchange = failed['exchange_id']
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
    restrictions = {
        'policy_denied': 'Действие запрещено текущей политикой доступа',
        'approval_required': 'Для действия требуется подтверждение владельца',
        'approval_rejected': 'Действие отменено: владелец отклонил подтверждение',
        'precondition_not_verified': 'Условие зависимого действия ложно или не подтверждено',
        'target_not_applicable': 'Выбранная сущность не соответствует принятой цели',
        'request_limit': 'Исчерпан бюджет HTTP-запросов',
    }
    if reason in restrictions:
        mode = (run.get('policy_config') or {}).get('mode')
        summary = restrictions[reason] + (f' (режим {mode})' if mode else '') + '. Цель не подтверждена; повтор запрещён без новой проверки.'
    if run.get('primary_safety_cause') == 'uncertain_write' or run.get('uncertain_write'):
        summary = 'Результат изменяющего запроса неизвестен. Автоматический повтор запрещён; ранее полученные свидетельства сохранены.'
    if run.get('blocking_uncertain_runs'):
        summary = 'Результат предыдущего изменяющего запроса Project неизвестен. Новая мутация запрещена до проверенного разрешения неопределённости; новое сообщение не снимает запрет.'
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
    goal_checklist = None
    goal_contract = (run.get('admission_manifest') or {}).get('goal_contract')
    if goal_contract:
        from ..goals.compiler import AcceptedGoal, check_goal
        from ..openapi.parser import Catalog
        accepted = AcceptedGoal(canonical(goal_contract['manifest']))
        if accepted.hash != goal_contract['manifest_hash']:
            raise ValueError('goal_manifest_changed')
        goal_checklist = check_goal(accepted, Catalog(canonical(run['contract'])), run)
        goal_verdict = ('FAIL' if checks_verdict == 'FAIL' or goal_checklist['verdict'] == 'FAIL' else
                        'PASS' if run['status'] == 'completed' and goal_checklist['verdict'] == 'PASS' else 'UNKNOWN')
        if reason == 'finished':
            summary = {'PASS': 'Цель подтверждена.', 'FAIL': 'Обнаружено несоответствие принятым условиям.',
                       'UNKNOWN': 'Цель не подтверждена полностью.'}[goal_verdict]
    answer = None
    if goal_checklist is not None:
        from ..goals.feedback import build_feedback
        answer = build_feedback({**run, 'report': {'goal_verdict': goal_verdict, 'goal_checklist': goal_checklist,
            'contract_verdict': result.get('contract_verdict', 'not_checked')}})
    return {
        **({'answer': answer} if answer is not None else {}),
        **({'answer_review': {'status': run['answer_feedback']['status'],
            'diagnostic': run['answer_feedback']['diagnostic']['status'],
            'revision': run['answer_feedback']['revision']['status'], 'calls': run['answer_feedback']['calls']}}
            if 'answer_feedback' in run else {}),
        **({'goal_checklist': goal_checklist} if goal_checklist is not None else {}),
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
        **({'primary_safety_cause': run['primary_safety_cause']} if run.get('primary_safety_cause') else {}),
        **({'blocking_uncertain_runs': deepcopy(run['blocking_uncertain_runs'])} if run.get('blocking_uncertain_runs') else {}),
        **({'secondary_stop_reason': run['secondary_stop_reason']} if run.get('secondary_stop_reason') else {}),
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

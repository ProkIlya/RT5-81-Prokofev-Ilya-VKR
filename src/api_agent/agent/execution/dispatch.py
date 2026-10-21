"""Последовательный model/dispatch loop без владения Run или transport."""
from copy import deepcopy
import json

from ...openapi.parser import canonical
from ...bounded_json import validate_tree, numeric_metadata
from ...validation import fingerprint, report, schema_issues
from ..resilience import BudgetViolation, EmptyDecision, build_window, tool_result

from .. import runtime as boundary
from .contracts import ToolArgumentsError
from .payload import model_payload
from .model_call import request_model
from .context import initial_context
from .controls import loop_controls
from .validation import validation_checks

def drive_loop(self, run, model, cancel, session, started, prepared, budget):
    """Последовательная dispatch boundary: форма → права → бюджет → эффект.

    Каждая попытка учитывается до вызова. Счётчик повторов проверяется до
    инструмента; исходный goal и условия не меняются при correction.
    Отказ tokenizer/journal/validator прекращает новые сетевые действия.
    """
    prepared_map = prepared if self.workflow else None
    if self.workflow:
        prepared = prepared_map[self.initial_operation]
    deadline = started + self.limits.max_seconds
    if hasattr(model, "set_deadline"):
        model.set_deadline(session.deadline)
    history, registry = initial_context(self, run, model)
    run["history"] = history
    counter = self.token_counter or getattr(model, "count_tokens", None)
    run["resources"] = budget.snapshot()
    seen, repeats = set(), {}
    expectations_hash = fingerprint(run["expectations"])
    manifest_hash = fingerprint(run.get('admission_manifest'))
    validation_stamp = exchange_stamp = chain_validation_stamp = None
    # Это исходное основание, не отпечаток маскированного DTO. Секретное
    # ожидаемое значение проверяется до redaction и не заменяется маской.
    trusted_validation_basis = {
        "schema_version": 1,
        "run_id": run["run_id"],
        "operation_id": self.initial_operation,
        "spec_hash": self.catalog.spec_hash,
        "operation_hash": self.operation_hash,
        "expectations_hash": fingerprint(prepared),
    }

    trusted_check_identity = [
        {
            "expectation_id": e["id"],
            **{k: deepcopy(e[k]) for k in ("source", "predicate", "basis")},
        }
        for e in prepared
    ]

    validation_bound, validation_shape = validation_checks(self, trusted_validation_basis, trusted_check_identity)
    stop, guard, persist, emit, warn = loop_controls(
        self, run, cancel, started, deadline, expectations_hash, manifest_hash, history, budget)

    if not emit("run_started", {"goal_hash": run["goal_hash"]}) or not persist():
        return
    while guard():
        if not warn():
            break
        if run["model_calls"] >= self.limits.max_steps:
            stop("step_limit")
            break
        payload = model_payload(self, run, history, registry, model)
        prompt_bytes = len(canonical(payload))
        if prompt_bytes > self.limits.max_prompt_bytes:
            stop("prompt_limit")
            break
        before = budget.snapshot()
        try:
            input_tokens, reserved = budget.reserve(payload)
        except BudgetViolation as error:
            if guard():
                stop(error.code)
            break
        run["resources"] = budget.snapshot()
        # Предупреждать по резервируемой верхней границе до generation:
        # иначе на 80% следующий вызов уже может не поместиться, и модель
        # никогда не увидит предупреждение, сохранённое лишь в истории.
        history_size = len(history)
        if not guard() or not warn(projected=True):
            break
        if len(history) != history_size:
            budget.release_unsent(reserved)
            run["resources"] = budget.snapshot()
            payload["messages"] = build_window(history)
            prompt_bytes = len(canonical(payload))
            if prompt_bytes > self.limits.max_prompt_bytes:
                stop("prompt_limit")
                break
            try:
                input_tokens, reserved = budget.reserve(payload)
            except BudgetViolation as error:
                if guard():
                    stop(error.code)
                break
            run["resources"] = budget.snapshot()
        if not guard():
            break
        run["model_calls"] += 1
        step = {
            "index": run["model_calls"],
            "action": None,
            "observation": None,
            "prefix_hash": fingerprint({"system": history[0], "tools": payload["tools"]}),
            "offered_tools": [t["function"]["name"] for t in payload["tools"]],
            "tool_choice": deepcopy(payload["tool_choice"]),
            "phase_hint": deepcopy(payload["messages"][-1]) if payload["messages"] != build_window(history) else None,
            "window_hash": fingerprint(payload),
            "prompt_bytes": prompt_bytes,
            "input_tokens_preflight": input_tokens,
            "reserved_tokens": reserved,
            "budget_before": before,
        }
        run["steps"].append(step)
        if not emit("model_requested") or not persist() or not guard():
            break
        received, response = request_model(self, run, model, session, payload, step, budget, emit, guard, stop)
        if not received:
            break
        executing = False
        try:
            validate_tree(response)
            step["timing"] = numeric_metadata(
                response.get("_timing", {}), ("ttft_seconds", "wall_seconds")
            )
            usage = numeric_metadata(
                response.get("usage", {}),
                ("prompt_tokens", "completion_tokens", "total_tokens"),
            )
            budget.settle(usage, payload["max_tokens"])
            run["resources"] = budget.snapshot()
            step["usage"] = usage if usage else None
            if not emit(
                "model_received", {"usage_status": run["resources"]["usage_status"]}
            ):
                break
            if budget.upper >= self.limits.max_tokens_total:
                stop("token_limit")
                break
            call, action = self._decision(response)
            offered = next((tool for tool in payload["tools"] if tool["function"]["name"] == action["name"]), None)
            if offered is None:
                raise ToolArgumentsError("Tool is not offered in this Run. Use the shown operations and available tools; do not repeat a redundant search.")
            if schema_issues(action["arguments"], offered["function"]["parameters"]):
                if action['name'] == 'execute_operation':
                    properties = offered['function']['parameters']['properties']
                    hint = {key: value.get('enum', list(value.get('properties', {}))) for key, value in properties.items()}
                    raise ToolArgumentsError('Use the offered operation and exact input fields: ' + json.dumps(hint, separators=(',', ':')) + '. body=requestBody, parameters=path/query, bindings=issued value refs, credential_ref=issued auth ref. Prerequisites may require login first. Never invent refs.')
                raise ToolArgumentsError("Arguments must match the offered tool schema. Choose evidence_ids exactly from its enum; never rewrite an ID.")
            run["empty_consecutive"] = 0
            step["decision_kind"] = "native_call"
            step["action"] = self.safe(action)
            name, args = action["name"], action["arguments"]
            if self.workflow:
                name = self.workflow.dispatch_name(name)
            action_hash = fingerprint(action)
            repeats[action_hash] = repeats.get(action_hash, 0) + 1
            if repeats[action_hash] > self.limits.max_repeats:
                stop("repeated_action")
                break
            if not guard():
                break
            tool_started = self.clock()
            if name == "finish_run":
                final = session.finish(args)
                if self.workflow:
                    if fingerprint(run["validations"]) != chain_validation_stamp:
                        raise ValueError("chain_validation_changed")
                    run["chain_verdict"] = self.workflow.completion(
                        session, run["validations"]
                    )
                    if run["chain_verdict"] == "not_checked":
                        raise ValueError("unfinished_chain")
                validation = run["validation"]
                if (
                    not isinstance(validation, dict)
                    or validation_stamp != fingerprint(validation)
                    or exchange_stamp != fingerprint(run["exchanges"])
                    or not validation_bound(
                        validation,
                        final["evidence_ids"][-1 if self.workflow else 0],
                        redacted=True,
                    )
                ):
                    raise ValueError("unverified_finish")
                step["observation"] = tool_result(
                    {"finished": True}, evidence_ids=final["evidence_ids"]
                )
                stop(
                    "finished",
                    "completed" if validation["task_postcondition"] or run.get('chain_verdict') in {'pass','fail'} else "partial",
                )
                run["final"] = self.safe(
                    {**final, "summary": report(run)["summary"]}
                )
                emit("tool_result", step["observation"])
                break
            elif (
                name == "search_operations"
                and self.workflow
                and hasattr(self.workflow, "discovery")
            ):
                observation = self.workflow.discovery(
                    run["run_id"], args, call=call
                )
                result = tool_result(
                    observation, provenance={"source": "RUN_CATALOG"}
                )
            elif self.workflow and name in getattr(self.workflow, 'application_tools', ()):
                observation = self.workflow.handle_tool(session, name, args)
                result = tool_result(observation, provenance={'source': 'APPLICATION', 'run_id': run['run_id']})
            elif name == "read_response" and self.workflow and hasattr(self.workflow, "read_response"):
                observation = self.workflow.read_response(session, args)
                result = tool_result(observation, evidence_ids=[args["exchange_id"]], provenance={"source": "HTTP_EXCHANGE", "run_id": run["run_id"]})
            elif name == "inspect_operation":
                inspected_id = args["operation_id"]
                operation = self.catalog.operations[inspected_id]
                observation = {
                    "operation": {
                        "operation_id": inspected_id,
                        "method": operation["method"],
                        "path": operation["path"],
                        "response_statuses": sorted(operation["responses"]),
                    }
                }
                if self.workflow:
                    observation = self.workflow.inspect(
                        self.catalog,
                        inspected_id,
                        **(
                            {"section": args.get("section", "inputs")}
                            if hasattr(self.workflow, "inspect_sections")
                            else {}
                        ),
                    )
                if self._active_state:
                    # Schema пришла от workflow.inspect, а не из HTTP/model.
                    # Регистрация exact DTO не доверяет одноимённым data keys.
                    for key in ('catalog_operation', 'operation'):
                        if key in observation:
                            self._active_state.trust_public_catalog(observation[key])
                result = tool_result(
                    observation,
                    provenance={
                        "source": "CONTRACT",
                        "operation_hash": self.catalog.operation_hashes[
                            inspected_id
                        ],
                    },
                )
            else:
                mutation = self.catalog.operations[args['operation_id']]['method'] not in {'GET', 'HEAD', 'OPTIONS'}
                if mutation:
                    unresolved = self._unresolved_mutations(run)
                    if unresolved:
                        run.update(blocking_uncertain_runs=unresolved, primary_safety_cause='uncertain_write')
                        stop('unresolved_uncertain_write')
                        emit('http_blocked', {'reason': 'unresolved_uncertain_write', 'blocking_run_ids': unresolved})
                        break
                if self.workflow:
                    session.admit(args)
                if run["requests"] >= self.limits.max_requests:
                    stop("request_limit")
                    break
                if mutation:
                    # Фиксируется ДО HTTP. Новый Message не является
                    # разрешением повторить эффект, потерянный при crash.
                    run['pending_mutation'] = {'operation_id': args['operation_id'],
                        'request_index': session.requests + 1}
                if (
                    not persist()
                    or not emit(
                        "http_prepared", {"expectations_hash": expectations_hash}
                    )
                    or not guard()
                ):
                    break
                run["requests"] += 1
                executing = True
                if self.workflow:
                    key = self.workflow.prepared_key(args) if hasattr(self.workflow, 'prepared_key') else args['operation_id']
                    prepared = prepared_map[key]
                    trusted_validation_basis.update(
                        operation_id=args["operation_id"],
                        operation_hash=self.catalog.operation_hashes[
                            args["operation_id"]
                        ],
                        expectations_hash=fingerprint(prepared),
                    )
                    trusted_check_identity[:] = [
                        {
                            "expectation_id": e["id"],
                            **{
                                k: deepcopy(e[k])
                                for k in ("source", "predicate", "basis")
                            },
                        }
                        for e in prepared
                    ]
                exchange = session.execute(args)
                if mutation and not getattr(session, 'uncertain', False):
                    run.pop('pending_mutation', None)
                validation = boundary.validate_exchange(self.catalog, exchange, prepared)
                if not isinstance(validation, dict):
                    raise RuntimeError("validator_missing")
                run["exchanges"].append(self.safe(exchange))
                if not validation_shape(validation, prepared) or not validation_bound(
                    validation, exchange["id"]
                ):
                    # Непроверенный результат не публикуется, даже как
                    # предварительный verdict. Сохраняется сам HTTP exchange.
                    executing = False
                    stop("validation_binding_error", "failed")
                    emit(
                        "validation_rejected", {"error_code": "invariant_violated"}
                    )
                    break
                if self.workflow:
                    run["validations"].append(self.safe(validation))
                    chain_validation_stamp = fingerprint(run["validations"])
                run["validation"] = self.safe(validation)
                validation_stamp = fingerprint(run["validation"])
                exchange_stamp = fingerprint(run["exchanges"])
                executing = False
                observation = {
                    "exchange_id": exchange["id"],
                    "outcome": exchange["outcome"],
                    "status": (exchange["response"] or {}).get("status"),
                    "validation": {
                        k: validation[k]
                        for k in (
                            "contract_verdict",
                            "intent_verdict",
                            "task_postcondition",
                        )
                    },
                }
                if self.workflow:
                    observation = self.workflow.observation(
                        session, exchange, validation
                    )
                infrastructure = exchange["outcome"] not in {
                    "received",
                    "invalid_json",
                    "invalid_content_type",
                }
                code = (
                    {
                        "time_limit": "timeout",
                        "response_limit": "too_large",
                        "response_structure_error": "too_large",
                        "redirect_denied": "permission_denied",
                    }.get(exchange["outcome"], "upstream_error")
                    if infrastructure
                    else None
                )
                result = tool_result(
                    None if infrastructure else observation,
                    error_code=code,
                    hint="Ответ не проверен полностью; новый HTTP запрещён лимитом Run.",
                    evidence_ids=[exchange["id"]],
                    provenance={
                        "source": "HTTP",
                        "run_id": run["run_id"],
                        "outcome": exchange["outcome"],
                    },
                    bytes_total=len(canonical(exchange)),
                    truncated=exchange.get("truncated", False),
                    omitted_fields=["response.json"],
                )
            step["tool_duration"] = self.clock() - tool_started
            step["observation"] = self.safe(result)
            if not emit("tool_result", step["observation"]):
                break
            observation_fingerprint = fingerprint(
                {"action": action, "observation": observation}
            )
            if observation_fingerprint in seen:
                stop("no_progress")
                break
            seen.add(observation_fingerprint)
            history.extend(
                [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": self.safe([call]),
                    },
                    {
                        "role": "tool",
                        "tool_call_id": self.safe(call["id"]),
                        "content": self._tool_content(result),
                    },
                ]
            )
        except BudgetViolation as error:
            run["resources"] = budget.snapshot()
            stop(error.code, "failed")
            break
        except EmptyDecision:
            run["empty_steps"] += 1
            run["empty_consecutive"] += 1
            step["decision_kind"] = "empty"
            step["observation"] = tool_result(
                error_code="validation",
                hint="Ответ пустой: выберите один разрешённый инструмент.",
            )
            if not emit("empty_decision", step["observation"]):
                break
            if run["empty_consecutive"] >= self.limits.max_empty_steps:
                stop("empty_limit")
                break
            history.append(
                {
                    "role": "user",
                    "content": "Empty response. Use one offered native tool with exact arguments.",
                }
            )
        except (
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            AttributeError,
            RecursionError,
        ) as error:
            if hasattr(error, "stop_reason"):
                stop(
                    error.stop_reason,
                    "cancelled" if error.stop_reason == "cancelled" else None,
                )
                break
            if executing:
                step["observation"] = tool_result(
                    error_code="invariant_violated",
                    hint="Проверка ответа не завершена; новый HTTP запрещён.",
                )
                stop("runtime_error", "failed")
                break
            run["empty_consecutive"] = 0
            run["malformed_calls"] += 1
            step["decision_kind"] = "malformed"
            correction = getattr(
                error,
                "repair_hint",
                "Invalid tool call. Use one offered tool with exact arguments and actual evidence.",
            )
            if self.workflow and str(error) == "foreign_finish_evidence":
                # Список принадлежит текущему Run; он помогает исправить
                # вызов, но модель всё равно сама должна вызвать finish.
                # Неполное evidence не принимается и HTTP не повторяется.
                correction = (
                    "Finish rejected. evidence_ids must contain ALL exchanges in order: "
                    + json.dumps([e["id"] for e in run["exchanges"]])
                    + ". Call finish_run with this exact list. Do not send another HTTP request."
                )
                if not run["exchanges"]:
                    correction = "Finish rejected: no HTTP evidence exists. Schemas alone do not prove behavior. Use execute_operation for a permitted operation before finish_run."
            step["observation"] = tool_result(
                error_code="validation", hint=correction
            )
            if not emit("invalid_decision", step["observation"]):
                break
            if run["malformed_calls"] > self.limits.malformed_retries:
                stop("malformed_limit")
                break
            history.append(
                {
                    "role": "user",
                    "content": correction,
                }
            )
        if not persist():
            break
        if getattr(session, 'uncertain', False):
            stop('uncertain_write')
            break

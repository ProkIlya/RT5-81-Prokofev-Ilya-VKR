"""Завершение, redaction, safety latch и durable события Run."""
from copy import deepcopy

from ...transport.http import Session
from ...validation import report
from ..resilience import TokenBudget, event_record, stop_category

class RunLifecycle:
    def _loop(self, run, model, cancel, prepared):
        """Гарантировать итоговый Run даже при неожиданном отказе компонента.

        Транспорт уже учитывает попытку до сети и сохраняет exchange в finally.
        На аварийном выходе берём эти счётчики, а не начальный снимок команды.
        Отказ redaction сохраняет лишь собственные безопасные metadata, без body.
        """
        started = self.clock() - getattr(self.workflow, 'preparation_active', 0)
        from ..chains import ChainSession

        session = (
            (
                self.workflow.make_session(self.target, self.catalog, run, self.limits)
                if hasattr(self.workflow, "make_session")
                else ChainSession(
                    self.target, self.catalog, self.workflow, run, self.limits
                )
            )
            if self.workflow
            else Session(
                self.target,
                {"getPet": {"method": "GET", "path": "/pets/1"}},
                run_seconds=self.limits.max_seconds,
            )
        )
        session.run_id = run["run_id"]
        session.cancel = cancel
        if self.workflow:
            self._active_state = session.state
            run["validations"] = []
            run["chain_verdict"] = "not_checked"
        budget = getattr(self.workflow, 'preparation_budget', None) or TokenBudget(
            self.limits, self.token_counter or getattr(model, 'count_tokens', None))
        try:
            if run["stop_reason"] is None:
                self._drive_loop(run, model, cancel, session, started, prepared, budget)
        except Exception:
            run["stop_reason"] = "runtime_error"
            run["status"] = "failed"
        finally:
            run["requests"] = session.requests
            # Если процесс дошёл до finally и транспорт не оставил uncertain,
            # известный ответ либо отмена до отправки закрывают prepared marker.
            # При crash этот finally не выполняется: marker остаётся durable.
            if not getattr(session, 'uncertain', False):
                run.pop('pending_mutation', None)
            if len(run["exchanges"]) != len(session.exchanges):
                run["exchanges"] = []
                for exchange in session.exchanges:
                    try:
                        run["exchanges"].append(self.safe(exchange))
                    except (ValueError, RecursionError):
                        run["exchanges"].append(
                            {
                                "id": exchange["id"],
                                "run_id": run["run_id"],
                                "operation_id": exchange["operation_id"],
                                "response": None,
                                "outcome": "response_structure_error",
                            }
                        )
            run["wall_seconds"] = self.clock() - started
            if getattr(self.workflow, 'human', None) is not None:
                run['human_wait_seconds'] = self.workflow.human.waited_seconds
                run['elapsed_wall_seconds'] = run['wall_seconds'] + run['human_wait_seconds']
                run['human_events'] = self.workflow.human.public_events()
                run['total_model_calls'] = run['model_calls'] + run.get('preparation', {}).get('generator_calls', 0) + run.get('preparation', {}).get('metadata_calls', 0)
            self._preserve_safety(run, session)
            if not run["stop_reason"]:
                run["stop_reason"] = "runtime_error"
                run["status"] = "failed"
            # Сначала проверить snapshot sink, затем объявлять завершение в
            # журнале. Отказ финального сохранения не должен выглядеть done.
            run["stop_category"] = stop_category(run)
            run["resources"]["wall_seconds"] = run["wall_seconds"]
            run["report"] = self.safe(report(run))
            if (run.get('admission_manifest') or {}).get('goal_contract'):
                from ...goals.feedback import correct_answer
                try:
                    correct_answer(run, model, budget, deadline=started + self.limits.max_seconds,
                        clock=self.clock, cancel=cancel, diagnostic=getattr(self.workflow, 'answer_diagnostic', False),
                        safe=self.safe, persist=lambda: self._persist_snapshot(run))
                except Exception:
                    # Ошибка пояснения не меняет проверенный исход API.
                    run['answer_feedback'] = {'status': 'canonical', 'calls': 0,
                        'diagnostic': {'status': 'unavailable'}, 'revision': {'status': 'unavailable'}}
                run['wall_seconds'] = self.clock() - started
                run['resources']['wall_seconds'] = run['wall_seconds']
                run['elapsed_wall_seconds'] = run['wall_seconds'] + run.get('human_wait_seconds', 0)
                run['total_model_calls'] = budget.calls
                run['report'] = self.safe(report(run))
                if run.get('final'):
                    run['final'].update(summary=run['report']['summary'],
                        claims=[{'condition_id': c['condition_id'], 'verdict': c['verdict']}
                            for c in run['report']['answer']['conditions']],
                        answer_fields=deepcopy(run['report']['answer']['answer_fields']))
            if self.summary_reviewer is not None:
                # Новый контекст без API tools; тот же deadline и total budget.
                # Диагноз LLM не получает права переписать validation/вердикт.
                try:
                    reviewed = self.summary_reviewer.review(
                        self._safe_run(run), self.limits, self.token_counter or getattr(model, 'count_tokens', None),
                        started + self.limits.max_seconds,
                    )
                except Exception:
                    reviewed = {'assessment': {'status': 'unavailable', 'verification': 'advisory',
                        'error_code': 'review_failed'}, 'calls': 0}
                run['summary_review'] = self.safe(reviewed)
                run['agent_model_calls'] = run['model_calls']
                run['review_model_calls'] = reviewed['calls']
                run['model_calls'] += reviewed['calls']
                if 'resources' in reviewed:
                    run['resources'] = reviewed['resources']
                run['wall_seconds'] = self.clock() - started
                run['resources']['wall_seconds'] = run['wall_seconds']
                run['report'] = self.safe(report(run))
                self._emit(run, 'summary_review', {'status':reviewed['assessment']['status'],'calls':reviewed['calls']})
            self._preserve_safety(run, session)
            try:
                self._persist_snapshot(run)
            except Exception:
                run.update(status="failed", stop_reason="trace_error")
            run["stop_category"] = stop_category(run)
            self._emit(
                run,
                "run_finished",
                {"status": run["status"], "stop_category": run["stop_category"]},
            )
            run["stop_category"] = stop_category(run)
            self._preserve_safety(run, session)
            run["report"] = self.safe(report(run))
            try:
                self._persist_snapshot(run)
            except Exception:
                run.update(status="failed", stop_reason="trace_error")
                # Последний отказ sink также виден в доступном JSONL. Повтор
                # HTTP/LLM или бесконечные попытки записи не выполняются.
                self._emit(run, "snapshot_error", {"error_code": "invariant_violated"})
            self._preserve_safety(run, session)
            run["stop_category"] = stop_category(run)
            run["report"] = self.safe(report(run))
        if self.workflow:
            run = self._safe_run(run)
            session.exchanges[:] = self.safe(session.exchanges)
            session._headers.clear()
            session.actions.clear()
            session.state.close()
            if hasattr(self.workflow, "close_run"):
                self.workflow.close_run(run["run_id"])
            self._active_state = None
        return run

    def _preserve_safety(self, run, session):
        """Внешний outcome первичен; вторичная ошибка не разрешает replay."""
        if not getattr(session, 'uncertain', False):
            return
        run['uncertain_write'] = True
        run['primary_safety_cause'] = 'uncertain_write'
        if run.get('stop_reason') not in (None, 'uncertain_write'):
            run['secondary_stop_reason'] = run['stop_reason']
        run['stop_reason'] = 'uncertain_write'
        if run['status'] == 'completed':
            run['status'] = 'partial'

    def _unresolved_mutations(self, run):
        """Проверять Project authority перед каждым изменяющим HTTP."""
        if self.history_store:
            return self.history_store.unresolved_mutations(run['project_id'], exclude_run_id=run['run_id'])
        return [entry['run']['run_id'] for entry in self._runs.values()
                if entry['scope'][0] == run['project_id'] and entry['run']['run_id'] != run['run_id']
                and (entry['run'].get('uncertain_write') or entry['run'].get('pending_mutation'))]

    def _persist_snapshot(self, run):
        """Отказ любой durable записи останавливает следующий внешний эффект."""
        snapshot = deepcopy(self._safe_run(run))
        if self.history_store:
            self.history_store.save_snapshot(snapshot)
        self.trace_sink(snapshot)

    def _emit(self, run, kind, details):
        """Сначала durable sink, затем in-memory журнал; ошибка запрещает HTTP."""
        event = self.safe(event_record(run, kind, details))
        if self.workflow:
            event["policy_version"] = getattr(
                self.workflow, "policy_version", "chain-test-v1"
            )
            event["prompt_version"] = getattr(
                self.workflow, "prompt_version", "runtime-chain-v1"
            )
        try:
            if self.history_store:
                self.history_store.append_event(deepcopy(event))
            if self.event_sink is not None:
                self.event_sink(deepcopy(event))
            run["events"].append(event)
            return True
        except Exception:
            run.update(status="failed", stop_reason="journal_error")
            return False

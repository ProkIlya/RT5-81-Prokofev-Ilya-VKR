"""Остановка, guard, журнал и предупреждения одного исполнения."""

from ...validation import fingerprint

def loop_controls(self, run, cancel, started, deadline, expectations_hash, manifest_hash, history, budget):
    def stop(reason, status=None):
        run["stop_reason"] = reason
        run["status"] = status or ("partial" if run["requests"] else "blocked")

    def guard():
        if cancel.is_set():
            stop("cancelled", "cancelled")
            return False
        if self.clock() >= deadline:
            stop("time_limit")
            return False
        if getattr(self.workflow, 'auth', None) != self._accepted_auth:
            stop('expectation_changed', 'failed')
            return False
        if run["goal_hash"] != fingerprint(
            self.safe(run["goal"])
        ) or expectations_hash != fingerprint(run["expectations"]):
            stop("expectation_changed", "failed")
            return False
        if self.workflow and hasattr(self.workflow, 'runtime_manifest') and (
            fingerprint(run.get('admission_manifest')) != manifest_hash or
            fingerprint(self.workflow.runtime_manifest()) != manifest_hash):
            stop('expectation_changed', 'failed')
            return False
        return True

    def persist():
        try:
            self._persist_snapshot(run)
            return True
        except Exception:
            stop("trace_error", "failed")
            return False

    def emit(kind, details=None):
        return self._emit(run, kind, details or {})

    def warn(*, projected=False):
        values = {
            "steps": (run["model_calls"] + int(projected)) / self.limits.max_steps,
            "time": (self.clock() - started) / self.limits.max_seconds,
            "tokens": budget.upper / self.limits.max_tokens_total,
        }
        for resource, ratio in values.items():
            if ratio >= 0.8 and resource not in run["warnings"]:
                run["warnings"].append(resource)
                history.append(
                    {
                        "role": "user",
                        "content": "budget_warning:"
                        + resource
                        + ". Resource at 80% or more. Finish supported checks; hard limits remain mandatory.",
                    }
                )
                if not emit(
                    "budget_warning", {"resource": resource, "ratio": ratio}
                ):
                    return False
        return True

    return stop, guard, persist, emit, warn

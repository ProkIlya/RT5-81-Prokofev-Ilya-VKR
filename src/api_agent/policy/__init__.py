"""Права Project и подтверждение конкретного эффекта до сетевого соединения.

OpenAPI не выдаёт полномочия. Конфигурация создаётся владельцем, снимок Run
не расширяется; актуальный отзыв проверяется заново непосредственно перед HTTP.
"""

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import hmac
import secrets
import json
from ..bounded_json import validate_tree
from ..first_http import loopback_origin
from ..openapi.parser import canonical
from ..validation import fingerprint
from ..agent.chains import ChainSession


class PolicyStop(ValueError):
    """Безопасная остановка приложения; не ошибка синтаксиса модели."""

    def __init__(self, reason):
        self.stop_reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ProjectConfig:
    """Неизменные bytes исключают изменение прав через выданную копию."""

    _payload: bytes

    @classmethod
    def create(
        cls, project_id, target, catalog, *, mode, resettable, operations, version=1
    ):
        origin, port = loopback_origin(target)
        if (
            port == 18084
            or mode not in {"READ_ONLY", "SAFE", "TEST"}
            or type(resettable) is not bool
        ):
            raise ValueError("invalid_project_policy")
        if (
            not isinstance(project_id, str)
            or not project_id
            or len(project_id) > 200
            or type(version) is not int
            or version < 1
        ):
            raise ValueError("invalid_config_identity")
        if not isinstance(operations, dict) or len(operations) > 1000:
            raise ValueError("invalid_allowlist")
        granted = {}
        for name, risk in operations.items():
            op = catalog.operations.get(name)
            if op is None or risk not in {"A", "B", "C", "D", "E"}:
                raise ValueError("invalid_operation_grant")
            if risk == "A" and op["method"] not in {"GET", "HEAD"}:
                raise ValueError("unsafe_read_classification")
            if op["path"].split("?")[0] in {"/reset", "/mode", "/control"}:
                raise ValueError("control_operation_denied")
            granted[name] = {
                "operation_hash": catalog.operation_hashes[name],
                "risk": risk,
            }
        return cls(
            canonical(
                {
                    "project_id": project_id,
                    "target": origin,
                    "mode": mode,
                    "resettable": resettable,
                    "version": version,
                    "spec_hash": catalog.spec_hash,
                    "operations": granted,
                }
            )
        )

    @property
    def snapshot(self):
        return json.loads(self._payload)

    @classmethod
    def load(cls, path, catalog):
        """Проверить сохранённые hashes, не переодобрять изменённый контракт."""
        from pathlib import Path

        path = Path(path)
        if path.stat().st_size > 262144:
            raise ValueError("config_limit")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        validate_tree(data)
        if set(data) != {
            "project_id",
            "target",
            "mode",
            "resettable",
            "version",
            "spec_hash",
            "operations",
        }:
            raise ValueError("invalid_project_config")
        config = cls.create(
            data["project_id"],
            data["target"],
            catalog,
            mode=data["mode"],
            resettable=data["resettable"],
            version=data["version"],
            operations={
                name: grant["risk"] for name, grant in data["operations"].items()
            },
        )
        if canonical(data) != config._payload:
            raise ValueError("stale_project_config")
        return config

    def save(self, path):
        """Сохранить конкретные права; файл не содержит auth credentials."""
        from pathlib import Path

        Path(path).write_text(
            json.dumps(self.snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def decision(snapshot, operation_id, operation_hash):
    """Чистая матрица: неизвестная операция/версия никогда не ALLOW."""
    grant = snapshot["operations"].get(operation_id)
    if grant is None or grant["operation_hash"] != operation_hash:
        return "DENY"
    risk, mode = grant["risk"], snapshot["mode"]
    if risk == "A":
        return "ALLOW"
    if mode == "READ_ONLY":
        return "DENY"
    if risk in {"D", "E"} or mode == "SAFE":
        return "CONFIRM"
    return "ALLOW" if snapshot["resettable"] else "DENY"


class PolicyWorkflow:
    """Добавить policy к одному существующему pilot workflow без второго loop."""

    policy_version = "project-policy-v1"

    def __init__(self, workflow, config, *, approval=None, current_config=None):
        self.workflow, self.config = workflow, config
        self.approval = approval
        self.current_config = current_config or (lambda: config)

    def __getattr__(self, name):
        return getattr(self.workflow, name)

    def make_session(self, target, catalog, run, limits):
        return PolicySession(target, catalog, self, run, limits)


class PolicySession(ChainSession):
    """Проверять снимок и текущие запреты до каждого эффекта, включая approval."""

    def __init__(self, target, catalog, workflow, run, limits):
        super().__init__(target, catalog, workflow.workflow, run, limits)
        self.policy_workflow = workflow
        self.pinned = workflow.config.snapshot
        if (
            self.pinned["project_id"] != run["project_id"]
            or self.pinned["target"] != self.target
            or self.pinned["spec_hash"] != catalog.spec_hash
        ):
            raise ValueError("config_scope_mismatch")
        self.run = run
        run["policy_decisions"] = []
        run["policy_config"] = deepcopy(self.pinned)
        self._approved = set()
        self._action_key = secrets.token_bytes(32)

    def _current(self, op_id):
        current = self.policy_workflow.current_config().snapshot
        if (
            current["project_id"] != self.pinned["project_id"]
            or current["target"] != self.target
            or current["spec_hash"] != self.pinned["spec_hash"]
        ):
            raise PolicyStop("config_scope_changed")
        op_hash = self.catalog.operation_hashes[op_id]
        fixed = decision(self.pinned, op_id, op_hash)
        live = decision(current, op_id, op_hash)
        if "DENY" in {fixed, live}:
            self.run["policy_decisions"].append(
                {
                    "operation_id": op_id,
                    "decision": "DENY",
                    "config_version": current["version"],
                }
            )
            raise PolicyStop("policy_denied")
        return current, "CONFIRM" if "CONFIRM" in {fixed, live} else "ALLOW"

    def admit(self, args):
        return self.authorize(args, super().admit(args))

    def authorize(self, args, result):
        """Связать разрешение с полностью подготовленным эффектом."""
        op_id, op, path, body, bound_id, action = result
        current, verdict = self._current(op_id)
        proposal = {
            "project_id": self.pinned["project_id"],
            "run_id": self.run_id,
            "target": self.target,
            "spec_hash": self.pinned["spec_hash"],
            "config_version": current["version"],
            "config_hash": fingerprint(current),
            "risk": current["operations"][op_id]["risk"],
            "operation_id": op_id,
            "operation_hash": self.catalog.operation_hashes[op_id],
            "method": op["method"],
            "path": path,
            "arguments_hash": fingerprint(args),
            "body_hash": hmac.new(
                self._action_key, body or b"", hashlib.sha256
            ).hexdigest(),
        }
        preview = {"method": op["method"], "path": path}
        if hasattr(self, '_request_principal'):
            proposal['request_principal'] = deepcopy(self._request_principal)
        if body is not None:
            private_body = (
                getattr(self.workflow, "auth", None) is not None
                and op_id == self.workflow.auth["login_operation"]
            ) or (
                not hasattr(self.workflow, "auth") and hasattr(self.workflow, "secrets")
            )
            if private_body:
                preview["credential_body_omitted"] = True
            else:
                preview["json"] = json.loads(body)
        proposal["preview"] = preview
        proposal["action_hash"] = fingerprint(proposal)
        proof = proposal["action_hash"]
        if proof not in self._approved:
            self.run["policy_decisions"].append({**proposal, "decision": verdict})
            if verdict == "CONFIRM":
                callback = self.policy_workflow.approval
                if callback is None:
                    raise PolicyStop("approval_required")
                response = callback(
                    {**deepcopy(proposal), "approval_timeout_seconds": self.remaining()}
                )
                if (
                    not isinstance(response, dict)
                    or response
                    != {
                        "action_hash": proof,
                        "config_version": current["version"],
                        "approved": True,
                    }
                    or type(response.get("approved")) is not bool
                ):
                    raise PolicyStop("approval_rejected")
                # Отзыв/ужесточение во время пользовательского решения не обходятся.
                now, now_verdict = self._current(op_id)
                if fingerprint(now) != fingerprint(current):
                    raise PolicyStop("stale_approval")
            self._approved.add(proof)
        principal = getattr(self, '_request_principal', None)
        explicit_old = (principal is not None and principal['mode'] == 'old_epoch'
                        and hasattr(self.workflow, 'old_epoch_allowed')
                        and self.workflow.old_epoch_allowed(self, args, principal))
        if principal is not None and ((principal['auth_epoch'] != self.state.auth_epoch and not explicit_old) or
            (principal['mode'] == 'authenticated' and principal['actor'] != self.state.principal)):
            raise PolicyStop('stale_auth_epoch')
        if getattr(self, "cancel", None) is not None and self.cancel.is_set():
            raise PolicyStop("cancelled")
        return result

    def execute(self, args):
        try:
            exchange = super().execute(args)
            if self.catalog.operations[args["operation_id"]]["method"] not in {
                "GET",
                "HEAD",
                "OPTIONS",
            }:
                uncertain = exchange["outcome"] not in {
                    "received",
                    "invalid_json",
                    "invalid_content_type",
                }
                exchange["effect_status"] = (
                    "uncertain" if uncertain else "response_received"
                )
                self.uncertain = self.uncertain or uncertain
                self.run["uncertain_write"] = self.uncertain
            return exchange
        finally:
            self._approved.clear()

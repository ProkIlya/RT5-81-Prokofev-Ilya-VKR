"""Immutable Project rights и актуальное решение перед действием."""
from dataclasses import dataclass
import json
from api_agent.bounded_json import validate_tree
from api_agent.transport.http import loopback_origin
from api_agent.openapi.parser import canonical

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

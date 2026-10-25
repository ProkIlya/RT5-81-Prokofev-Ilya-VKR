"""Типизированные значения одного Run, независимые от сообщений модели.

Программа является владельцем записей: модель выбирает только существующую
ссылку. Исходный JSON не становится долговременным фактом Project; provenance
и scope проверяются при извлечении, ссылка другого запуска не разрешается.
"""

from copy import deepcopy
import uuid
import json
import hashlib
from ..openapi.parser import pointer_get
from ..bounded_json import validate_tree


class RunState:
    """Закрытое ограниченное хранилище scalar values с непрозрачными ссылками."""

    def __init__(self, project_id, run_id, rules):
        validate_tree(rules, max_bytes=16384)
        self.project_id, self.run_id = project_id, run_id
        self.rules = deepcopy(rules)
        self._records = {}
        self._secrets = set()
        self._public_catalog_hashes = set()
        self.auth_epoch = 0
        self.principal = None
        self.auth_revocations = 0

    def begin_auth(self, principal):
        """Приложение открывает новую эпоху; старые refs остаются историей."""
        self.auth_epoch += 1
        self.principal = principal

    def invalidate_auth(self):
        """Отзыв не удаляет evidence и никогда не оживляет старый credential."""
        self.auth_epoch += 1
        self.auth_revocations += 1
        self.principal = None

    def remember(self, exchange, path, *, authenticate_as=None):
        """Извлечь только заранее разрешённый путь из ответа собственного Run."""
        if exchange["run_id"] != self.run_id:
            raise ValueError("foreign_run")
        rule = self.rules.get(exchange["operation_id"], {}).get(path)
        if rule is None:
            raise ValueError("extraction_path_denied")
        if rule.get("secret"):
            self.protect(exchange)
        try:
            value = pointer_get(exchange["response"]["json"], path)
        except (KeyError, IndexError, TypeError):
            raise ValueError("extraction_value_missing") from None
        expected = {"integer": int, "string": str, "boolean": bool}.get(rule["type"])
        if expected is None or type(value) is not expected:
            raise ValueError("extraction_type_mismatch")
        if rule.get("secret"):
            self.protect(exchange)
            if not value or len(value) > 4096 or any(c in value for c in ("\r", "\n")):
                raise ValueError("invalid_secret_value")
        if len(self._records) >= 32:
            raise ValueError("state_limit")
        if authenticate_as is not None:
            status = exchange['response'].get('status')
            if (not rule.get('secret') or not isinstance(authenticate_as, str) or not authenticate_as
                or type(status) is not int or not 200 <= status < 300
                or exchange.get('outcome') != 'received' or exchange.get('truncated')):
                raise ValueError('authentication_response_denied')
        ref = "value-" + uuid.uuid4().hex[:16]
        while ref in self._records:
            ref = "value-" + uuid.uuid4().hex[:16]
        record = {
            "ref": ref,
            "operation_id": exchange["operation_id"],
            "exchange_id": exchange["id"],
            "path": path,
            "type": rule["type"],
            "secret": bool(rule.get("secret")),
            "value": deepcopy(value),
            **({'occurrence_id': exchange['occurrence_id']} if 'occurrence_id' in exchange else {}),
        }
        if authenticate_as is not None:
            self.begin_auth(authenticate_as)
        if rule.get('secret'):
            record.update(auth_epoch=self.auth_epoch, principal=self.principal)
        self._records[ref] = record
        return self._public_record(self._records[ref])

    def resolve(self, reference, kind, *, secret=False):
        """Разрешить явную ссылку; никаких fallback к последнему созданному ID."""
        record = self._records.get(reference)
        if record is None or record["type"] != kind or record["secret"] != secret:
            raise ValueError("runtime_reference_denied")
        return deepcopy(record["value"])

    def public(self):
        """Вернуть независимую безопасную проекцию, не authority dictionary."""
        return [self._public_record(record) for record in self._records.values()]

    @staticmethod
    def _public_record(record):
        """Секрет получает только ссылку и provenance, значение не возвращается."""
        return {
            k: deepcopy(v)
            for k, v in record.items()
            if k != "value" or not record["secret"]
        }

    def protect(self, exchange):
        """Классифицировать secret paths до validator/ошибок, даже при плохом типе.

        Это не успешное извлечение: malformed token остаётся недопустимым.
        Сырые листья нужны только для обязательной редакции диагностик.
        """
        if exchange["run_id"] != self.run_id:
            raise ValueError("foreign_run")

        def strings(value):
            if isinstance(value, str) and value:
                self._secrets.add(value)
            elif isinstance(value, dict):
                for v in value.values():
                    strings(v)
            elif isinstance(value, list):
                for v in value:
                    strings(v)

        for path, rule in self.rules.get(exchange["operation_id"], {}).items():
            if rule.get("secret"):
                try:
                    strings(pointer_get(exchange["response"]["json"], path))
                except (KeyError, IndexError, TypeError):
                    pass

    def trust_public_catalog(self, operation):
        """Зарегистрировать exact inspect DTO только из доверенного каталога.

        Метод вызывает приложение после inspect, не модель/HTTP adapter. Hash
        известной публичной декларации переживает JSON copies и не разрешает
        masking exemption по одному похожему имени ключа. Изменённый DTO уже
        не совпадает; literal credentials всё равно проходят строковую редакцию.
        """
        validate_tree(operation, max_bytes=8192)
        if len(self._public_catalog_hashes) >= 32:
            raise ValueError("public_catalog_limit")
        self._public_catalog_hashes.add(self._catalog_hash(operation))

    @staticmethod
    def _catalog_hash(value):
        return hashlib.sha256(
            json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()

    def redact_public_metadata(self, value):
        """Доверенные metadata не являются response body; literals всё ещё скрыты.

        Вызывается приложением только для закреплённого ContractSnapshot.
        Имена полей HTTP и похожие на каталог оболочки такого допуска не дают.
        """
        return self.redact(value, _catalog=True)

    def redact(self, value, *, _catalog=False):
        """Скрыть секретный путь целиком, не полагаясь на допустимый тип token.

        Исходный ответ остаётся у validator. Публичная копия заменяет всю ветвь,
        поэтому числа, null и ключи malformed object также не сериализуются.
        Diagnostics ссылаются на data path: их actual скрывается независимо
        от типа, без глобальной замены чисел в несекретных ID и счётчиках.
        """
        if isinstance(value, str):
            for secret in sorted(self._secrets, key=len, reverse=True):
                value = value.replace(secret, "[REDACTED]")
            return value
        if isinstance(value, dict):
            # Только зарегистрированный приложением exact DTO является schema.
            # Произвольный catalog_operation в response не даёт таких полномочий.
            registered = (
                not _catalog
                and bool(self._public_catalog_hashes)
                and self._catalog_hash(value) in self._public_catalog_hashes
            )
            _catalog = _catalog or registered
            value = dict(value)
            # inspect_operation возвращает доверенную оболочку каталога.
            # Публичная декларация properties.token не является значением
            # response.json/token. Родительские data paths скрываются до
            # обхода детей, поэтому оболочка внутри malformed secret не
            # позволяет обойти маскирование всей секретной ветви.
            paths = (
                set()
                if _catalog
                else {
                    path
                    for rules in self.rules.values()
                    for path, rule in rules.items()
                    if rule.get("secret")
                }
            )
            diagnostic_path = value.get("path")
            if (
                isinstance(diagnostic_path, str)
                and any(
                    diagnostic_path == path or diagnostic_path.startswith(path + "/")
                    for path in paths
                )
                and "actual" in value
            ):
                value["actual"] = "[REDACTED]"
            for path in paths:
                parts = [
                    part.replace("~1", "/").replace("~0", "~")
                    for part in path.lstrip("/").split("/")
                ]
                cursor = value
                try:
                    for part in parts[:-1]:
                        key = int(part) if isinstance(cursor, list) else part
                        child = cursor[key]
                        if not isinstance(child, (dict, list)):
                            raise KeyError(part)
                        cursor[key] = (
                            dict(child) if isinstance(child, dict) else list(child)
                        )
                        cursor = cursor[key]
                    key = int(parts[-1]) if isinstance(cursor, list) else parts[-1]
                    if isinstance(cursor, list) or key in cursor:
                        cursor[key] = "[REDACTED]"
                except (KeyError, IndexError, TypeError, ValueError):
                    pass
            redacted = {
                self.redact(k): self.redact(
                    v,
                    _catalog=_catalog,
                )
                for k, v in value.items()
            }
            if registered:
                # Строковые secrets маскируются и в enum. Безопасный exact DTO
                # сохраняет schema provenance при повторной сериализации safe.
                digest = self._catalog_hash(redacted)
                if (
                    digest not in self._public_catalog_hashes
                    and len(self._public_catalog_hashes) >= 32
                ):
                    raise ValueError("public_catalog_limit")
                self._public_catalog_hashes.add(digest)
            return redacted
        if isinstance(value, list):
            return [self.redact(v, _catalog=_catalog) for v in value]
        return value

    def close(self):
        """Не переносить credentials/ссылки в следующий Run application."""
        self._records.clear()
        self._secrets.clear()
        self._public_catalog_hashes.clear()

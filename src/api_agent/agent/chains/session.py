"""Общий транспорт цепочек: admission, deadline, auth и учёт HTTP."""
import http.client
import json
import time
import uuid
from ...transport.http import loopback_origin, PolicyDenied, reject_json_constant
from ...transport.socket_deadline import SocketDeadline
from ...bounded_json import validate_tree
from ...state import RunState
from ...validation import schema_issues

from .common import RequestLimitExceeded

from .auth_read import AuthRead


class ChainSession:
    """Один loopback origin, закрытый профиль, bounded HTTP, без retries."""

    def __init__(self, target, catalog, workflow, run, limits):
        self.target, self.port = loopback_origin(target)
        self.catalog, self.workflow = catalog, workflow
        self.run_id = run["run_id"]
        self.timeout = 5
        self.max_response_bytes = 65536
        self.deadline = time.monotonic() + limits.max_seconds
        self.requests = 0
        self.max_requests = limits.max_requests
        self.exchanges = []
        self.state = RunState(run["project_id"], self.run_id, workflow.rules())
        self.actions = set()
        self.uncertain = False
        self._headers = {}

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise PolicyDenied("run_time_limit")
        return value

    def admit(self, args):
        """Проверить форму/ссылки/повтор до учёта попытки и сетевого эффекта."""
        self.remaining()
        self.workflow.decision(
            self.workflow.execution_tool(args.get("operation_id")), args
        )
        if self.uncertain:
            raise PolicyDenied("uncertain_write")
        if self.requests >= self.max_requests:
            raise RequestLimitExceeded("request_limit")
        op_id = args["operation_id"]
        op = self.catalog.operations[op_id]
        path = op["path"]
        body = None
        bound_id = None
        if isinstance(self.workflow, AuthRead):
            if op_id == self.workflow.create:
                body = json.dumps(
                    {"login": self.workflow.account, "password": self.workflow.password}
                ).encode()
            else:
                ref = args["credential_ref"]
                token = self.state.resolve(ref, "string", secret=True)
                metadata = next(v for v in self.state.public() if v["ref"] == ref)
                bound_id = next(
                    v["value"]
                    for v in self.state.public()
                    if not v["secret"]
                    and v["exchange_id"] == metadata["exchange_id"]
                    and v["path"] == self.workflow.id_path
                )
                self._headers = {"Authorization": "Bearer " + token}
        elif op_id == self.workflow.create:
            schema = op["requestBody"]["content"]["application/json"]["schema"]
            if schema_issues(args["body"], schema):
                raise PolicyDenied("invalid_request_body")
            body = json.dumps(args["body"]).encode()
        else:
            bound_id = self.state.resolve(args["value_ref"], "integer")
            if not 1 <= bound_id <= 2147483647:
                raise PolicyDenied("runtime_id_out_of_range")
            path = path.replace("{" + self.workflow.parameter + "}", str(bound_id))
        action = (op_id, path, body)
        if action in self.actions:
            raise PolicyDenied("duplicate_semantic_action")
        return op_id, op, path, body, bound_id, action

    def execute(self, args):
        op_id, op, path, body, bound_id, action = self.admit(args)
        self.actions.add(action)
        self.requests += 1
        exchange = {
            "id": str(uuid.uuid4()),
            "run_id": self.run_id,
            "operation_id": op_id,
            "request": {"method": op["method"], "url": self.target + path},
            "response": None,
            "outcome": "transport_error",
            "bytes_total": 0,
            "truncated": False,
            "bound_id": bound_id,
        }
        if body is not None:
            if isinstance(self.workflow, AuthRead):
                exchange["request"]["credential_body_omitted"] = True
            else:
                exchange["request"]["json"] = json.loads(body)
        if isinstance(self.workflow, AuthRead) and op_id == self.workflow.read:
            exchange["request"]["credential_ref"] = args["credential_ref"]
        started = time.monotonic()
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=min(self.timeout, self.remaining())
        )
        try:
            connection.connect()
            with SocketDeadline(
                connection.sock, min(self.deadline, started + self.timeout)
            ) as watchdog:
                headers = {"Accept": "application/json", **self._headers}
                if body is not None:
                    headers["Content-Type"] = "application/json"
                connection.request(op["method"], path, body=body, headers=headers)
                response = connection.getresponse()
                watchdog.check()
                exchange["response"] = {
                    "status": response.status,
                    "content_type": response.getheader("Content-Type", ""),
                    "json": None,
                    "json_valid": False,
                }
                if 300 <= response.status < 400:
                    exchange["outcome"] = "redirect_denied"
                else:
                    chunks = []
                    size = 0
                    while size <= self.max_response_bytes:
                        chunk = response.read1(
                            min(4096, self.max_response_bytes + 1 - size)
                        )
                        watchdog.check()
                        if not chunk:
                            break
                        chunks.append(chunk)
                        size += len(chunk)
                    exchange["bytes_total"] = size
                    if size > self.max_response_bytes:
                        exchange.update(outcome="response_limit", truncated=True)
                    elif response.length is not None and response.length > 0:
                        exchange.update(outcome="incomplete_response", truncated=True)
                    else:
                        try:
                            value = json.loads(
                                b"".join(chunks), parse_constant=reject_json_constant
                            )
                            validate_tree(value, max_bytes=self.max_response_bytes)
                        except (json.JSONDecodeError, UnicodeError):
                            exchange["outcome"] = "invalid_json"
                        except (ValueError, RecursionError):
                            exchange["outcome"] = "response_structure_error"
                        else:
                            exchange["response"].update(json=value, json_valid=True)
                            exchange["outcome"] = "received"
                    if (
                        exchange["outcome"] in {"received", "invalid_json"}
                        and exchange["response"]["content_type"]
                        .split(";")[0]
                        .strip()
                        .lower()
                        != "application/json"
                    ):
                        exchange["outcome"] = "invalid_content_type"
        except (OSError, http.client.HTTPException):
            exchange["outcome"] = "transport_error"
        finally:
            connection.close()
            exchange["wall_seconds"] = time.monotonic() - started
            if op["method"] not in {'GET', 'HEAD', 'OPTIONS'} and exchange["outcome"] not in {
                "received",
                "invalid_json",
                "invalid_content_type",
            }:
                self.uncertain = True
                exchange['effect_status'] = 'uncertain'
            self.exchanges.append(exchange)
        self.state.protect(exchange)
        self._headers.clear()
        return exchange

    def finish(self, args):
        self.remaining()
        if (
            args["evidence_ids"] != [e["id"] for e in self.exchanges]
            or not self.exchanges
        ):
            raise PolicyDenied("foreign_finish_evidence")
        return {**args, "run_id": self.run_id}

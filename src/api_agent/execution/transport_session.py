"""Общий транспорт цепочек: admission, deadline, auth и учёт HTTP."""
import http.client
import json
import re
import time
import uuid
from api_agent.transport.http import loopback_origin, PolicyDenied, reject_json_constant
from api_agent.transport.socket_deadline import SocketDeadline
from api_agent.bounded_json import validate_tree
from api_agent.execution.state import RunState
from api_agent.validation import schema_issues

from api_agent.execution.contracts import RequestLimitExceeded
from api_agent.execution.http_metadata import request_headers,classified_headers



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
        self.active_interval_remaining = None

    def remaining(self):
        value = self.deadline - time.monotonic()
        if self.active_interval_remaining is not None:
            value = min(value, self.active_interval_remaining())
        if value <= 0:
            raise PolicyDenied("run_time_limit")
        return value

    def admit(self, args):
        """Допуск реализует CatalogSession; native profiles удалены."""
        raise PolicyDenied("operation_denied")

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
            exchange["request"]["json"] = json.loads(body)
        started = time.monotonic()
        timeout = min(self.timeout, self.remaining())
        connection = http.client.HTTPConnection(
            "127.0.0.1", self.port, timeout=timeout
        )
        headers=request_headers(self.port,op['method'],body,self._headers)
        response_headers=None;watchdog=None;raw_content_type=''
        try:
            connection.connect()
            with SocketDeadline(
                connection.sock, min(self.deadline, started + timeout)
            ) as watchdog:
                connection.request(op["method"], path, body=body, headers=headers)
                response = connection.getresponse()
                response_headers=response.getheaders()
                raw_content_type=response.getheader('Content-Type','')
                exchange["response"] = {
                    "status": response.status,
                    "content_type": '',
                    "content_type_is_json": raw_content_type.split(';')[0].strip().lower()=='application/json',
                    "json": None,
                    "json_valid": False,
                }
                watchdog.check()
                challenge = response.getheader('WWW-Authenticate', '')
                auth_error = re.search(r'^Bearer\s+.*?\berror="(invalid_token|insufficient_scope)"', challenge, re.IGNORECASE)
                if auth_error: exchange['response']['auth_error'] = auth_error.group(1).lower()
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
                        and raw_content_type
                        .split(";")[0]
                        .strip()
                        .lower()
                        != "application/json"
                    ):
                        exchange["outcome"] = "invalid_content_type"
        except (OSError, http.client.HTTPException) as error:
            exchange["outcome"] = "transport_error"
            exchange['transport_error']=('timeout' if isinstance(error,TimeoutError)
                or watchdog is not None and watchdog.expired else 'disconnect'
                if isinstance(error,(http.client.RemoteDisconnected,ConnectionError)) else 'io_error')
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
        def redact(value):
            value=self.state.redact(value)
            for secret in sorted(self.workflow.secrets(),key=len,reverse=True):
                if secret:value=value.replace(secret,'[REDACTED]')
            return value
        exchange['request']['headers']=classified_headers(headers.items(),
            public=[p['name'] for p in op['parameters'] if p['in']=='header'],redact=redact)
        if response_headers is not None:
            exchange['response']['headers']=classified_headers(response_headers,
                public=self.workflow.public_response_headers,redact=redact)
            # Alias проходит ту же классификацию независимо от omitted в общем списке.
            alias=classified_headers([('Content-Type',raw_content_type)],redact=redact)
            exchange['response']['content_type']=alias['items'][0]['value']
            if alias['truncated']:exchange['response']['content_type_truncated']=True
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

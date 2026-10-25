"""Допуск и исполнение одного запроса каталога."""
from copy import deepcopy
import json
from urllib.parse import quote, urlencode
from ...policy import PolicySession
from ...validation import schema_issues
from ...agent.chains import RequestLimitExceeded

from .types import CatalogArgumentsError

class CatalogSession(PolicySession):
    """Общий binding до policy и bounded транспорта существующего ChainSession."""

    def __init__(self, target, catalog, workflow, run, limits):
        super().__init__(target, catalog, workflow, run, limits)
        self._bound_inputs = {}
        self._selection_anchors = {}
        run["run_catalog"] = {
            "selection_scopes": deepcopy(workflow.selection_scopes),
            "allowed_ids": list(workflow.allowed),
            "operation_hashes": {
                n: catalog.operation_hashes[n] for n in workflow.allowed
            },
            "spec_hash": catalog.spec_hash,
            "config_version": self.pinned["version"],
            "exclusions": deepcopy(workflow.excluded),
            'occurrences': deepcopy(workflow.occurrences),
        }

    def admit(self, args):
        self.remaining()
        if self.uncertain:
            from ...policy import PolicyStop

            raise PolicyStop("uncertain_write")
        if self.requests >= self.max_requests:
            raise RequestLimitExceeded("request_limit")
        self.workflow.decision("execute_operation", args)
        name = args["operation_id"]
        op = self.catalog.operations[name]
        slot = self.workflow.next_occurrence(self.exchanges)
        if self.workflow.occurrences:
            if slot is None or args.get('occurrence_id') != slot['id'] or name != slot['operation_id']:
                raise ValueError('occurrence_denied')
            if any(args.get(k) != slot[k] for k in ('body', 'parameters') if k in slot):
                raise ValueError('occurrence_inputs_changed')
        elif 'occurrence_id' in args:
            raise ValueError('unexpected_occurrence')
        supplied = deepcopy(args.get("parameters", {}))
        bindings = args.get("bindings", {})
        descriptors = {(p['in'] + '__' + p['name'] if self.workflow.parameter_namespaces else p['name']): p
            for p in op['parameters']}
        if (
            not set(supplied) <= set(descriptors)
            or not set(bindings) <= set(descriptors)
            or set(supplied) & set(bindings)
        ):
            body_fields = set(op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema", {}).get("properties", {}))
            misplaced = sorted(set(supplied) - set(descriptors))
            if misplaced and set(misplaced) <= body_fields:
                raise CatalogArgumentsError("body_fields_in_parameters", "Use top-level body for JSON requestBody fields: " + ", ".join(misplaced) + ". parameters is path/query ONLY. Do not repeat this call shape.")
            raise ValueError("invalid_parameters")
        self._bound_inputs = {}
        self._request_parameters = {}
        rules = self.workflow.bindings_for(name, args.get('occurrence_id'))
        for key, rule in rules.items():
            if key in supplied or key not in bindings:
                refs = [
                    v["ref"]
                    for v in self.state.public()
                    if v["operation_id"] == rule["source_operation"]
                    and v["path"] == rule["source_path"]
                    and not v["secret"]
                ]
                hint = (
                    "Call execute_operation with exact top-level JSON: "
                    + json.dumps(
                        {"operation_id": name, "bindings": {key: refs[0]}},
                        separators=(",", ":"),
                    )
                    + ". No body or parameters for this read."
                    if len(refs) == 1
                    else "Use top-level bindings for "
                    + name
                    + ". Available refs: "
                    + ", ".join(refs)
                )
                raise CatalogArgumentsError("explicit_binding_required", hint)
        for key, ref in bindings.items():
            rule = rules.get(key)
            record = next((v for v in self.state.public() if v["ref"] == ref), None)
            if (
                rule is None
                or record is None
                or record["operation_id"] != rule["source_operation"]
                or record["path"] != rule["source_path"]
                or ('source_occurrence' in rule and record.get('occurrence_id') != rule['source_occurrence'])
            ):
                raise ValueError("binding_source_denied")
            supplied[key] = self.state.resolve(ref, descriptors[key]["schema"]["type"])
            self._bound_inputs[key] = supplied[key]
        path = op["path"]
        query = []
        parameter_headers = {}
        for key, p in descriptors.items():
            if key not in supplied:
                if p["required"]:
                    raise ValueError("required_parameter_missing")
                continue
            value = supplied[key]
            if schema_issues(value, p["schema"]):
                raise CatalogArgumentsError(
                    "invalid_parameter_type",
                    "Parameter "
                    + key
                    + " must have JSON type "
                    + str(p["schema"]["type"])
                    + ". Integers are numbers, not quoted strings. Use bindings for runtime references.",
                )
            text = str(value).lower() if type(value) is bool else str(value)
            if (
                len(text) > 2048
                or text in {".", ".."}
                or any(c in text for c in ("\r", "\n", "\x00"))
            ):
                raise ValueError("unsafe_parameter_value")
            if p["in"] == "path":
                # Запрет encoded traversal/разделителей даже после серверного decode.
                if any(c in text for c in ("/", "\\", "%", "?", "#")):
                    raise ValueError("unsafe_path_parameter")
                path = path.replace("{" + p['name'] + "}", quote(text, safe=""))
            elif p['in'] == 'query':
                query.append((p['name'], text))
            else:
                parameter_headers[p['name']] = text
        if query:
            path += "?" + urlencode(query)
        body = args.get("body")
        auth = self.workflow.auth
        if auth and name == auth["login_operation"]:
            if body is not None:
                raise ValueError("credential_body_is_private")
            body = deepcopy(auth["body"])
        if "requestBody" in op:
            request = op["requestBody"]
            if body is None and request.get("required"):
                fields = request["content"]["application/json"]["schema"].get(
                    "required", []
                )
                raise CatalogArgumentsError(
                    "required_body_missing",
                    "Required JSON body for "
                    + name
                    + ". Put these fields in body, NOT parameters: "
                    + ", ".join(fields)
                    + ". Inspect operation schema.",
                )
            if body is not None and schema_issues(
                body, request["content"]["application/json"]["schema"]
            ):
                raise ValueError("invalid_request_body")
        elif body is not None:
            raise ValueError("body_denied")
        encoded = (
            None
            if body is None
            else json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
        )
        if encoded and len(encoded) > 16384:
            raise ValueError("request_body_limit")
        self._headers = parameter_headers
        mode = self.workflow.request_mode(name, slot)
        self._request_principal = {'mode': mode, 'auth_epoch': self.state.auth_epoch,
                                   'actor': None}
        if mode == 'authenticated':
            ref = args.get("credential_ref")
            if ref is None:
                raise CatalogArgumentsError("credential_ref_required", "credential_ref is required. First execute " + auth["login_operation"] + " with operation_id only; application supplies its private body. Then use the issued credential_ref. Never invent a token.")
            record = next((v for v in self.state.public() if v["ref"] == ref), None)
            if (
                record is None
                or record["operation_id"] != auth["login_operation"]
                or record["path"] != auth["token_path"]
                or record.get('auth_epoch') != self.state.auth_epoch
                or record.get('principal') != self.state.principal
                or self.state.principal is None
            ):
                raise ValueError("auth_reference_denied")
            self._request_principal['actor'] = self.state.principal
            self._headers = {**parameter_headers,
                "Authorization": "Bearer "
                + self.state.resolve(ref, "string", secret=True)
            }
        elif "credential_ref" in args:
            raise ValueError("unexpected_auth_reference")
        from ..admission import admit_conditions
        admit_conditions(self, name, args, supplied, body)
        epoch = (self.state.auth_revocations if auth and name == auth['login_operation']
                 else self.state.auth_epoch if mode == 'authenticated' else self.state.auth_revocations)
        action = (name, path, encoded, mode, epoch)
        if self.workflow.parameter_namespaces:
            action += (tuple(sorted(parameter_headers.items())),)
        if action in self.actions:
            raise ValueError("duplicate_semantic_action")
        self._request_parameters = deepcopy(supplied)
        return self.authorize(args, (name, op, path, encoded, None, action))

    def execute(self, args):
        exchange = super().execute(args)
        exchange['request_principal'] = deepcopy(self._request_principal)
        if self.workflow.occurrences:
            exchange['occurrence_id'] = args['occurrence_id']
        exchange["bound_inputs"] = deepcopy(self._bound_inputs)
        name = exchange['operation_id']
        actual = deepcopy(self._request_parameters)
        exchange['request_parameters'] = actual
        anchor = args.get('occurrence_id', name)
        exchange['initial_request_parameters'] = deepcopy(self._selection_anchors.setdefault(anchor,actual))
        if (
            self.workflow.auth
            and args["operation_id"] == self.workflow.auth["login_operation"]
        ):
            exchange["request"].pop("json", None)
            exchange["request"]["credential_body_omitted"] = True
        if name in (self.workflow.auth or {}).get('logout_operations', []):
            self.state.invalidate_auth()
            self.run['auth_state'] = {'auth_epoch': self.state.auth_epoch, 'principal': self.state.principal}
        return exchange

"""Детерминированный scoped поиск по данным разрешённого снимка каталога."""

import base64
import hashlib
import hmac
import json
import re
import secrets
from copy import deepcopy
from ..openapi.parser import canonical


class CatalogDiscovery:
    """Cursor привязан к Run/query/config; рейтинг никогда не выдаёт права."""

    def __init__(self, catalog, allowed, config, anonymous=()):
        self.catalog, self.allowed, self.config = catalog, tuple(allowed), config
        self.anonymous = frozenset(anonymous)
        self._key = secrets.token_bytes(32)

    def ranked(self, query, allowed=None, *, max_characters=200):
        """Ранжировать bounded текст; tool query и полная цель имеют разные caps."""
        if not isinstance(query, str) or len(query) > max_characters:
            raise ValueError("search_query_limit")
        terms = set(re.findall(r"\w+", query.casefold()))
        result = []
        metadata = getattr(self.catalog, "search_metadata", {})
        for name in self.allowed if allowed is None else allowed:
            op = self.catalog.operations[name]
            haystack = (
                name
                + " "
                + op["path"]
                + " "
                + op["method"]
                + " "
                + metadata.get(name, {}).get("summary", "")
            ).casefold()
            score = sum(1 for term in terms if term in haystack)
            if terms and score == 0:
                continue
            result.append((name, score))
        return sorted(result, key=lambda item: (-item[1], item[0]))

    def card(self, name, policy):
        op = self.catalog.operations[name]
        card = {
            "operation_id": name,
            "method": op["method"],
            "path": op["path"],
            "policy": policy,
            "auth_required": bool(op["security"]) and name not in self.anonymous,
        }
        if name in self.anonymous:
            card["request_auth"] = "anonymous_negative"
        summary = self.catalog.search_metadata.get(name, {}).get("summary")
        if summary:
            card["untrusted_summary"] = summary
        return card

    def page(self, run_id, args, allowed, policies, current_version, page_size=8):
        query = args["query"]
        ranked = self.ranked(query, allowed)
        scope = {
            "run": run_id,
            "spec": self.catalog.spec_hash,
            "config": self.config.snapshot["version"],
            "current": current_version,
            "query": query,
        }
        offset = 0
        if args.get("cursor"):
            try:
                payload, signature = args["cursor"].split(".")
                raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
                if not hmac.compare_digest(
                    hmac.new(self._key, raw, hashlib.sha256).hexdigest(), signature
                ):
                    raise ValueError()
                data = json.loads(raw)
                if (
                    data["scope"] != scope
                    or type(data["offset"]) is not int
                    or not 0 < data["offset"] < len(ranked)
                ):
                    raise ValueError()
                offset = data["offset"]
            except (ValueError, KeyError, TypeError, UnicodeError):
                raise ValueError("foreign_or_stale_cursor") from None
        selected = ranked[offset : offset + page_size]
        end = offset + len(selected)
        cursor = None
        if end < len(ranked):
            raw = canonical({"scope": scope, "offset": end})
            cursor = (
                base64.urlsafe_b64encode(raw).decode().rstrip("=")
                + "."
                + hmac.new(self._key, raw, hashlib.sha256).hexdigest()
            )
        return {
            "items": [self.card(name, policies[name]) for name, score in selected],
            "total": len(ranked),
            "more": end < len(ranked),
            "cursor": cursor,
            "omitted_count": len(ranked) - end,
            "source": "RUN_CATALOG",
            # Карточка содержит ID/path, но не декларацию входных параметров.
            # Это подсказка выбора инструмента, не автоматическое inspect/HTTP.
            "next_hint": "Call inspect_operation for the chosen operation_id.",
        }

"""Детерминированный scoped поиск по данным разрешённого снимка каталога."""

import base64
import hashlib
import hmac
import json
import secrets
import unicodedata
from copy import deepcopy
from ..openapi.parser import canonical


def _terms(text):
    """Общие Unicode-разделители и camelCase, без словаря API/языковых правил."""
    words, current = [], ""
    for character in unicodedata.normalize("NFKC", text):
        if not character.isalnum():
            if current: words.append(current.casefold())
            current = ""
        else:
            if current and current[-1].islower() and character.isupper():
                words.append(current.casefold())
                current = ""
            current += character
    if current: words.append(current.casefold())
    return set(words)


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
        terms = _terms(query)
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
            )
            operation_terms = _terms(haystack)
            score = len(terms & operation_terms)
            if query.casefold().strip() == name.casefold():
                score += 20
            if terms and score == 0:
                continue
            result.append((name, score))
        return sorted(result, key=lambda item: (-item[1], item[0]))

    def index(self, allowed, max_groups=16):
        """Показать лексику всего scoped API вместо угадывания английского запроса.

        Группы выводятся из path после общего literal prefix. Семантический
        перевод/выбор делает уже существующая LLM по metadata, не Python aliases.
        Ограниченный индекс не выдаёт новые права и не заменяет полный контракт.
        """
        paths = {name: [p for p in self.catalog.operations[name]["path"].split("/") if p and not (p.startswith("{") and p.endswith("}"))] for name in allowed}
        sequences = list(paths.values())
        shared = 0
        while sequences and all(len(p) > shared + 1 for p in sequences) and len({p[shared] for p in sequences}) == 1:
            shared += 1
        groups = {}
        for name, segments in paths.items():
            group = segments[shared] if len(segments) > shared else self.catalog.operations[name]["path"]
            groups.setdefault(group, []).append(name)
        items = []
        for group, names in sorted(groups.items())[:max_groups]:
            descriptions = [self.catalog.search_metadata.get(n, {}).get("summary", "") for n in names]
            descriptions = list(dict.fromkeys(d for d in descriptions if d))[:2]
            items.append({"query": group, "operation_count": len(names), "untrusted_descriptions": [d[:160] for d in descriptions]})
        return {"groups": items, "total_groups": len(groups), "more": len(groups) > len(items)}

    def card(self, name, policy):
        op = self.catalog.operations[name]
        card = {
            "operation_id": name,
            "method": op["method"],
            "path": op["path"],
            "agent_permission": policy,
            "api_security": deepcopy(op["security"]),
            "request_auth": "anonymous_negative" if name in self.anonymous else "credential_reference" if op["security"] else "none",
        }
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
            "next_hint": (
                "Call inspect_operation for the chosen operation_id."
                if selected else
                "No metadata matched. Use search_operations with API vocabulary from initial context; search covers operation names/paths/summaries, not response values."
            ),
        }

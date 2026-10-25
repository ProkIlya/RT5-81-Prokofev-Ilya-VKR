"""Реестр инструментов, проверка аргументов и выбор разрешённой фазы."""
from copy import deepcopy

from .types import PhaseDecision

class CatalogTools:

    def decision(self, name, args):
        if (
            name in {"execute_operation", "inspect_operation"}
            and args["operation_id"] not in self.allowed
        ):
            raise ValueError("operation_denied")
        if name not in {
            "execute_operation",
            "inspect_operation",
            "finish_run",
            "search_operations",
            "read_response",
        }:
            raise ValueError("unknown_tool")

    def dispatch_name(self, name):
        return name


    def next_occurrence(self, exchanges):
        """Последовательный manifest принят приложением до первого HTTP."""
        done = {e.get('occurrence_id') for e in exchanges if e.get('outcome') == 'received'}
        return next((s for s in self.occurrences if s['id'] not in done), None)

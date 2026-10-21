"""Построение effective payload с точной фазой и собственными evidence IDs."""
from copy import deepcopy
from ..resilience import build_window

def model_payload(self, run, history, registry, model):
    payload = {
        "model": run["model"]["name"],
        "messages": build_window(history),
        "tools": deepcopy(registry),
        "tool_choice": self.workflow.next_tool(run) if self.workflow and hasattr(self.workflow, "next_tool") else "required",
        **run["model"]["request_options"],
        **run["model"]["sampling"],
    }
    if self.workflow and hasattr(self.workflow, 'phase_decision'):
        phase = self.workflow.phase_decision(run, payload['tools'])
        payload['tools'], payload['tool_choice'] = phase.tools, phase.tool_choice
    elif self.workflow and hasattr(self.workflow, "phase_registry"):
        payload["tools"] = self.workflow.phase_registry(run, payload["tools"])
    # UUID нельзя надёжно переписывать малой моделью как свободный
    # текст. Native grammar предлагает только уже наблюдённые ID;
    # session всё равно проверяет scope и полноту evidence.
    if run["exchanges"]:
        for tool in payload["tools"]:
            if tool["function"]["name"] == "finish_run":
                evidence = tool["function"]["parameters"]["properties"]["evidence_ids"]
                # Базовые schemas могут разделять один scalar object
                # с credential_ref: enum относится только к evidence.
                evidence["items"] = {**deepcopy(evidence["items"]), "enum": [e["id"] for e in run["exchanges"]]}
    if hasattr(model, "prepare_payload"):
        payload = model.prepare_payload(payload)
    return payload

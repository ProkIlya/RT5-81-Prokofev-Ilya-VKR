"""Сохранённый reviewer boundary проверяется без генерации и без изменения oracle."""

import sys, json, os, unittest
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from api_agent.agent.catalog import CatalogWorkflow, CatalogLimits
from api_agent.agent.model import LocalModel
from api_agent.agent.resilience import TokenBudget
from api_agent.policy import ProjectConfig
from test_catalog_execution import catalog_variant, status_condition


class BudgetBoundaryTests(unittest.TestCase):
    def fixture(self):
        f = json.loads(
            (ROOT / "tests/fixtures/catalog_budget_boundary.json").read_text(
                encoding="utf-8"
            )
        )
        cat = catalog_variant("beta")
        cfg = ProjectConfig.create(
            "p",
            "http://127.0.0.1:18080",
            cat,
            mode="TEST",
            resettable=True,
            operations={"betacreateOrder": "C", "betareadOrder": "A"},
        )
        wf = CatalogWorkflow(
            cat,
            cfg,
            goal=f["goal"],
            conditions={
                "betacreateOrder": [status_condition(201)],
                "betareadOrder": [status_condition(200)],
            },
        )
        return f, cat, wf

    def projection(self, f, wf):
        history = deepcopy(f["history"])
        for m in history:
            if m["role"] == "tool":
                m["content"] = wf.model_content(
                    wf.model_result(json.loads(m["content"]))
                )
        return history

    def test_projection_preserves_full_native_history_and_all_facts(self):
        f, cat, wf = self.fixture()
        projected = self.projection(f, wf)
        self.assertEqual(len(projected), len(f["history"]))
        for old, new in zip(f["history"], projected):
            if old["role"] != "tool":
                self.assertEqual(new, old)
                continue
            self.assertEqual(new["tool_call_id"], old["tool_call_id"])
            a = json.loads(old["content"])
            b = json.loads(new["content"])
            # Сравниваем факты независимо от явных имён новой проекции.
            b["status"] = b.pop("tool_execution", "ok")
            if "catalog_operation" in (a.get("data") or {}):
                a["data"].pop("sections", None)
                a["data"].pop("omitted_sections", None)
            if (a.get("data") or {}).get("runtime_values") == []:
                a["data"].pop("runtime_values")
            if "http_response" in (b.get("data") or {}):
                data = b["data"]
                http = data.pop("http_response")
                data["status"] = http["status_code"]
                data["outcome"] = http["outcome"]
                if "body" in http:
                    data["response_json"] = http["body"]
                checks = data.pop("machine_checks")
                data["validation"] = {
                    "contract_verdict": checks["contract_verdict"],
                    "intent_verdict": checks["expectation_verdict"],
                    "task_postcondition": checks["evaluation_complete"],
                }
            # Меняется только служебная формулировка подсказки, не actual facts.
            if (a.get("data") or {}).get("checks_complete"):
                a["data"].pop("next_hint", None)
                b["data"].pop("next_hint", None)
            self.assertEqual(b, a)

    @unittest.skipUnless(
        os.environ.get("CATALOG_LIVE_TOKENIZER") == "1",
        "Actual GGUF tokenizer required",
    )
    def test_original_failed_boundary_leaves_headroom_for_finish(self):
        f, cat, wf = self.fixture()
        payload = {
            "model": "MiniCPM5-2B-Q4_K_M",
            "messages": self.projection(f, wf),
            "tools": wf.registry(cat),
            "tool_choice": "required",
            "parallel_tool_calls": False,
            "chat_template_kwargs": {"enable_thinking": False},
            **f["sampling"],
        }
        budget = TokenBudget(
            CatalogLimits(), LocalModel("http://127.0.0.1:18081").count_tokens
        )
        budget.upper = f["used_tokens"]
        count, reserve = budget.reserve(payload)
        self.assertLessEqual(
            f["used_tokens"] + reserve, f["total_limit"] - f["minimum_headroom"]
        )

    def test_runtime_uses_compact_projection_before_next_native_call(self):
        from demo_api.server import DemoServer
        from api_agent.agent import StartAgentRun
        from test_catalog_execution import GenericExecutionTests, GenericModel
        from agent_support import count_test_tokens

        with DemoServer() as server:
            cat, cfg, wf, create, read = GenericExecutionTests().workflow(
                server, "beta"
            )
            delegate = GenericModel("betacreateOrder", "betareadOrder")
            observed = []

            def model(payload):
                if payload["messages"][-1]["role"] == "tool":
                    content = payload["messages"][-1]["content"]
                    observed.append(content)
                    self.assertIn('"tool_execution":"ok"', content)
                    self.assertLess(
                        len(content),
                        len(json.dumps(json.loads(content), ensure_ascii=False)),
                    )
                return delegate(payload)

            r = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start("p", "s", "compact", wf.goal, model)
            self.assertEqual(r["status"], "completed", r["stop_reason"])
            self.assertEqual(len(observed), 2)
            self.assertEqual(len(server.journal), 2)

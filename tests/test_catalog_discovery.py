"""Discovery не меняет права; весь разрешённый каталог достижим пагинацией."""

import sys
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.agent import StartAgentRun
from demo_api.server import DemoServer
from agent_support import reply, count_test_tokens
from test_catalog_execution import status_condition


def hundred_catalog():
    schema = {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "name": {"type": "string"}},
        "required": ["id", "name"],
    }

    def operation(name):
        return {
            "operationId": name,
            "responses": {
                "200": {
                    "description": "ok",
                    "content": {"application/json": {"schema": schema}},
                }
            },
        }

    paths = {
        "/unrelated/" + str(i): {"get": operation("item" + str(i).zfill(3))}
        for i in range(99)
    }
    paths["/pets/1"] = {"get": operation("z_pet_observation")}
    return parse_document(
        {
            "openapi": "3.0.3",
            "info": {"title": "Catalog100", "version": "1"},
            "paths": paths,
        }
    )


class DiscoveryModel:
    def __call__(self, payload):
        obs = [
            json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"
        ]
        if not obs:
            return reply("search_operations", {"query": "pets"})
        if len(obs) == 1:
            return reply(
                "inspect_operation",
                {"operation_id": obs[-1]["data"]["items"][0]["operation_id"]},
            )
        if len(obs) == 2:
            return reply("execute_operation", {"operation_id": "z_pet_observation"})
        return reply(
            "finish_run",
            {
                "summary": "Checked",
                "evidence_ids": obs[-1]["data"]["finish_evidence_ids"],
            },
        )


class DiscoveryTests(unittest.TestCase):
    def workflow(self, target, operations=None):
        cat = hundred_catalog()
        cfg = ProjectConfig.create(
            "p",
            target,
            cat,
            mode="READ_ONLY",
            resettable=True,
            operations=operations or {n: "A" for n in cat.operations},
        )
        wf = CatalogWorkflow(
            cat,
            cfg,
            goal="Найди объект Мурку. Ничего не изменяй.",
            conditions={"z_pet_observation": [status_condition(200)]},
        )
        return cat, cfg, wf

    def test_search_finds_operation_outside_start_cards_and_live_http_checks_it(self):
        with DemoServer() as server:
            cat, cfg, wf = self.workflow(server.target)
            run = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start("p", "s", "m", wf.goal, DiscoveryModel())
            self.assertEqual(run["status"], "completed")
            self.assertNotIn(
                "z_pet_observation", run["selection_manifest"]["selected_ids"]
            )
            self.assertEqual(server.journal, [{"method": "GET", "path": "/pets/1"}])
            self.assertEqual(len(run["run_catalog"]["allowed_ids"]), 100)

    def test_search_cards_offer_inspect_before_execution(self):
        cat, cfg, wf = self.workflow("http://127.0.0.1:18080")
        page = wf.discovery("r", {"query": "pets"})
        self.assertEqual(page["items"][0]["operation_id"], "z_pet_observation")
        self.assertIn("inspect_operation", page["next_hint"])

    def test_scoped_cursor_rejects_other_run_query_and_forgery(self):
        cat, cfg, wf = self.workflow("http://127.0.0.1:18080")
        self.assertTrue(hasattr(wf, "discovery"), "Отсутствует scoped discovery")
        first = wf.discovery("own", {"query": ""})
        self.assertLessEqual(len(first["items"]), 8)
        self.assertTrue(first["more"])
        for run, args in (
            ("other", {"query": "", "cursor": first["cursor"]}),
            ("own", {"query": "pets", "cursor": first["cursor"]}),
            ("own", {"query": "", "cursor": "forged"}),
        ):
            with self.assertRaises(ValueError):
                wf.discovery(run, args)
        page = wf.discovery("own", {"query": "", "cursor": first["cursor"]})
        self.assertNotEqual(
            page["items"][0]["operation_id"], first["items"][0]["operation_id"]
        )

    def test_registry_independent_of_catalog_size_and_budget_trims_cards(self):
        cat, cfg, wf = self.workflow("http://127.0.0.1:18080")
        names = [t["function"]["name"] for t in wf.registry(cat)]
        self.assertEqual(
            set(names),
            {
                "search_operations",
                "inspect_operation",
                "execute_operation",
                "finish_run",
                "read_response",
            },
        )
        self.assertNotIn(
            "enum",
            wf.registry(cat)[0]["function"]["parameters"]["properties"]["operation_id"],
        )
        run = {"run_id": "r"}
        wf.initial_context(run, lambda payload: 1900)
        self.assertEqual(run["selection_manifest"]["selected_ids"], [])
        self.assertGreater(run["selection_manifest"]["omitted_count"], 0)


if __name__ == "__main__":
    unittest.main()

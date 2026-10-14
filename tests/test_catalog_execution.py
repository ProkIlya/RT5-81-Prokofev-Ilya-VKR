"""Общий executor: новый operationId не требует новой ветви Python dispatch."""

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from api_agent.agent import StartAgentRun
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig
from demo_api.server import DemoServer
from agent_support import reply, count_test_tokens


def catalog_variant(prefix="v1"):
    document = json.loads(
        (ROOT / "tests/fixtures/chain_openapi.json").read_text(encoding="utf-8")
    )
    for item in document["paths"].values():
        for operation in item.values():
            operation["operationId"] = prefix + operation["operationId"]
    return parse_document(document)


def status_condition(status):
    return {
        "predicate": "status_in",
        "parameters": {"values": [status]},
        "intent": "positive",
        "preconditions": "verified",
    }


class GenericModel:
    def __init__(self, create, read):
        self.create, self.read = create, read

    def __call__(self, payload):
        obs = [
            json.loads(m["content"]) for m in payload["messages"] if m["role"] == "tool"
        ]
        exchanges = [
            o["data"]
            for o in obs
            if isinstance(o.get("data"), dict) and "exchange_id" in o["data"]
        ]
        if not exchanges:
            return reply(
                "execute_operation",
                {"operation_id": self.create, "body": {"name": "A"}},
            )
        if len(exchanges) == 1:
            return reply(
                "execute_operation",
                {
                    "operation_id": self.read,
                    "bindings": {"order_id": exchanges[0]["runtime_values"][0]["ref"]},
                },
            )
        return reply(
            "finish_run",
            {
                "summary": "Observed",
                "evidence_ids": [e["exchange_id"] for e in exchanges],
            },
        )


class GenericExecutionTests(unittest.TestCase):
    def workflow(self, server, prefix="v1", mode="TEST", **options):
        self.assertIsNotNone(
            importlib.util.find_spec("api_agent.agent.catalog"),
            "Общий executor ещё отсутствует",
        )
        from api_agent.agent.catalog import CatalogWorkflow

        catalog = catalog_variant(prefix)
        create, read = prefix + "createOrder", prefix + "readOrder"
        config = ProjectConfig.create(
            "p",
            server.target,
            catalog,
            mode=mode,
            resettable=True,
            operations={create: "C", read: "A"},
        )
        wf = CatalogWorkflow(
            catalog,
            config,
            goal="Создай заявку A и проверь её чтение",
            conditions={create: [status_condition(201)], read: [status_condition(200)]},
            extraction={create: {"/id": {"type": "integer", "secret": False}}},
            binding_rules={
                read: {"order_id": {"source_operation": create, "source_path": "/id"}}
            },
            **options
        )
        return catalog, config, wf, create, read

    def test_two_contracts_execute_without_manual_dispatch(self):
        for prefix in ("alpha", "beta"):
            with self.subTest(prefix=prefix), DemoServer() as server:
                cat, cfg, wf, create, read = self.workflow(server, prefix)
                run = StartAgentRun(
                    server.target, cat, workflow=wf, token_counter=count_test_tokens
                ).start(
                    "p",
                    "s",
                    "m",
                    "Создай заявку A и проверь её чтение",
                    GenericModel(create, read),
                )
                self.assertEqual(run["status"], "completed")
                self.assertEqual(len(server.journal), 2)
                self.assertEqual(
                    run["run_catalog"]["allowed_ids"], sorted([create, read])
                )
                self.assertEqual(
                    server.journal[-1]["path"],
                    "/orders/" + str(server.journal[0]["created_id"]),
                )

    def test_foreign_headers_unknown_parameters_and_fake_refs_never_connect(self):
        attacks = (
            {"headers": {"Authorization": "Bearer fake"}},
            {"parameters": {"unexpected": 2}},
            {"bindings": {"order_id": "foreign"}},
        )
        for attack in attacks:
            with self.subTest(attack=attack), DemoServer() as server:
                cat, cfg, wf, create, read = self.workflow(server)
                args = {"operation_id": read, **attack}
                run = StartAgentRun(
                    server.target, cat, workflow=wf, token_counter=count_test_tokens
                ).start(
                    "p",
                    "s",
                    "m",
                    "Создай заявку A и проверь её чтение",
                    lambda p: reply("execute_operation", args),
                )
                self.assertNotEqual(run["status"], "completed")
                self.assertEqual(server.connections, 0)

    def test_unsupported_goal_never_generates_or_connects(self):
        with DemoServer() as server:
            cat, cfg, wf, create, read = self.workflow(server)
            run = StartAgentRun(
                server.target, cat, workflow=wf, token_counter=count_test_tokens
            ).start(
                "p",
                "s",
                "m",
                "Проверь безопасность всех серверов",
                lambda p: self.fail("unexpected model call"),
            )
            self.assertEqual(run["stop_reason"], "clarification_required")
            self.assertEqual(server.connections, 0)

    def test_empty_rights_and_stale_hash_rejected_before_network(self):
        with DemoServer() as server:
            cat, cfg, wf, create, read = self.workflow(server)
            from api_agent.agent.catalog import CatalogWorkflow

            empty = ProjectConfig.create(
                "p", server.target, cat, mode="TEST", resettable=True, operations={}
            )
            with self.assertRaises(ValueError):
                CatalogWorkflow(cat, empty, goal="x", conditions={})
            from tempfile import TemporaryDirectory

            with TemporaryDirectory() as directory:
                path = Path(directory) / "project.json"
                cfg.save(path)
                data = json.loads(path.read_text(encoding="utf-8"))
                data["operations"][create]["operation_hash"] = "0" * 64
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ValueError):
                    ProjectConfig.load(path, cat)
            self.assertEqual(server.connections, 0)


if __name__ == "__main__":
    unittest.main()

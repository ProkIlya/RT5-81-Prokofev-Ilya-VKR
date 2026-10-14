"""Tokenizer является внешней границей: raw secrets не попадают и туда."""

import sys, json, unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]
from api_agent.openapi import parse_document
from api_agent.policy import ProjectConfig
from api_agent.agent.catalog import CatalogWorkflow
from api_agent.agent import StartAgentRun
from demo_api.server import DemoServer
from agent_support import ObserverModel
from test_catalog_execution import status_condition


class MetadataPrivacyTests(unittest.TestCase):
    def test_configured_secret_in_metadata_is_redacted_before_tokenizer(self):
        canary = "S07-private-metadata-canary"
        doc = json.loads(
            (ROOT / "tests/fixtures/demo_openapi.json").read_text(encoding="utf-8")
        )
        doc["paths"]["/pets/1"]["get"]["summary"] = "Read pet " + canary
        cat = parse_document(doc)
        with DemoServer() as server:
            cfg = ProjectConfig.create(
                "p",
                server.target,
                cat,
                mode="READ_ONLY",
                resettable=True,
                operations={"getPet": "A"},
            )
            wf = CatalogWorkflow(
                cat,
                cfg,
                goal="Check pet",
                conditions={"getPet": [status_condition(200)]},
            )
            seen = []

            def counter(payload):
                seen.append(payload)
                self.assertNotIn(canary, json.dumps(payload))
                return 100

            run = StartAgentRun(
                server.target,
                cat,
                workflow=wf,
                secrets=(canary,),
                token_counter=counter,
            ).start("p", "s", "m", "Check pet", ObserverModel())
            self.assertEqual(run["status"], "completed")
            self.assertGreater(len(seen), 0)
            self.assertNotIn(canary, json.dumps(run))


if __name__ == "__main__":
    unittest.main()

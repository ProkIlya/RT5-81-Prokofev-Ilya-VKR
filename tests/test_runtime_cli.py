"""CLI S06 передаёт доверенный профиль application; пароль не в argv/prompt."""

import io, json, sys, unittest
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from demo_api.server import DemoServer
from api_agent.agent.__main__ import main
from test_runtime_chain import CreateReadModel
from agent_support import count_test_tokens
import tempfile


class RuntimeCliTests(unittest.TestCase):
    def test_explicit_workflow_is_executable(self):
        with DemoServer() as server, tempfile.TemporaryDirectory() as directory:
            out = io.StringIO()
            try:
                with redirect_stdout(out), redirect_stderr(io.StringIO()):
                    code = main(
                        [
                            "Create and read order",
                            "--target",
                            server.target,
                            "--spec",
                            str(ROOT / "tests/fixtures/chain_openapi.json"),
                            "--workflow",
                            "create-read",
                            "--evidence-dir",
                            directory,
                        ],
                        model=CreateReadModel(),
                        token_counter=count_test_tokens,
                    )
            except SystemExit as error:
                self.fail("CLI не поддерживает S06 workflow: " + str(error.code))
            self.assertEqual(code, 0)
            result = json.loads(out.getvalue())
            self.assertEqual(result["chain_verdict"], "pass")
            self.assertEqual(result["requests"], 2)

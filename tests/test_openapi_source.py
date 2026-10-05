"""Живые socket-пробы доказывают сетевую границу и полноту входного документа."""

import importlib
from pathlib import Path
import sys
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from openapi_support import SpecServer
from test_openapi_profile import document


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.api = importlib.import_module("api_agent.openapi")

    def test_allowed_source_and_external_ref_never_connects(self):
        with SpecServer(document()) as forbidden, SpecServer(document()) as allowed:
            catalog = self.api.load_source(
                allowed.target + "/openapi.json", target=allowed.target
            )
            self.assertIn("getPet", catalog.operations)
            spec = document()
            spec["paths"]["/pets/{id}"]["get"]["parameters"][0]["schema"] = {
                "$ref": forbidden.target + "/ref"
            }
            allowed.document = spec
            with self.assertRaises(self.api.OpenApiError) as caught:
                self.api.load_source(
                    allowed.target + "/openapi.json", target=allowed.target
                )
            self.assertEqual(caught.exception.code, "external_ref")
            self.assertEqual(forbidden.connections, 0)
            self.assertEqual(allowed.journal, ["/openapi.json", "/openapi.json"])

    def test_other_origin_ambiguous_url_and_redirect_never_connect(self):
        with SpecServer(document()) as forbidden, SpecServer(
            document(), "redirect", forbidden.target + "/stolen"
        ) as allowed:
            for url in (
                forbidden.target + "/spec",
                allowed.target + "/spec?x=1",
                allowed.target + "/spec#x",
                allowed.target.replace("127.0.0.1", "localhost") + "/spec",
                allowed.target + "/%2e/spec",
            ):
                with self.assertRaises(self.api.OpenApiError) as caught:
                    self.api.load_source(url, target=allowed.target)
                self.assertEqual(caught.exception.code, "source_denied")
            self.assertEqual(allowed.connections, 0)
            with self.assertRaises(self.api.OpenApiError) as caught:
                self.api.load_source(allowed.target + "/spec", target=allowed.target)
            self.assertEqual(caught.exception.code, "source_denied")
            self.assertEqual(forbidden.connections, 0)

    def test_truncated_oversize_and_content_type(self):
        for mode, code in (
            ("truncated", "source_error"),
            ("oversize", "limits"),
            ("wrong_type", "source_error"),
        ):
            with self.subTest(mode=mode), SpecServer(document(), mode) as server:
                with self.assertRaises(self.api.OpenApiError) as caught:
                    self.api.load_source(server.target + "/spec", target=server.target)
                self.assertEqual(caught.exception.code, code)

    def test_deadline_bounds_headers_and_body(self):
        for mode in ("slow_headers", "slow_body"):
            with self.subTest(mode=mode), SpecServer(document(), mode) as server:
                started = time.monotonic()
                with self.assertRaises(self.api.OpenApiError) as caught:
                    self.api.load_source(
                        server.target + "/spec", target=server.target, timeout=0.06
                    )
                self.assertEqual(caught.exception.code, "source_error")
                self.assertLess(time.monotonic() - started, 0.2)

    def test_unc_source_denied_before_filesystem_access(self):
        from unittest.mock import patch

        # Любое чтение UNC само является выходом в сеть, поэтому запрет до open.
        with patch(
            "pathlib.Path.open", side_effect=AssertionError("network file opened")
        ):
            for source in (
                r"\\127.0.0.1\share\spec.json",
                "//127.0.0.1/share/spec.json",
            ):
                with self.assertRaises(self.api.OpenApiError) as caught:
                    self.api.load_source(source)
                self.assertEqual(caught.exception.code, "source_denied")

    def test_error_branches_close_actual_http_response(self):
        import http.client
        from unittest.mock import patch

        responses = []
        original = http.client.HTTPConnection

        class ObservedConnection(original):
            def getresponse(self):
                response = super().getresponse()
                responses.append(response)
                return response

        # Наблюдаем настоящий HTTPResponse: transport и сервер не заменены fake.
        with patch(
            "api_agent.openapi.source.http.client.HTTPConnection", ObservedConnection
        ):
            for mode in ("redirect", "oversize", "wrong_type", "slow_body"):
                with self.subTest(mode=mode), SpecServer(document(), mode) as server:
                    with self.assertRaises(self.api.OpenApiError):
                        self.api.load_source(
                            server.target + "/spec", target=server.target, timeout=0.06
                        )
                    self.assertTrue(
                        responses[-1].isclosed(),
                        "HTTP response remains open after error",
                    )

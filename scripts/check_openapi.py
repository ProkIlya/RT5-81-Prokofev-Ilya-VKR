"""Машинный good/bad gate профиля: oracle — golden snapshot и журнал TCP стенда."""

import copy
import hashlib
import http.client
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from api_agent.openapi import OpenApiError, load_source, parse_document
from api_agent.openapi.parser import PROFILE_VERSION, PARSER_VERSION
from openapi_support import SpecServer


def save(path, value):
    """Только очищенные декларации и безопасные коды — исходные bytes не сохраняются."""
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def digest(value):
    """Независимое вычисление oracle хеша; не использует canonical из parser."""
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def verify_good(catalog, fixture):
    """Пропуск операции или потеря поля — ошибка gate, даже при успешном parse."""
    if (
        sorted(catalog.operations) != fixture["expected_ids"]
        or catalog.snapshot != fixture["expected_snapshot"]
    ):
        raise RuntimeError("catalog differs from golden snapshot")
    if catalog.spec_hash != digest(fixture["expected_snapshot"]):
        raise RuntimeError("invalid spec hash")
    expected_hashes = {
        name: digest(op)
        for name, op in fixture["expected_snapshot"]["operations"].items()
    }
    if catalog.operation_hashes != expected_hashes:
        raise RuntimeError("invalid operation hash")
    if "CANARY_SECRET" in json.dumps(catalog.snapshot):
        raise RuntimeError("secret retained")


def require_rejected(action, code):
    """Fail-closed gate: тихое принятие или не тот diagnostic — execution error, не bad=2."""
    try:
        action()
    except OpenApiError as error:
        if error.code != code:
            raise RuntimeError("unexpected diagnostic") from error
        return {"code": error.code, "pointer": error.pointer}
    raise RuntimeError("bad input accepted")


def observer_control(server):
    """Положительный контроль доказывает способность наблюдателя заметить TCP/HTTP."""
    port = server.server.server_port
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        connection.request("GET", "/positive-control")
        connection.getresponse().read()
    finally:
        connection.close()
    if server.connections != 1 or server.journal != ["/positive-control"]:
        raise RuntimeError("observer failed")
    server.connections = 0
    server.journal.clear()


def run_good(spec, fixture, allowed):
    """Проверить snapshot, воспроизводимость, basis и HTTP источник того же target."""
    catalog = parse_document(spec)
    verify_good(catalog, fixture)
    from_file = load_source(ROOT / fixture["source"])
    verify_good(from_file, fixture)
    from_url = load_source(allowed.target + "/openapi.json", target=allowed.target)
    verify_good(from_url, fixture)
    shuffled = json.loads(json.dumps(spec, sort_keys=True))
    shuffled["components"]["schemas"]["Pet"]["required"].reverse()
    shuffled["info"]["description"] = "changed documentation"
    if parse_document(shuffled).spec_hash != catalog.spec_hash:
        raise RuntimeError("unstable hash")
    changed = copy.deepcopy(spec)
    changed["components"]["schemas"]["Pet"]["properties"]["id"]["type"] = "string"
    newer = parse_document(changed)
    if newer.spec_hash == catalog.spec_hash or any(
        newer.operation_hashes[k] == catalog.operation_hashes[k]
        for k in ("getPet", "createPet")
    ):
        raise RuntimeError("schema change not reflected")
    if newer.operation_hashes["GET /health"] != catalog.operation_hashes["GET /health"]:
        raise RuntimeError("unrelated operation changed")
    basis = catalog.resolve_basis(
        "getPet",
        "/responses/200/content/application~1json/schema/properties/id/type",
        catalog.operation_hashes["getPet"],
    )
    if basis != "integer":
        raise RuntimeError("basis not retained")
    return {
        "catalog": catalog.snapshot,
        "spec_hash": catalog.spec_hash,
        "operation_hashes": catalog.operation_hashes,
        "schema_change": {
            "spec_hash": newer.spec_hash,
            "operation_hashes": newer.operation_hashes,
        },
        "basis": basis,
    }


def run_bad(spec, fixture, catalog, allowed, forbidden):
    """Негативные fixtures плюс живые probes; unsupported никогда не даёт каталог."""
    rejected = []
    for case in fixture["cases"]:
        changed = copy.deepcopy(spec)
        parent = changed
        for key in case["pointer"][:-1]:
            parent = parent[key]
        value = copy.deepcopy(case["value"])
        if case["name"] == "external_ref":
            value = {"$ref": forbidden.target + "/forbidden.json"}
        parent[case["pointer"][-1]] = value
        result = require_rejected(lambda: parse_document(changed), case["code"])
        rejected.append({"case": case["name"], **result})
        if case["name"] == "external_ref":
            allowed.document = changed
            rejected.append(
                {
                    "case": "external_ref_from_http",
                    **require_rejected(
                        lambda: load_source(
                            allowed.target + "/openapi.json", target=allowed.target
                        ),
                        "external_ref",
                    ),
                }
            )
            allowed.document = spec
    for url in (
        forbidden.target + "/spec",
        allowed.target + "/spec?x=1",
        allowed.target + "/spec#x",
        allowed.target.replace("127.0.0.1", "localhost") + "/spec",
    ):
        rejected.append(
            {
                "case": "source_origin",
                **require_rejected(
                    lambda: load_source(url, target=allowed.target), "source_denied"
                ),
            }
        )
    allowed.mode = "redirect"
    allowed.location = forbidden.target + "/redirected"
    rejected.append(
        {
            "case": "redirect",
            **require_rejected(
                lambda: load_source(allowed.target + "/spec", target=allowed.target),
                "source_denied",
            ),
        }
    )
    for mode, code in (
        ("truncated", "source_error"),
        ("oversize", "limits"),
        ("wrong_type", "source_error"),
        ("slow_headers", "source_error"),
        ("slow_body", "source_error"),
    ):
        allowed.mode = mode
        started = time.monotonic()
        result = require_rejected(
            lambda: load_source(
                allowed.target + "/spec",
                target=allowed.target,
                timeout=0.06 if mode.startswith("slow") else 5,
            ),
            code,
        )
        elapsed = time.monotonic() - started
        if mode.startswith("slow") and elapsed > 0.2:
            raise RuntimeError("deadline violated")
        rejected.append({"case": mode, "elapsed_seconds": elapsed, **result})
    for pointer, h in (
        ("/responses/404", catalog.operation_hashes["getPet"]),
        ("/responses/200", "wrong"),
    ):
        rejected.append(
            {
                "case": "lost_basis",
                **require_rejected(
                    lambda: catalog.resolve_basis("getPet", pointer, h), "invalid_basis"
                ),
            }
        )
    return {"rejected_cases": rejected}


def main(mode):
    """Code 2 означает доказанное отклонение bad, code 1 — поломку самой проверки."""
    directory = Path(os.environ.get("S03_EVIDENCE_DIR", ROOT / "evidence" / "S03"))
    directory.mkdir(parents=True, exist_ok=True)
    result = {
        "mode": mode,
        "profile_version": PROFILE_VERSION,
        "parser_version": PARSER_VERSION,
    }
    try:
        good = json.loads(
            (ROOT / "tests/gates/s03_good.json").read_text(encoding="utf-8")
        )
        spec = json.loads((ROOT / good["source"]).read_text(encoding="utf-8"))
        with SpecServer(spec) as forbidden, SpecServer(spec) as allowed:
            observer_control(forbidden)
            if mode == "good":
                result.update(run_good(spec, good, allowed))
            else:
                bad = json.loads(
                    (ROOT / "tests/gates/s03_bad.json").read_text(encoding="utf-8")
                )
                result.update(
                    run_bad(spec, bad, parse_document(spec), allowed, forbidden)
                )
            result.update(
                observer_positive_control="passed",
                forbidden_connections=forbidden.connections,
                forbidden_journal=forbidden.journal,
                source_connections=allowed.connections,
                source_journal=allowed.journal,
            )
            if forbidden.connections or forbidden.journal:
                raise RuntimeError("forbidden network access")
        result["source_sha256"] = {
            str(p.relative_to(ROOT))
            .replace("\\", "/"): hashlib.sha256(p.read_bytes())
            .hexdigest()
            for p in [
                *sorted((ROOT / "src/api_agent/openapi").glob("*.py")),
                ROOT / "src/api_agent/socket_deadline.py",
                ROOT / "scripts/check_openapi.py",
                ROOT / "scripts/check_stage.py",
                ROOT / "tests/openapi_support.py",
                ROOT / good["source"],
                ROOT / "tests/gates/s03_good.json",
                ROOT / "tests/gates/s03_bad.json",
            ]
        }
        result.update(
            result="accepted" if mode == "good" else "rejected",
            exit_code=0 if mode == "good" else 2,
        )
    except Exception as error:
        # Unexpected error — не bad=2. Исключения не печатают исходный contract/URL.
        result.update(result="failed", exit_code=1, error_type=type(error).__name__)
    save(directory / (mode + ".json"), result)
    if result["exit_code"] == 2:
        for diagnostic in result["rejected_cases"]:
            print(json.dumps(diagnostic, ensure_ascii=False), file=sys.stderr)
    print(
        "S03_" + mode.upper() + "_" + result["result"].upper(),
        file=sys.stderr if result["exit_code"] == 1 else sys.stdout,
    )
    return result["exit_code"]

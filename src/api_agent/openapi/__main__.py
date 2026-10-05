"""CLI просмотра каталога S03: показывает декларации, не запускает Agent Run."""

import argparse
import json
import sys
from . import OpenApiError, load_source


def main():
    """Принять source и доверенный target отдельно, вывести безопасный snapshot."""
    parser = argparse.ArgumentParser(description="Inspect restricted REST/JSON OpenAPI")
    parser.add_argument("source", help="UTF-8 JSON file or allowed loopback URL")
    parser.add_argument(
        "--target", help="Explicit allowed http://127.0.0.1:port for URL source"
    )
    args = parser.parse_args()
    try:
        catalog = load_source(args.source, target=args.target)
    except OpenApiError as error:
        print(
            json.dumps({"code": error.code, "pointer": error.pointer}), file=sys.stderr
        )
        return 2
    print(
        json.dumps(
            {
                "snapshot": catalog.snapshot,
                "spec_hash": catalog.spec_hash,
                "operation_hashes": catalog.operation_hashes,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

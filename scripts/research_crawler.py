from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.retrieval.cninfo import CninfoSearchProvider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Search official CNInfo announcements")
    parser.add_argument("symbol", help="A-share symbol, e.g. 600519.SH")
    parser.add_argument("query", help="announcement keywords")
    parser.add_argument("--max-results", type=int, default=5, choices=range(1, 21))
    args = parser.parse_args(argv)
    results = CninfoSearchProvider().search(
        query=args.query,
        symbol=args.symbol,
        max_results=args.max_results,
        timeout=30,
    )
    print(json.dumps(results.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Quick manual test for `scholarly` with the FreeProxies helper."""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Dict


def parse_args() -> argparse.Namespace:
    """Parse CLI flags for the manual scholarly tester."""
    parser = argparse.ArgumentParser(
        description="Run a simple Google Scholar query via scholarly using FreeProxies.",
    )
    parser.add_argument(
        "query",
        help="Search phrase to send to Google Scholar.",
    )
    parser.add_argument(
        "--max-results",
        type=int,
        default=3,
        help="Number of entries to fetch before stopping.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser.parse_args()


def configure_logging(verbose: bool) -> None:
    """Configure root logging for the script."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s | %(message)s")


def main() -> int:
    """Entry point for exercising scholarly proxy behavior."""
    args = parse_args()
    configure_logging(args.verbose)

    try:
        from scholarly import ProxyGenerator, scholarly  # type: ignore
    except Exception as exc:  # pragma: no cover - import only used manually
        logging.error("Unable to import scholarly: %s", exc)
        return 1

    pg = ProxyGenerator()
    logging.info("Requesting free proxies from scholarly...")
    try:
        success = pg.FreeProxies()
    except Exception as exc:
        logging.error("FreeProxies call failed: %s", exc)
        return 1

    if not success:
        logging.error("No proxies returned; scholarly will not use a proxy.")
        return 2

    scholarly.use_proxy(pg)
    logging.info("Proxy configured. Running query: %s", args.query)

    try:
        search_iter = scholarly.search_pubs(args.query)
    except Exception as exc:
        logging.error("scholarly.search_pubs failed: %s", exc)
        return 1

    results = 0
    for results, record in enumerate(search_iter, start=1):
        bib: Dict[str, Any] = record.get("bib", {}) if isinstance(record, dict) else {}
        title = bib.get("title") or "<no title>"
        authors = ", ".join(bib.get("author") or []) if isinstance(bib.get("author"), list) else ""
        year = bib.get("pub_year") or bib.get("year") or "?"
        print(f"{results}. {title} ({year}) {authors}")
        try:
            filled = scholarly.fill(record)  # type: ignore[arg-type]
        except Exception as exc:
            logging.warning("Unable to fill record for '%s': %s", title, exc)
            filled = record
        bib_data = filled.get("bib") if isinstance(filled, dict) else {}
        abstract = (bib_data or {}).get("abstract") or bib.get("abstract")
        if abstract:
            print(f"    Abstract: {abstract}")
        else:
            print("    Abstract: <missing>")
        if results >= args.max_results:
            break
    else:
        results = 0

    logging.info("Done. Retrieved %d result(s).", results)
    return 0


if __name__ == "__main__":
    sys.exit(main())

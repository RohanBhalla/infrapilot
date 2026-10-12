"""Command line entry point: `infrapilot-data <command>` or `python -m infrapilot_data <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import pipeline, registry


def _select(args) -> list:
    specs = registry.load_sources()
    ids = args.source.split(",") if getattr(args, "source", None) else None
    return registry.select(specs, ids=ids, category=getattr(args, "category", None),
                           strategy=getattr(args, "strategy", None))


def cmd_sync_sources(args) -> None:
    path = registry.sync_sheet()
    print(f"Wrote {path}")


def cmd_list(args) -> None:
    specs = registry.load_sources()
    for s in specs:
        flag = f"SKIP: {s.skip_reason}" if not s.enabled else s.strategy
        print(f"{s.id:>3}  {s.category[:28]:<28} {s.name[:48]:<48} {flag}")


def cmd_fetch(args) -> None:
    specs = _select(args)
    if not specs:
        sys.exit("No sources matched.")
    pipeline.run_fetch(specs, max_pages=args.max_pages, workers=args.workers,
                       use_cache=not args.no_cache)


def cmd_process(args) -> None:
    specs = registry.load_sources()
    stats = pipeline.run_process(specs)
    pipeline.save_process_stats(stats)
    report = pipeline.build_report(specs, stats)
    print(json.dumps({"totals": report["totals"], "embedding_estimate": report["embedding_estimate"]}, indent=2))


def cmd_report(args) -> None:
    specs = registry.load_sources()
    report = pipeline.build_report(specs)
    print(json.dumps({"totals": report["totals"], "embedding_estimate": report["embedding_estimate"]}, indent=2))
    print(f"Full report: {pipeline.P.reports / 'report.md'}")


def cmd_run(args) -> None:
    cmd_fetch(args)
    cmd_process(args)


def cmd_embed(args) -> None:
    from . import embed

    if not args.run:
        print(json.dumps(embed.estimate(embed.load_chunks()), indent=2))
        print("Dry run only. Add --run to call Bedrock (needs AWS credentials + model access).")
        return
    print(json.dumps(embed.run_embed(limit=args.limit, workers=args.workers), indent=2))


def cmd_search(args) -> None:
    from . import embed

    for hit in embed.search(args.query, k=args.k, category=args.category):
        print(json.dumps(hit, indent=2))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="infrapilot-data", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("sync-sources", help="download the Google Sheet into sources/sources.csv")
    sub.add_parser("list", help="list sources and their strategies")

    def add_filters(p):
        p.add_argument("--source", help="comma-separated source ids, e.g. 1,2,37")
        p.add_argument("--category", help="substring match on category")
        p.add_argument("--strategy", help="only sources using this strategy")
        p.add_argument("--max-pages", type=int, help="cap pages per source (for quick tests)")
        p.add_argument("--workers", type=int, default=6)
        p.add_argument("--no-cache", action="store_true", help="ignore ETag cache")

    add_filters(sub.add_parser("fetch", help="download sources into data/raw"))
    sub.add_parser("process", help="normalize, filter, dedupe, chunk, export, report")
    sub.add_parser("report", help="rebuild data/reports/report.md")
    add_filters(sub.add_parser("run", help="fetch + process"))

    p = sub.add_parser("embed", help="estimate cost, or --run to embed into LanceDB")
    p.add_argument("--run", action="store_true")
    p.add_argument("--limit", type=int)
    p.add_argument("--workers", type=int, default=4)

    p = sub.add_parser("search", help="query the local LanceDB index")
    p.add_argument("query")
    p.add_argument("-k", type=int, default=5)
    p.add_argument("--category")

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    {
        "sync-sources": cmd_sync_sources, "list": cmd_list, "fetch": cmd_fetch,
        "process": cmd_process, "report": cmd_report, "run": cmd_run, "embed": cmd_embed,
        "search": cmd_search,
    }[args.command](args)


if __name__ == "__main__":
    main()

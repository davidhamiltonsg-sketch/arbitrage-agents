#!/usr/bin/env python3
"""Command-line entry point for the arbitrage agents.

    python3 cli.py domain-flipper --dry-run
    python3 cli.py saas-scout --dry-run
    python3 cli.py domain-flipper --date 2026-10-05 --top 5
    python3 cli.py saas-scout --listings exports/acquire.json --no-deliver
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from agents.common.markdown import blocks_to_markdown  # noqa: E402
from agents.domain_flipper import pipeline as domain_pipeline  # noqa: E402
from agents.saas_scout import pipeline as saas_pipeline  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arbitrage-agents", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="agent", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true", help="use fixtures, synthetic metrics and the heuristic scorer; no network calls")
    common.add_argument("--top", type=int, help="override the shortlist size")
    common.add_argument("--no-deliver", action="store_true", help="print the Slack payload instead of posting it")
    common.add_argument("--quiet", action="store_true", help="suppress progress logging")
    common.add_argument("--markdown-out", metavar="PATH", help="also write the digest as GitHub-flavoured Markdown to PATH")

    flipper = sub.add_parser("domain-flipper", parents=[common], help="Agent 1: dropped-domain shortlist")
    flipper.add_argument("--date", help="drop date to fetch (YYYY-MM-DD); defaults to yesterday UTC")
    flipper.add_argument("--fixture", help="path to a dropped-domains JSON/CSV file instead of calling WhoisFreaks")

    scout = sub.add_parser("saas-scout", parents=[common], help="Agent 2: neglected SaaS shortlist")
    scout.add_argument("--listings", help="path to a normalised listings JSON file instead of the configured sources")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log = (lambda *a, **k: None) if args.quiet else print

    if args.agent == "domain-flipper":
        conf = domain_pipeline.DomainFlipperConfig.from_env()
        result = domain_pipeline.run(conf, dry_run=args.dry_run, drop_date=args.date, top=args.top, deliver=not args.no_deliver, fixture_path=args.fixture, log=log)
    else:
        conf = saas_pipeline.SaasScoutConfig.from_env()
        result = saas_pipeline.run(conf, dry_run=args.dry_run, top=args.top, deliver=not args.no_deliver, listings_path=args.listings, log=log)

    if args.markdown_out:
        Path(args.markdown_out).write_text(blocks_to_markdown(result.digest_text, result.digest_blocks), encoding="utf-8")
        log(f"[deliver] markdown digest written to {args.markdown_out}")
    log(f"[done] run {result.run_id}: " + ", ".join(f"{k}={v}" for k, v in result.funnel.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

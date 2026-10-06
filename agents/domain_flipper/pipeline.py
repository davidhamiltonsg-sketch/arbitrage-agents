"""Orchestration for Agent 1. ``run()`` executes all eight phases once."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from ..common import config as cfg
from ..common.audit import AuditLog
from ..common.cache import Cache
from ..common.llm import LLMClient
from ..common.ranking import rank
from ..common.slack import Sender, SlackClient, StdoutSender
from . import digest, enrich, filters, scoring, sources

FIXTURE_PATH = cfg.PROJECT_ROOT / "fixtures" / "dropped_domains.sample.json"


@dataclass
class DomainFlipperConfig:
    whoisfreaks_api_key: str | None = None
    tlds: tuple[str, ...] = ("com", "ai")
    filters: filters.FilterConfig = field(default_factory=filters.FilterConfig)
    dataforseo_auth: str | None = None
    dr_divisor: float = 10.0
    gate: enrich.AuthorityGate = field(default_factory=enrich.AuthorityGate)
    max_enrich: int = 400
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.2
    top_n: int = 5
    slack_webhook_url: str | None = None
    slack_bot_token: str | None = None
    slack_channel: str | None = "#domain-arbitrage-radar"
    cache_path: str = str(cfg.PROJECT_ROOT / ".cache" / "agents.sqlite")
    audit_path: str | None = str(cfg.PROJECT_ROOT / "logs" / "domain_flipper.jsonl")
    sheets_webhook_url: str | None = None

    @classmethod
    def from_env(cls) -> "DomainFlipperConfig":
        cfg.load_dotenv()
        log_dir = cfg.env("AUDIT_LOG_DIR", str(cfg.PROJECT_ROOT / "logs"))
        dataforseo_auth = None
        if cfg.env("DATAFORSEO_AUTH") or (cfg.env("DATAFORSEO_LOGIN") and cfg.env("DATAFORSEO_PASSWORD")):
            dataforseo_auth = enrich.basic_auth_header(cfg.env("DATAFORSEO_LOGIN"), cfg.env("DATAFORSEO_PASSWORD"), cfg.env("DATAFORSEO_AUTH"))
        return cls(
            whoisfreaks_api_key=cfg.env("WHOISFREAKS_API_KEY"),
            tlds=cfg.env_list("DOMAIN_TLDS", ("com", "ai")),
            filters=filters.FilterConfig(
                max_sld_length=cfg.env_int("DOMAIN_MAX_LENGTH", 20),
                allowed_tlds=cfg.env_list("DOMAIN_TLDS", ("com", "ai")),
                trademark_terms=cfg.env_list("DOMAIN_TRADEMARK_TERMS", filters.DEFAULT_TRADEMARK_TERMS),
                extra_blocklist=cfg.env_list("DOMAIN_BLOCKLIST", ()),
            ),
            dataforseo_auth=dataforseo_auth,
            dr_divisor=cfg.env_float("DR_SCALE_DIVISOR", 10.0),
            gate=enrich.AuthorityGate(min_dr=cfg.env_float("DOMAIN_MIN_DR", 10), min_referring_domains=cfg.env_int("DOMAIN_MIN_REFERRING_DOMAINS", 5)),
            max_enrich=cfg.env_int("DOMAIN_MAX_ENRICH", 400),
            openai_api_key=cfg.env("OPENAI_API_KEY"),
            openai_model=cfg.env("OPENAI_MODEL_DOMAIN", cfg.env("OPENAI_MODEL", "gpt-4o-mini")) or "gpt-4o-mini",
            openai_base_url=cfg.env("OPENAI_BASE_URL", "https://api.openai.com/v1") or "https://api.openai.com/v1",
            temperature=cfg.env_float("OPENAI_TEMPERATURE_DOMAIN", 0.2),
            top_n=cfg.env_int("DOMAIN_TOP_N", 5),
            slack_webhook_url=cfg.env("SLACK_WEBHOOK_URL_DOMAIN", cfg.env("SLACK_WEBHOOK_URL")),
            slack_bot_token=cfg.env("SLACK_BOT_TOKEN"),
            slack_channel=cfg.env("SLACK_CHANNEL_DOMAIN", "#domain-arbitrage-radar"),
            cache_path=cfg.env("AGENTS_CACHE_PATH", str(cfg.PROJECT_ROOT / ".cache" / "agents.sqlite")) or ":memory:",
            audit_path=str(Path(log_dir) / "domain_flipper.jsonl") if log_dir else None,
            sheets_webhook_url=cfg.env("GOOGLE_SHEETS_WEBHOOK_URL"),
        )


@dataclass
class RunResult:
    run_id: str
    shortlist: list[dict[str, Any]]
    funnel: dict[str, int]
    digest_text: str
    digest_blocks: list[dict[str, Any]]


def yesterday_utc() -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()


def run(
    conf: DomainFlipperConfig,
    *,
    dry_run: bool = False,
    drop_date: str | None = None,
    top: int | None = None,
    deliver: bool = True,
    fixture_path: str | Path | None = None,
    sender: Sender | None = None,
    log=print,
) -> RunResult:
    """Execute the pipeline. ``dry_run`` uses fixtures, synthetic metrics and the heuristic scorer with zero network calls."""
    top_n = top or conf.top_n
    drop_date = drop_date or yesterday_utc()
    audit = AuditLog("domain_flipper", None if dry_run else conf.audit_path, None if dry_run else conf.sheets_webhook_url)
    cache = Cache(":memory:" if dry_run else conf.cache_path)
    funnel: dict[str, int] = {}

    # Phase 2: fetch
    if dry_run or fixture_path:
        path = Path(fixture_path) if fixture_path else FIXTURE_PATH
        records = sources.load_fixture(path)
        log(f"[fetch] loaded {len(records)} records from fixture {path.name}")
    else:
        if not conf.whoisfreaks_api_key:
            raise SystemExit("WHOISFREAKS_API_KEY is not set (use --dry-run to exercise the pipeline without keys)")
        records = sources.fetch_dropped_domains(conf.whoisfreaks_api_key, date=drop_date, tlds=conf.tlds)
        log(f"[fetch] WhoisFreaks returned {len(records)} dropped domains for {drop_date}")
    funnel["fetched"] = len(records)
    for record in records:
        audit.record("fetched", record["domain"], drop_date=record.get("drop_date"), registrar=record.get("registrar"))

    # Phase 3: deterministic filter
    kept, rejected = filters.apply_filters(records, conf.filters)
    for record, reason in rejected:
        audit.record("filtered_out", record["domain"], reason=reason)
    funnel["after_filters"] = len(kept)
    log(f"[filter] {len(kept)} candidates remain ({len(rejected)} rejected)")
    if len(kept) > conf.max_enrich:
        log(f"[filter] capping enrichment at {conf.max_enrich} candidates (DOMAIN_MAX_ENRICH)")
        kept = kept[: conf.max_enrich]

    # Phase 4: enrich + hard gate
    enriched: list[dict[str, Any]] = []
    metrics_for = _metrics_provider(conf, cache, dry_run)
    for record in kept:
        try:
            metrics = metrics_for(record)
        except Exception as exc:  # one bad lookup must not sink the run
            audit.record("enrich_error", record["domain"], error=str(exc))
            log(f"[enrich] {record['domain']}: {exc}")
            continue
        candidate = {**record, **metrics}
        candidate.pop("_fixture_metrics", None)
        audit.record("enriched", record["domain"], dr=metrics["dr"], referring_domains=metrics["referring_domains"], total_backlinks=metrics["total_backlinks"])
        if enrich.passes_authority_gate(metrics, conf.gate):
            enriched.append(candidate)
        else:
            audit.record("gated_out", record["domain"], dr=metrics["dr"], referring_domains=metrics["referring_domains"])
    funnel["after_authority_gate"] = len(enriched)
    log(f"[enrich] {len(enriched)} pass the authority gate (DR >= {conf.gate.min_dr:g}, refs >= {conf.gate.min_referring_domains})")

    # Phase 5: AI score
    scorer = _scorer(conf, dry_run, log)
    scored: list[dict[str, Any]] = []
    for candidate in enriched:
        try:
            result = scorer(candidate)
        except Exception as exc:
            audit.record("score_error", candidate["domain"], error=str(exc))
            log(f"[score] {candidate['domain']}: {exc}")
            continue
        item = {**candidate, **result}
        audit.record("scored", candidate["domain"], **result)
        scored.append(item)
    funnel["scored"] = len(scored)

    # Phase 6: rank
    shortlist = rank(scored, keys=(("score", True), ("suggested_price", True)), top=top_n)
    funnel["shortlisted"] = len(shortlist)
    for item in shortlist:
        audit.record("shortlisted", item["domain"], score=item["score"], suggested_price=item["suggested_price"])

    # Phase 7: deliver
    text, blocks = digest.build_digest(shortlist, drop_date, funnel=funnel)
    out = sender or _sender(conf, deliver and not dry_run)
    out.send(text=text, blocks=blocks)
    log(f"[deliver] digest with {len(shortlist)} items sent via {type(out).__name__}")

    # Phase 8 (audit): push to Sheets when configured
    if not dry_run and audit.flush_to_sheets():
        log("[audit] rows pushed to Google Sheets webhook")
    cache.close()
    return RunResult(audit.run_id, shortlist, funnel, text, blocks)


def _metrics_provider(conf: DomainFlipperConfig, cache: Cache, dry_run: bool) -> Callable[[dict[str, Any]], dict[str, Any]]:
    if dry_run or not conf.dataforseo_auth:
        def offline(record: dict[str, Any]) -> dict[str, Any]:
            fixture = record.get("_fixture_metrics")
            if isinstance(fixture, dict):
                rank_value = float(fixture.get("rank", fixture.get("dr", 0) * conf.dr_divisor))
                return {
                    "rank": rank_value,
                    "dr": round(rank_value / conf.dr_divisor, 1),
                    "referring_domains": int(fixture.get("referring_domains", 0)),
                    "total_backlinks": int(fixture.get("total_backlinks", 0)),
                    "spam_score": fixture.get("spam_score"),
                    "first_seen": fixture.get("first_seen"),
                }
            return enrich.synthetic_metrics(record["domain"])
        return offline

    def live(record: dict[str, Any]) -> dict[str, Any]:
        return enrich.fetch_backlink_summary(record["domain"], conf.dataforseo_auth, cache=cache, dr_divisor=conf.dr_divisor)
    return live


def _scorer(conf: DomainFlipperConfig, dry_run: bool, log) -> Callable[[dict[str, Any]], dict[str, Any]]:
    if dry_run or not conf.openai_api_key:
        if not dry_run:
            log("[score] OPENAI_API_KEY not set; using heuristic scorer")
        return scoring.heuristic_score
    client = LLMClient(api_key=conf.openai_api_key, model=conf.openai_model, base_url=conf.openai_base_url, temperature=conf.temperature, max_tokens=300)
    return lambda record: scoring.score_domain(record, client)


def _sender(conf: DomainFlipperConfig, deliver: bool) -> Sender:
    if deliver and (conf.slack_webhook_url or (conf.slack_bot_token and conf.slack_channel)):
        return SlackClient(webhook_url=conf.slack_webhook_url, bot_token=conf.slack_bot_token, channel=conf.slack_channel)
    return StdoutSender()

"""Orchestration for Agent 2. ``run()`` executes all eight phases once."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable

from ..common import config as cfg
from ..common.audit import AuditLog
from ..common.cache import Cache
from ..common.llm import LLMClient
from ..common.ranking import rank
from ..common.slack import Sender, SlackClient, StdoutSender
from . import digest, enrich, filters, scoring, sources

FIXTURE_PATH = cfg.PROJECT_ROOT / "fixtures" / "saas_listings.sample.json"


@dataclass
class SaasScoutConfig:
    source_names: tuple[str, ...] = ("file",)
    listings_file: str | None = None
    chrome_extension_ids: tuple[str, ...] = ()
    acquire_max_listings: int = 150
    filters: filters.SaasFilterConfig = field(default_factory=filters.SaasFilterConfig)
    builtwith_api_key: str | None = None
    similarweb_api_key: str | None = None
    gate: enrich.TrafficGate = field(default_factory=enrich.TrafficGate)
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o"
    openai_base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.15
    min_score: int = 7
    min_rewrite_potential: int = 8
    top_n: int = 3
    slack_webhook_url: str | None = None
    slack_bot_token: str | None = None
    slack_channel: str | None = "#saas-acquisitions"
    cache_path: str = str(cfg.PROJECT_ROOT / ".cache" / "agents.sqlite")
    audit_path: str | None = str(cfg.PROJECT_ROOT / "logs" / "saas_scout.jsonl")
    sheets_webhook_url: str | None = None

    @classmethod
    def from_env(cls) -> "SaasScoutConfig":
        cfg.load_dotenv()
        log_dir = cfg.env("AUDIT_LOG_DIR", str(cfg.PROJECT_ROOT / "logs"))
        return cls(
            source_names=cfg.env_list("SAAS_SOURCES", ("file",)),
            listings_file=cfg.env("SAAS_LISTINGS_FILE"),
            chrome_extension_ids=tuple(e for e in cfg.env_list("CHROME_EXTENSION_IDS", ()) if e),
            acquire_max_listings=cfg.env_int("ACQUIRE_MAX_LISTINGS", 150),
            filters=filters.SaasFilterConfig(
                max_asking_price=cfg.env_float("SAAS_MAX_ASKING_PRICE", 10_000),
                min_inactive_days=cfg.env_int("SAAS_MIN_INACTIVE_DAYS", 540),
                blocked_categories=cfg.env_list("SAAS_BLOCKED_CATEGORIES", filters.DEFAULT_BLOCKED_CATEGORIES),
                min_claimed_users=cfg.env_int("SAAS_MIN_CLAIMED_USERS", 1_000),
                require_price=cfg.env_bool("SAAS_REQUIRE_PRICE", False),
            ),
            builtwith_api_key=cfg.env("BUILTWITH_API_KEY"),
            similarweb_api_key=cfg.env("SIMILARWEB_API_KEY"),
            gate=enrich.TrafficGate(min_monthly_visits=cfg.env_int("SAAS_MIN_MONTHLY_VISITS", 800), min_installs=cfg.env_int("SAAS_MIN_INSTALLS", 1_200)),
            openai_api_key=cfg.env("OPENAI_API_KEY"),
            openai_model=cfg.env("OPENAI_MODEL_SAAS", cfg.env("OPENAI_MODEL", "gpt-4o")) or "gpt-4o",
            openai_base_url=cfg.env("OPENAI_BASE_URL", "https://api.openai.com/v1") or "https://api.openai.com/v1",
            temperature=cfg.env_float("OPENAI_TEMPERATURE_SAAS", 0.15),
            min_score=cfg.env_int("SAAS_MIN_SCORE", 7),
            min_rewrite_potential=cfg.env_int("SAAS_MIN_REWRITE_POTENTIAL", 8),
            top_n=cfg.env_int("SAAS_TOP_N", 3),
            slack_webhook_url=cfg.env("SLACK_WEBHOOK_URL_SAAS", cfg.env("SLACK_WEBHOOK_URL")),
            slack_bot_token=cfg.env("SLACK_BOT_TOKEN"),
            slack_channel=cfg.env("SLACK_CHANNEL_SAAS", "#saas-acquisitions"),
            cache_path=cfg.env("AGENTS_CACHE_PATH", str(cfg.PROJECT_ROOT / ".cache" / "agents.sqlite")) or ":memory:",
            audit_path=str(Path(log_dir) / "saas_scout.jsonl") if log_dir else None,
            sheets_webhook_url=cfg.env("GOOGLE_SHEETS_WEBHOOK_URL"),
        )


@dataclass
class RunResult:
    run_id: str
    shortlist: list[dict[str, Any]]
    funnel: dict[str, int]
    digest_text: str
    digest_blocks: list[dict[str, Any]]


def run(
    conf: SaasScoutConfig,
    *,
    dry_run: bool = False,
    top: int | None = None,
    deliver: bool = True,
    listings_path: str | Path | None = None,
    today: date | None = None,
    sender: Sender | None = None,
    log=print,
) -> RunResult:
    today = today or date.today()
    top_n = top or conf.top_n
    audit = AuditLog("saas_scout", None if dry_run else conf.audit_path, None if dry_run else conf.sheets_webhook_url)
    cache = Cache(":memory:" if dry_run else conf.cache_path)
    funnel: dict[str, int] = {}

    # Phase 2: fetch from every configured source
    listings = _fetch_listings(conf, dry_run, listings_path, log)
    funnel["fetched"] = len(listings)
    for listing in listings:
        audit.record("fetched", listing["asset_id"], name=listing["name"], source=listing["source"], asking_price=listing.get("asking_price"))

    # Phase 3: deterministic filter
    kept, rejected = filters.apply_filters(listings, conf.filters, today)
    for listing, reason in rejected:
        audit.record("filtered_out", listing["asset_id"], reason=reason)
    funnel["after_filters"] = len(kept)
    log(f"[filter] {len(kept)} candidates remain ({len(rejected)} rejected)")

    # Phase 4: enrich + traffic gate
    enrich_fn = _enrichment_provider(conf, cache, dry_run, today)
    enriched: list[dict[str, Any]] = []
    for listing in kept:
        try:
            extra = enrich_fn(listing)
        except Exception as exc:
            audit.record("enrich_error", listing["asset_id"], error=str(exc))
            log(f"[enrich] {listing['name']}: {exc}")
            continue
        candidate = {**listing, **extra}
        candidate.pop("_fixture_enrichment", None)
        audit.record("enriched", listing["asset_id"], tech_stack=extra.get("tech_stack"), monthly_visits=extra.get("monthly_visits"))
        if enrich.passes_traffic_gate(candidate, conf.gate):
            enriched.append(candidate)
        else:
            audit.record("gated_out", listing["asset_id"], monthly_visits=extra.get("monthly_visits"), claimed_users=listing.get("claimed_users"))
    funnel["after_traffic_gate"] = len(enriched)
    log(f"[enrich] {len(enriched)} pass the traffic gate (visits >= {conf.gate.min_monthly_visits} or installs >= {conf.gate.min_installs})")

    # Phase 5: AI score
    scorer = _scorer(conf, dry_run, log)
    scored: list[dict[str, Any]] = []
    for candidate in enriched:
        try:
            result = scorer(candidate)
        except Exception as exc:
            audit.record("score_error", candidate["asset_id"], error=str(exc))
            log(f"[score] {candidate['name']}: {exc}")
            continue
        item = {**candidate, **result}
        audit.record("scored", candidate["asset_id"], **result)
        scored.append(item)
    funnel["scored"] = len(scored)

    # Phase 6: threshold + rank
    eligible = [i for i in scored if i["score"] >= conf.min_score and i["agent_rewrite_potential"] >= conf.min_rewrite_potential]
    shortlist = rank(eligible, keys=(("score", True), ("estimated_flip_value", True)), top=top_n)
    funnel["shortlisted"] = len(shortlist)
    for item in shortlist:
        audit.record("shortlisted", item["asset_id"], score=item["score"], estimated_flip_value=item["estimated_flip_value"])

    # Phase 7: deliver
    text, blocks = digest.build_digest(shortlist, today.isoformat(), funnel=funnel)
    out = sender or _sender(conf, deliver and not dry_run)
    out.send(text=text, blocks=blocks)
    log(f"[deliver] digest with {len(shortlist)} items sent via {type(out).__name__}")

    if not dry_run and audit.flush_to_sheets():
        log("[audit] rows pushed to Google Sheets webhook")
    cache.close()
    return RunResult(audit.run_id, shortlist, funnel, text, blocks)


def _fetch_listings(conf: SaasScoutConfig, dry_run: bool, listings_path: str | Path | None, log) -> list[dict[str, Any]]:
    if dry_run or listings_path:
        path = Path(listings_path) if listings_path else FIXTURE_PATH
        listings = sources.FileSource(path).fetch()
        log(f"[fetch] loaded {len(listings)} listings from {path.name}")
        return listings
    listings: list[dict[str, Any]] = []
    for name in conf.source_names:
        if name == "file":
            if not conf.listings_file:
                log("[fetch] SAAS_LISTINGS_FILE not set; skipping file source")
                continue
            batch = sources.FileSource(conf.listings_file).fetch()
        elif name == "acquire":
            batch = sources.AcquireSitemapSource(max_listings=conf.acquire_max_listings).fetch()
        elif name == "chromewebstore":
            if not conf.chrome_extension_ids:
                log("[fetch] CHROME_EXTENSION_IDS not set; skipping Chrome Web Store source")
                continue
            batch = sources.ChromeWebStoreSource(conf.chrome_extension_ids).fetch()
        else:
            log(f"[fetch] unknown source {name!r}; skipping")
            continue
        log(f"[fetch] {name}: {len(batch)} listings")
        listings.extend(batch)
    return listings


def _enrichment_provider(conf: SaasScoutConfig, cache: Cache, dry_run: bool, today: date) -> Callable[[dict[str, Any]], dict[str, Any]]:
    def offline(listing: dict[str, Any]) -> dict[str, Any]:
        fixture = listing.get("_fixture_enrichment")
        if isinstance(fixture, dict):
            return {"tech_stack": list(fixture.get("tech_stack") or []), "monthly_visits": fixture.get("monthly_visits")}
        return enrich.synthetic_enrichment(listing)

    if dry_run:
        return offline

    def live(listing: dict[str, Any]) -> dict[str, Any]:
        domain = listing.get("domain")
        out: dict[str, Any] = {"tech_stack": [], "monthly_visits": None}
        if domain and conf.builtwith_api_key:
            out["tech_stack"] = enrich.fetch_tech_stack(domain, conf.builtwith_api_key, cache=cache)
        if domain and conf.similarweb_api_key:
            out["monthly_visits"] = enrich.fetch_monthly_visits(domain, conf.similarweb_api_key, today=today, cache=cache)
        return out
    return live


def _scorer(conf: SaasScoutConfig, dry_run: bool, log) -> Callable[[dict[str, Any]], dict[str, Any]]:
    if dry_run or not conf.openai_api_key:
        if not dry_run:
            log("[score] OPENAI_API_KEY not set; using heuristic scorer")
        return scoring.heuristic_score
    client = LLMClient(api_key=conf.openai_api_key, model=conf.openai_model, base_url=conf.openai_base_url, temperature=conf.temperature, max_tokens=500)
    return lambda listing: scoring.score_listing(listing, client)


def _sender(conf: SaasScoutConfig, deliver: bool) -> Sender:
    if deliver and (conf.slack_webhook_url or (conf.slack_bot_token and conf.slack_channel)):
        return SlackClient(webhook_url=conf.slack_webhook_url, bot_token=conf.slack_bot_token, channel=conf.slack_channel)
    return StdoutSender()

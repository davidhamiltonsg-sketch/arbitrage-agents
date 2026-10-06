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


DOMAIN_SOURCES = ("auto", "whoisfreaks", "whoisfreaks-free")
AUTHORITY_SOURCES = ("auto", "dataforseo", "openpagerank", "none")


@dataclass
class DomainFlipperConfig:
    domain_source: str = "auto"
    authority_source: str = "auto"
    openpagerank_api_key: str | None = None
    whoisfreaks_api_key: str | None = None
    tlds: tuple[str, ...] = ("com", "ai")
    filters: filters.FilterConfig = field(default_factory=filters.FilterConfig)
    dataforseo_auth: str | None = None
    dr_divisor: float = 10.0
    gate: enrich.AuthorityGate = field(default_factory=enrich.AuthorityGate)
    max_enrich: int = 400           # paid lookups (DataForSEO) per run
    max_openpagerank: int = 5000    # free lookups (Open PageRank, 100 per call) per run
    max_llm_score: int = 100
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
            domain_source=(cfg.env("DOMAIN_SOURCE", "auto") or "auto").lower(),
            authority_source=(cfg.env("AUTHORITY_SOURCE", "auto") or "auto").lower(),
            openpagerank_api_key=cfg.env("OPENPAGERANK_API_KEY"),
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
            max_openpagerank=cfg.env_int("DOMAIN_MAX_OPENPAGERANK", 5000),
            max_llm_score=cfg.env_int("DOMAIN_MAX_LLM_SCORE", 100),
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


    def resolved_domain_source(self) -> str:
        if self.domain_source not in DOMAIN_SOURCES:
            raise SystemExit(f"DOMAIN_SOURCE must be one of {', '.join(DOMAIN_SOURCES)}, got {self.domain_source!r}")
        if self.domain_source == "auto":
            return "whoisfreaks" if self.whoisfreaks_api_key else "whoisfreaks-free"
        return self.domain_source

    def resolved_authority_source(self) -> str:
        if self.authority_source not in AUTHORITY_SOURCES:
            raise SystemExit(f"AUTHORITY_SOURCE must be one of {', '.join(AUTHORITY_SOURCES)}, got {self.authority_source!r}")
        if self.authority_source == "auto":
            if self.dataforseo_auth:
                return "dataforseo"
            if self.openpagerank_api_key:
                return "openpagerank"
            return "none"
        return self.authority_source


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
    explicit_date = drop_date
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
        source = conf.resolved_domain_source()
        try:
            records = None
            if source == "whoisfreaks":
                if not conf.whoisfreaks_api_key:
                    raise SystemExit("DOMAIN_SOURCE=whoisfreaks needs WHOISFREAKS_API_KEY (or use DOMAIN_SOURCE=whoisfreaks-free)")
                try:
                    records = sources.fetch_dropped_domains(conf.whoisfreaks_api_key, date=drop_date, tlds=conf.tlds)
                    log(f"[fetch] WhoisFreaks returned {len(records)} dropped domains for {drop_date}")
                except sources.SourceError as exc:
                    if conf.domain_source != "auto" or not exc.is_plan_problem:
                        raise
                    log(f"::warning title=Paid feed unavailable::WhoisFreaks rejected the key; using the free public feed instead. {exc}")
                    audit.record("source_fallback", "whoisfreaks", reason=str(exc))
            if records is None:
                records = sources.fetch_free_dropped_domains(date=explicit_date)
                log(f"[fetch] free WhoisFreaks GitHub feed returned {len(records)} dropped domains ({'file for ' + explicit_date if explicit_date else 'latest file'})")
        except sources.SourceError as exc:
            raise SystemExit(f"[fetch] {exc}") from exc
    funnel["fetched"] = len(records)
    for record in records:
        audit.record("fetched", record["domain"], drop_date=record.get("drop_date"), registrar=record.get("registrar"))

    # Phase 3: deterministic filter
    kept, rejected = filters.apply_filters(records, conf.filters)
    for record, reason in rejected:
        audit.record("filtered_out", record["domain"], reason=reason)
    funnel["after_filters"] = len(kept)
    log(f"[filter] {len(kept)} candidates remain ({len(rejected)} rejected)")
    authority = "fixture" if dry_run else conf.resolved_authority_source()
    cap = {"dataforseo": conf.max_enrich, "openpagerank": conf.max_openpagerank}.get(authority)
    if cap is not None and len(kept) > cap:
        # Spend lookups on the most brandable names first, not on feed order.
        kept = sorted(kept, key=lambda r: -scoring.heuristic_score(r)["brandability"])[:cap]
        log(f"[filter] capping {authority} lookups at {cap} candidates, best names first")

    # Phase 4: enrich + hard gate
    enriched: list[dict[str, Any]] = []
    metrics_by_domain = _enrich_all(conf, cache, authority, kept, audit, log)
    for record in kept:
        metrics = metrics_by_domain.get(record["domain"])
        if metrics is None:
            continue  # lookup failed; already audited
        candidate = {**record, **metrics}
        candidate.pop("_fixture_metrics", None)
        audit.record("enriched", record["domain"], dr=metrics["dr"], referring_domains=metrics["referring_domains"], total_backlinks=metrics["total_backlinks"])
        if authority == "none" or enrich.passes_authority_gate(metrics, conf.gate):
            enriched.append(candidate)
        else:
            audit.record("gated_out", record["domain"], dr=metrics["dr"], referring_domains=metrics["referring_domains"])
    notes: list[str] = []
    if authority not in ("none", "fixture") and not enriched and metrics_by_domain:
        # Nothing cleared the gate. Relax in two steps so the digest is never empty for
        # a threshold reason: any measurable authority first, then name quality alone.
        with_signal = [
            {**r, **metrics_by_domain[r["domain"]]}
            for r in kept
            if r["domain"] in metrics_by_domain and float(metrics_by_domain[r["domain"]].get("dr") or 0) > 0
        ]
        if with_signal:
            with_signal.sort(key=lambda r: -float(r.get("dr") or 0))
            enriched = with_signal
            notes.append(f"Gate relaxed: nothing reached authority {conf.gate.min_dr:g}; showing the {len(enriched)} candidates with any measurable authority.")
        else:
            enriched = [{**r, **metrics_by_domain[r["domain"]]} for r in kept if r["domain"] in metrics_by_domain]
            notes.append("Gate relaxed: no candidate has measurable authority in the index today; ranked on name quality alone.")
        for item in enriched:
            item.pop("_fixture_metrics", None)
        log(f"::warning title=Authority gate relaxed::{notes[-1]}")
        funnel["gate_relaxed"] = 1
    funnel["after_authority_gate"] = len(enriched)
    if authority == "none":
        log(f"[enrich] no authority source configured; gate skipped, {len(enriched)} candidates go to scoring on name quality alone")
    elif not notes:
        log(f"[enrich] {len(enriched)} pass the authority gate via {authority} (DR >= {conf.gate.min_dr:g}, refs >= {conf.gate.min_referring_domains} where available)")

    # Phase 5: AI score (LLM calls are capped; the heuristic pre-ranks the field first)
    scorer = _scorer(conf, dry_run, log)
    to_score = enriched
    if scorer is not scoring.heuristic_score and len(enriched) > conf.max_llm_score:
        pre = sorted(enriched, key=lambda r: (-scoring.heuristic_score(r)["score"], -scoring.heuristic_score(r)["brandability"]))
        to_score = pre[: conf.max_llm_score]
        skipped = {id(r) for r in enriched} - {id(r) for r in to_score}
        for candidate in enriched:
            if id(candidate) in skipped:
                audit.record("llm_skipped", candidate["domain"], reason="below heuristic pre-rank cut")
        log(f"[score] {len(enriched)} candidates; sending the top {len(to_score)} by heuristic pre-rank to the model (DOMAIN_MAX_LLM_SCORE)")
    scored: list[dict[str, Any]] = []
    consecutive_failures = 0
    for candidate in to_score:
        try:
            result = scorer(candidate)
            consecutive_failures = 0
        except Exception as exc:
            audit.record("score_error", candidate["domain"], error=str(exc))
            log(f"[score] {candidate['domain']}: {exc}")
            consecutive_failures += 1
            if scorer is not scoring.heuristic_score and consecutive_failures >= LLM_CIRCUIT_BREAKER:
                log(f"::warning title=Model scoring disabled::{LLM_CIRCUIT_BREAKER} consecutive OpenAI failures; using the heuristic scorer for the rest of this run. Last error: {exc}")
                audit.record("llm_circuit_open", "-", error=str(exc))
                scorer = scoring.heuristic_score
                to_score = enriched  # heuristic is free: score everything after all
                scored = [{**c, **scoring.heuristic_score(c)} for c in enriched[: enriched.index(candidate) + 1]]
                for item in scored:
                    audit.record("scored", item["domain"], score=item["score"], brandability=item["brandability"], suggested_price=item["suggested_price"], reasoning=item["reasoning"])
                remaining = enriched[enriched.index(candidate) + 1:]
                for c in remaining:
                    item = {**c, **scoring.heuristic_score(c)}
                    audit.record("scored", c["domain"], score=item["score"], brandability=item["brandability"], suggested_price=item["suggested_price"], reasoning=item["reasoning"])
                    scored.append(item)
                break
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
    text, blocks = digest.build_digest(shortlist, drop_date, funnel=funnel, notes=notes)
    out = sender or _sender(conf, deliver and not dry_run)
    out.send(text=text, blocks=blocks)
    log(f"[deliver] digest with {len(shortlist)} items sent via {type(out).__name__}")

    # Phase 8 (audit): push to Sheets when configured
    if not dry_run and audit.flush_to_sheets():
        log("[audit] rows pushed to Google Sheets webhook")
    cache.close()
    return RunResult(audit.run_id, shortlist, funnel, text, blocks)


LLM_CIRCUIT_BREAKER = 3

NO_AUTHORITY = {"rank": None, "dr": None, "referring_domains": None, "total_backlinks": None, "spam_score": None, "first_seen": None, "authority_source": "none"}


def _enrich_all(conf: DomainFlipperConfig, cache: Cache, authority: str, records: list[dict[str, Any]], audit: AuditLog, log) -> dict[str, dict[str, Any]]:
    """Return metrics per domain. Missing entries mean the lookup failed (already audited)."""
    out: dict[str, dict[str, Any]] = {}
    if authority == "fixture":
        for record in records:
            fixture = record.get("_fixture_metrics")
            if isinstance(fixture, dict):
                rank_value = float(fixture.get("rank", fixture.get("dr", 0) * conf.dr_divisor))
                out[record["domain"]] = {
                    "rank": rank_value,
                    "dr": round(rank_value / conf.dr_divisor, 1),
                    "referring_domains": int(fixture.get("referring_domains", 0)),
                    "total_backlinks": int(fixture.get("total_backlinks", 0)),
                    "spam_score": fixture.get("spam_score"),
                    "first_seen": fixture.get("first_seen"),
                }
            else:
                out[record["domain"]] = enrich.synthetic_metrics(record["domain"])
        return out
    if authority == "none":
        return {record["domain"]: dict(NO_AUTHORITY) for record in records}
    if authority == "openpagerank":
        if not conf.openpagerank_api_key:
            raise SystemExit("AUTHORITY_SOURCE=openpagerank needs OPENPAGERANK_API_KEY")
        domains = [r["domain"] for r in records]
        for start in range(0, len(domains), enrich.OPENPAGERANK_BATCH):
            chunk = domains[start:start + enrich.OPENPAGERANK_BATCH]
            try:
                out.update(enrich.fetch_openpagerank(chunk, conf.openpagerank_api_key, cache=cache))
            except Exception as exc:
                for domain in chunk:
                    audit.record("enrich_error", domain, error=str(exc))
                log(f"[enrich] Open PageRank batch of {len(chunk)} failed: {exc}")
        return out
    if authority == "dataforseo":
        if not conf.dataforseo_auth:
            raise SystemExit("AUTHORITY_SOURCE=dataforseo needs DATAFORSEO_LOGIN/PASSWORD or DATAFORSEO_AUTH")
        for record in records:
            try:
                out[record["domain"]] = enrich.fetch_backlink_summary(record["domain"], conf.dataforseo_auth, cache=cache, dr_divisor=conf.dr_divisor)
            except Exception as exc:  # one bad lookup must not sink the run
                audit.record("enrich_error", record["domain"], error=str(exc))
                log(f"[enrich] {record['domain']}: {exc}")
        return out
    raise SystemExit(f"unknown authority source {authority!r}")


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

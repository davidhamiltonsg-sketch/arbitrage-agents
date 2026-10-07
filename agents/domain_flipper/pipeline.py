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
from . import availability, digest, diligence, enrich, filters, scoring, sources

FIXTURE_PATH = cfg.PROJECT_ROOT / "fixtures" / "dropped_domains.sample.json"


DOMAIN_SOURCES = ("auto", "whoisfreaks", "whoisfreaks-free")
AUTHORITY_SOURCES = ("auto", "dataforseo", "openpagerank", "none")
DEEP_AUTHORITY_SOURCES = ("auto", "dataforseo", "none")
AVAILABILITY_CHECKS = ("auto", "rdap", "none")
APPRAISAL_SOURCES = ("auto", "humbleworth", "godaddy", "none")


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
    # Deep enrichment: paid DataForSEO link counts for the best candidates only (after the free gate).
    deep_authority: str = "auto"
    max_deep_enrich: int = 25
    # RDAP availability check (free): drop names a drop-catcher already re-registered.
    availability_check: str = "auto"
    max_availability: int = 150
    godaddy_api_key: str | None = None
    godaddy_api_secret: str | None = None
    godaddy_api_base: str = diligence.GODADDY_API_BASE
    appraisal_source: str = "auto"
    replicate_api_token: str | None = None
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
        if cfg.env("DATAFORSEO_AUTH") or cfg.env("DATAFORSEO_PASSWORD"):
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
            deep_authority=(cfg.env("DEEP_AUTHORITY_SOURCE", "auto") or "auto").lower(),
            max_deep_enrich=cfg.env_int("DOMAIN_MAX_DEEP_ENRICH", 25),
            availability_check=(cfg.env("DOMAIN_AVAILABILITY_CHECK", "auto") or "auto").lower(),
            max_availability=cfg.env_int("DOMAIN_MAX_AVAILABILITY", 150),
            godaddy_api_key=cfg.env("GODADDY_API_KEY"),
            godaddy_api_secret=cfg.env("GODADDY_API_SECRET"),
            godaddy_api_base=cfg.env("GODADDY_API_BASE", diligence.GODADDY_API_BASE) or diligence.GODADDY_API_BASE,
            appraisal_source=(cfg.env("APPRAISAL_SOURCE", "auto") or "auto").lower(),
            replicate_api_token=cfg.env("REPLICATE_API_TOKEN"),
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
            # The free, batchable source gates the whole field; DataForSEO (metered) goes deep on the best names.
            if self.openpagerank_api_key:
                return "openpagerank"
            if self.dataforseo_auth:
                return "dataforseo"
            return "none"
        return self.authority_source

    def resolved_deep_authority(self) -> str:
        if self.deep_authority not in DEEP_AUTHORITY_SOURCES:
            raise SystemExit(f"DEEP_AUTHORITY_SOURCE must be one of {', '.join(DEEP_AUTHORITY_SOURCES)}, got {self.deep_authority!r}")
        if self.deep_authority == "auto":
            return "dataforseo" if self.dataforseo_auth and self.resolved_authority_source() != "dataforseo" else "none"
        return self.deep_authority

    def resolved_availability_check(self) -> str:
        if self.availability_check not in AVAILABILITY_CHECKS:
            raise SystemExit(f"DOMAIN_AVAILABILITY_CHECK must be one of {', '.join(AVAILABILITY_CHECKS)}, got {self.availability_check!r}")
        return "rdap" if self.availability_check == "auto" else self.availability_check

    def godaddy_auth(self) -> str | None:
        return diligence.godaddy_auth_header(self.godaddy_api_key, self.godaddy_api_secret)

    def resolved_appraisal_source(self) -> str:
        if self.appraisal_source not in APPRAISAL_SOURCES:
            raise SystemExit(f"APPRAISAL_SOURCE must be one of {', '.join(APPRAISAL_SOURCES)}, got {self.appraisal_source!r}")
        if self.appraisal_source == "auto":
            if self.replicate_api_token:
                return "humbleworth"
            return "godaddy" if self.godaddy_auth() else "none"
        return self.appraisal_source


@dataclass
class RunResult:
    run_id: str
    shortlist: list[dict[str, Any]]
    funnel: dict[str, int]
    digest_text: str
    digest_blocks: list[dict[str, Any]]
    notes: list[str] = field(default_factory=list)
    mode: str = "live"
    date: str = ""
    sources: dict[str, str] = field(default_factory=dict)

    def export(self) -> dict[str, Any]:
        """JSON-serialisable record for the dashboard and history."""
        import os

        run_url = None
        if os.environ.get("GITHUB_RUN_ID") and os.environ.get("GITHUB_REPOSITORY"):
            run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
        items = []
        for item in self.shortlist:
            clean = {k: v for k, v in item.items() if not k.startswith("_")}
            clean["links"] = digest.registrar_links(item["domain"])
            items.append(clean)
        return {
            "agent": "domain-flipper",
            "run_id": self.run_id,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "date": self.date,
            "mode": self.mode,
            "sources": self.sources,
            "funnel": self.funnel,
            "notes": self.notes,
            "run_url": run_url,
            "shortlist": items,
        }


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
        used_source = source
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
                used_source = "whoisfreaks-free"
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

    # Phase 4b: availability (RDAP). A dropped name a drop-catcher already took is not a deal.
    enriched = _check_availability(conf, cache, enriched, dry_run, audit, funnel, log)

    # Phase 4c: deep enrichment + appraisal on the best names only (metered DataForSEO, free GoDaddy GoValue)
    enriched = _deep_enrich(conf, cache, authority, enriched, dry_run, audit, funnel, log)

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

    # Phase 6b: due diligence on the shortlist only (Wayback history + trademark screen + appraisal)
    if not dry_run:
        missing = [i for i in shortlist if "appraisal" not in i]
        if missing:
            for domain, appraisal in _appraise_many(conf, cache, [i["domain"] for i in missing]).items():
                for i in missing:
                    if i["domain"] == domain:
                        i["appraisal"] = appraisal
    for item in shortlist:
        if dry_run:
            item.update(diligence.sample_diligence(item["domain"]))
            item.setdefault("appraisal", diligence.sample_appraisal(item["domain"]))
        else:
            item["wayback"] = diligence.wayback_summary(item["domain"])
            item["trademark"] = diligence.trademark_screen(item["domain"])
            item.setdefault("appraisal", {"status": "unconfigured", "source": "none", "value": None, "comparables": []})
        audit.record("diligence", item["domain"], wayback=item["wayback"].get("status"), trademark_risk=item["trademark"].get("risk"),
                     appraisal=item["appraisal"].get("value"), availability=(item.get("availability") or {}).get("status"))
    if shortlist:
        log(f"[diligence] Wayback + trademark screen done for {len(shortlist)} shortlisted domains")

    # Phase 7: deliver
    text, blocks = digest.build_digest(shortlist, drop_date, funnel=funnel, notes=notes)
    out = sender or _sender(conf, deliver and not dry_run)
    out.send(text=text, blocks=blocks)
    log(f"[deliver] digest with {len(shortlist)} items sent via {type(out).__name__}")

    # Phase 8 (audit): push to Sheets when configured
    if not dry_run and audit.flush_to_sheets():
        log("[audit] rows pushed to Google Sheets webhook")
    cache.close()
    return RunResult(
        audit.run_id, shortlist, funnel, text, blocks,
        notes=notes,
        mode="dry-run" if dry_run else "live",
        date=drop_date,
        sources={
            "domain": "fixture" if (dry_run or fixture_path) else used_source,
            "authority": authority,
            "deep_authority": "fixture" if dry_run else conf.resolved_deep_authority(),
            "availability": "sample" if dry_run else conf.resolved_availability_check(),
            "appraisal": "sample" if dry_run else conf.resolved_appraisal_source(),
        },
    )


def _prerank(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Best names first by the free heuristic, so capped lookups go where they matter."""
    return sorted(candidates, key=lambda r: (-scoring.heuristic_score(r)["score"], -scoring.heuristic_score(r)["brandability"]))


def _check_availability(conf, cache: Cache, candidates: list[dict[str, Any]], dry_run: bool, audit: AuditLog, funnel: dict[str, int], log) -> list[dict[str, Any]]:
    if not candidates:
        return candidates
    if dry_run:
        for item in candidates:
            item["availability"] = availability.sample_availability(item["domain"])
        return candidates
    mode = conf.resolved_availability_check()
    if mode == "none":
        return candidates
    ordered = _prerank(candidates)
    to_check, rest = ordered[: conf.max_availability], ordered[conf.max_availability:]
    bases = availability.load_rdap_bases(cache)
    kept: list[dict[str, Any]] = []
    taken = 0
    unknown = 0
    for item in to_check:
        result = availability.check_availability(item["domain"], bases=bases)
        item["availability"] = result
        audit.record("availability", item["domain"], status=result["status"], registrar=result.get("registrar"), note=result.get("note"))
        if result["status"] in availability.BLOCKING_STATUSES:
            taken += 1
            continue
        if result["status"] == availability.STATUS_UNKNOWN:
            unknown += 1
        kept.append(item)
    for item in rest:
        item["availability"] = {"status": availability.STATUS_UNCHECKED, "source": "rdap", "checked_at": None}
        kept.append(item)
    keep_ids = {id(item) for item in kept}
    kept = [item for item in candidates if id(item) in keep_ids]  # original order (e.g. relaxed-gate ranking) survives
    funnel["available"] = len(kept)
    log(f"[availability] RDAP checked {len(to_check)}: {taken} already re-registered and dropped, {unknown} unknown"
        + (f", {len(rest)} beyond the cap left unchecked" if rest else ""))
    return kept


def _appraise_many(conf, cache: Cache, domains: list[str]) -> dict[str, dict[str, Any]]:
    """Appraise domains with the configured source; one Replicate call for HumbleWorth, per-domain for GoDaddy."""
    source = conf.resolved_appraisal_source()
    if source == "humbleworth":
        if not conf.replicate_api_token:
            raise SystemExit("APPRAISAL_SOURCE=humbleworth needs REPLICATE_API_TOKEN")
        return diligence.humbleworth_appraise(domains, conf.replicate_api_token, cache=cache)
    if source == "godaddy":
        auth = conf.godaddy_auth()
        if not auth:
            raise SystemExit("APPRAISAL_SOURCE=godaddy needs GODADDY_API_KEY and GODADDY_API_SECRET")
        return {d: diligence.appraise(d, auth, cache=cache, base_url=conf.godaddy_api_base) for d in domains}
    return {d: {"status": "unconfigured", "source": "none", "value": None, "comparables": []} for d in domains}


def _deep_enrich(conf, cache: Cache, authority: str, candidates: list[dict[str, Any]], dry_run: bool, audit: AuditLog, funnel: dict[str, int], log) -> list[dict[str, Any]]:
    if not candidates or dry_run:
        return candidates
    deep = conf.resolved_deep_authority()
    appraisal_source = conf.resolved_appraisal_source()
    if deep == "none" and appraisal_source == "none":
        return candidates
    ordered = _prerank(candidates)
    top = ordered[: conf.max_deep_enrich]
    if deep == "dataforseo":
        if not conf.dataforseo_auth:
            raise SystemExit("DEEP_AUTHORITY_SOURCE=dataforseo needs DATAFORSEO_LOGIN/PASSWORD or DATAFORSEO_AUTH")
        done = 0
        for item in top:
            try:
                metrics = enrich.fetch_backlink_summary(item["domain"], conf.dataforseo_auth, cache=cache, dr_divisor=conf.dr_divisor)
            except Exception as exc:
                audit.record("deep_enrich_error", item["domain"], error=str(exc))
                status = getattr(exc, "status", None)
                if status in (401, 402, 403):
                    # Credentials or balance problem: every further call would fail the same way.
                    log("::warning title=DataForSEO rejected the credentials::"
                        f"HTTP {status}; deep link counts skipped this run. DATAFORSEO_LOGIN is the API login (an email) and "
                        f"DATAFORSEO_PASSWORD the API password from app.dataforseo.com/api-access, not the account password. {exc}")
                    break
                log(f"[deep] {item['domain']}: {exc}")
                continue
            item["gate_dr"] = item.get("dr")
            item.update(metrics)
            item["authority_source"] = f"{authority}+dataforseo" if authority not in ("none", "dataforseo") else "dataforseo"
            audit.record("deep_enriched", item["domain"], dr=metrics["dr"], referring_domains=metrics["referring_domains"], total_backlinks=metrics["total_backlinks"], spam_score=metrics.get("spam_score"))
            done += 1
        funnel["deep_enriched"] = done
        log(f"[deep] DataForSEO link counts for the top {done} of {len(candidates)} candidates (DOMAIN_MAX_DEEP_ENRICH={conf.max_deep_enrich})")
    if appraisal_source != "none":
        appraisals = _appraise_many(conf, cache, [i["domain"] for i in top])
        for item in top:
            item["appraisal"] = appraisals.get(item["domain"].lower()) or {"status": "none", "source": appraisal_source, "value": None, "comparables": []}
            audit.record("appraised", item["domain"], status=item["appraisal"].get("status"), value=item["appraisal"].get("value"))
        valued = sum(1 for i in top if i["appraisal"].get("value") is not None)
        failed = next((i["appraisal"] for i in top if i["appraisal"].get("status") in ("denied", "error")), None)
        if failed:
            why = failed.get("error", "")
            hint = (" A 401 means the key or secret is wrong (or an OTE test key); a 403 ACCESS_DENIED means GoDaddy restricts the API for this account."
                    if appraisal_source == "godaddy" else "")
            log(f"::warning title={appraisal_source} appraisal {failed.get('status')}::{why}.{hint} Appraisals affected this run.")
        log(f"[appraise] {appraisal_source} valued {valued} of {len(top)} candidates")
    return candidates


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

# Arbitrage Agents

Standalone, dependency-free implementations of the two "flipper" agents from
the Passive Arbitrage Agent Cookbook: a daily dropped-domain shortlist and a
weekly neglected-SaaS shortlist, each delivered to your phone as a GitHub
issue, with Slack as an optional extra.

| Agent | Cadence | Pipeline | Shortlist |
| --- | --- | --- | --- |
| **Domain Flipper** | daily 06:00 | WhoisFreaks dropped feed → deterministic filter → DataForSEO backlinks → authority gate → OpenAI strict JSON → rank | top 5 with GoDaddy/Namecheap checkout links |
| **Dead SaaS Scout** | weekly Monday 07:00 | listings (file / Acquire.com / Chrome Web Store) → deterministic filter → BuiltWith + Similarweb → traffic gate → OpenAI strict JSON → threshold + rank | top 3 with seller contact links |

Each agent ships twice: as a Python package driven by `cli.py`, and as an
importable n8n workflow under `n8n/`. Both share the same prompts, schemas,
thresholds and Slack layout.

## Free mode versus paid mode

The Domain Flipper runs live with **no paid keys**:

| Piece | Free option (default) | Paid option |
| --- | --- | --- |
| Dropped-domain feed | WhoisFreaks' public GitHub sample: 10,000 dropped domains a day, partial gTLD coverage, no key (`DOMAIN_SOURCE=whoisfreaks-free`) | WhoisFreaks Domainer package: ~400,000 a day, all TLDs (`DOMAIN_SOURCE=whoisfreaks` + `WHOISFREAKS_API_KEY`) |
| Authority metrics | Open PageRank: free API key, 0 to 10 score from the Common Crawl host graph, no link counts (`AUTHORITY_SOURCE=openpagerank` + `OPENPAGERANK_API_KEY`) | DataForSEO: DR-like rank plus referring domains, backlinks and spam score. With both keys set, Open PageRank gates the whole field and DataForSEO goes deep on the top `DOMAIN_MAX_DEEP_ENRICH` (25) names only, so the metered spend stays at a few cents a run |
| Availability | Registry RDAP lookup, no key: names a drop-catcher already re-registered are dropped before scoring; "pending delete" names are flagged for a backorder (`DOMAIN_AVAILABILITY_CHECK=rdap`, cap `DOMAIN_MAX_AVAILABILITY=150`) | – |
| Appraisal | HumbleWorth's open valuation model on Replicate: auction, marketplace and brokerage estimates for the whole deep set in one call, about $0.0001 a run (`REPLICATE_API_TOKEN`); the model sees it before scoring | GoDaddy GoValue with comparable sales (`GODADDY_API_KEY` + `GODADDY_API_SECRET`), but only for accounts GoDaddy still allows on its API: 10+ domains or Discount Domain Club since May 2024 |
| Scoring | Built-in rubric heuristic | OpenAI structured output (`OPENAI_API_KEY`) |
| Delivery | GitHub issue + run summary | Slack webhook on top |

`auto` (the default for both source settings) picks the paid option whenever
its key is present and the free one otherwise. With no authority source at
all the gate is skipped and domains are ranked on name quality alone, which
is noticeably weaker; a free Open PageRank key is the single most useful
addition. The free feed is a sample, so the best drops of the day may not be
in it; that is the trade-off against the paid feed.

The n8n workflow for Agent 1 still targets the paid APIs.

## Quick start

Python 3.10 or newer, no packages to install.

```bash
git clone https://github.com/davidhamiltonsg-sketch/arbitrage-agents.git
cd arbitrage-agents
cp .env.example .env            # fill in keys later; dry runs need none

python3 cli.py domain-flipper --dry-run
python3 cli.py saas-scout --dry-run

python3 -m unittest discover -s tests -t . -v
```

A dry run loads the fixtures in `fixtures/`, uses the metrics embedded in
them (or deterministic synthetic metrics), scores with the rubric-only
heuristic scorer, and prints the Slack payload instead of posting. It makes no
network calls at all, which the test suite enforces.

### Live runs

```bash
python3 cli.py domain-flipper                    # yesterday's drops, posts to Slack
python3 cli.py domain-flipper --date 2026-10-05 --top 3 --no-deliver
python3 cli.py saas-scout --listings exports/acquire.json
```

With `OPENAI_API_KEY` unset the heuristic scorer is used; with the DataForSEO
or BuiltWith/Similarweb keys unset the corresponding enrichment is skipped
(the domain agent then falls back to synthetic metrics, which you do not want
in production, so set the keys).

Schedule with cron:

```cron
0 6 * * *  cd /path/to/arbitrage-agents && python3 cli.py domain-flipper >> logs/cron.log 2>&1
0 7 * * 1  cd /path/to/arbitrage-agents && python3 cli.py saas-scout     >> logs/cron.log 2>&1
```

### Run it from GitHub (no laptop needed)

`.github/workflows/run.yml` runs the agents on GitHub's
servers on the cookbook schedule (06:00 UTC daily for the Domain Flipper,
07:00 UTC Mondays for the SaaS Scout) and on demand. Everything can be done
from the GitHub mobile app or a phone browser.

1. **Add keys** (all optional) at *Settings → Secrets and variables → Actions →
   New repository secret*. Free and most useful first: `OPENPAGERANK_API_KEY`.
   Near-free: `REPLICATE_API_TOKEN` for HumbleWorth appraisals (GoDaddy's `GODADDY_API_KEY`
   and `GODADDY_API_SECRET` work only for accounts GoDaddy admits to its API).
   Paid upgrades: `WHOISFREAKS_API_KEY` (Domainer package), `DATAFORSEO_LOGIN`
   and `DATAFORSEO_PASSWORD` (both needed, used on the top names only), and `OPENAI_API_KEY` for AI scoring. For the SaaS Scout also
   `SAAS_LISTINGS_URL` (a URL that returns the listings JSON, such as an Apify
   dataset items URL) and optionally `BUILTWITH_API_KEY`, `SIMILARWEB_API_KEY`.
   Thresholds such as `DOMAIN_TOP_N` go under *Variables* instead of secrets.
2. **Trigger a run** at *Actions → Run arbitrage agents → Run workflow*. Leave
   *Dry run* ticked the first time: it needs no keys and prints the digest in
   the job log so you can see the output shape. Untick it for a live run.
3. **Read the result** on the run's *Summary* page (every run) and as a
   GitHub issue labelled `arbitrage-digest` (live runs, or any run with
   *Open issue* ticked), which the GitHub app notifies you about. Slack is
   optional on top: set `SLACK_WEBHOOK_URL` and live runs post there too.
   Audit logs are attached to every run as an artifact.

Missing keys degrade rather than fail: no feed key uses the free public
sample feed, no authority key skips the gate, no OpenAI key uses the heuristic
scorer, no listings URL makes the SaaS Scout fall back to a dry run.

### Phone dashboard

A private Claude artifact, the **Arbitrage Console**, reads this repository
through your claude.ai GitHub connector and needs no server of its own:

- the latest shortlist from `data/domain-flipper/latest.json` with registry
  availability, authority, the GoDaddy appraisal, Wayback history (first and last archived year, monthly snapshots, links to
  the timeline and the latest copy) and the trademark screen with prefilled
  USPTO, WIPO and EUIPO searches;
- **Bought** / **Pass** decisions kept in the artifact's own store, so they
  work even when the connector can only read the repository;
- **Run now**, which opens an issue labelled `run-request`; the runner picks it
  up, replies with the digest and closes it;
- thresholds edited in `settings.env`, loaded before every run.

Run now and the settings editor need the Claude GitHub App to have **Contents**
and **Issues** write access on this repository; until then the console shows
direct GitHub links for the same actions.

The runner writes `data/<agent>/latest.json` and a dated copy under
`data/<agent>/history/` after each live run.

### n8n

Import `n8n/domain_flipper.workflow.json` and `n8n/saas_scout.workflow.json`
(Workflows → Import from file). Each has a sticky note listing the credentials
to attach and the placeholders to replace. Thresholds live at the top of the
Code nodes. The JSON is generated by `n8n/build_workflows.py`; edit the
generator and re-run it rather than editing the JSON by hand (a test fails if
they drift apart).

## Layout

```
cli.py                      entry point: domain-flipper | saas-scout
agents/common/              http (retries/backoff), sqlite cache, OpenAI strict output,
                            Slack Block Kit, JSONL audit log, ranking, .env loading
agents/domain_flipper/      sources, filters, enrich, scoring, digest, pipeline
agents/saas_scout/          sources, filters, enrich, scoring, digest, pipeline
fixtures/                   sample feeds used by --dry-run and the tests
n8n/                        workflow generator + generated JSON
tests/                      unittest suite (offline; any network call fails the test)
logs/, .cache/              created at runtime, git-ignored
```

## Operating rules baked in

1. **Filters before paid calls.** Deterministic rules (length, hyphens, digits,
   TLD, trademark prefixes, price, staleness, category, footprint) run before
   any enrichment or model call. `DOMAIN_MAX_ENRICH` caps paid lookups per run.
2. **Strict JSON only.** Scoring uses `response_format: json_schema` with
   `strict: true`. Markdown fences, prose and refusals raise and the item is
   skipped. Strict mode rejects `minimum`/`maximum`, so numeric ranges are
   clamped in code after parsing.
3. **Direct action links.** Every digest item carries registrar checkout links
   or the seller contact URL. No generic search links.
4. **Isolation.** The two agents share library code but no state; a failure in
   one lookup is logged and skipped, never fatal to the run.
5. **Audit trail.** Every fetched, filtered, enriched, gated, scored and
   shortlisted record is appended to `logs/<agent>.jsonl` with a run id. Set
   `GOOGLE_SHEETS_WEBHOOK_URL` to an Apps Script web app that appends the
   POSTed `rows` and the same rows land in a sheet.
6. **Caching.** Enrichment responses are cached in SQLite for seven days, keyed
   by a hash of the asset id, so re-runs and retries do not re-bill.

## Corrections to the cookbook

These differ from the source document on purpose:

- **WhoisFreaks endpoint.** The dropped-domains feed is
  `https://files.whoisfreaks.com/v3.1/domains/dropped` with `apiKey`, optional
  `date` (yyyy-MM-dd) and `tlds`, not `api.whoisfreaks.com/v1.0/...`. The
  parser accepts JSON or CSV, gzip or zip, with or without a header row.
- **DR scale.** DataForSEO's `rank` is 0 to 1000. It is divided by
  `DR_SCALE_DIVISOR` (default 10) to approximate the 0 to 100 DR the cookbook's
  thresholds and prompt assume. Set the divisor to 1 if you swap in a vendor
  that returns DR directly.
- **Strict schemas.** The cookbook's schemas include `minimum`/`maximum`,
  which OpenAI strict mode rejects. They are removed before sending and
  enforced after parsing.
- **Acquire.com and the Chrome Web Store have no public API.** The reliable
  path is `SAAS_SOURCES=file` with a normalised JSON export (for example an
  Apify dataset). `acquire` reads the public sitemap and each listing's JSON-LD
  without logging in, so prices and metrics may be missing. `chromewebstore`
  parses the detail pages for the extension IDs you list and treats them as
  outreach targets (no asking price). Both parsers are best effort and may
  need adjusting when those sites change their markup.
- **Trademark filter** is a prefix match on the second-level label, as in the
  cookbook, with a longer default list. It is a cheap screen, not legal
  clearance. Check a shortlisted name against a trademark database before
  buying it.

## Listing file format (SaaS Scout)

A JSON array (or `{"listings": [...]}`) of objects. Unknown keys are ignored;
common aliases (`title`, `url`, `price`, `users`, `website`) are accepted.

```json
{
  "asset_id": "acq_11",
  "name": "InvoiceNudge",
  "category": "Finance SaaS",
  "source_url": "https://app.acquire.com/startup/invoicenudge",
  "asking_price": 7800,
  "claimed_users": 1450,
  "last_updated_date": "2024-01-20",
  "domain": "invoicenudge.example",
  "description": "Sends manual reminder emails for overdue invoices.",
  "seller_contact_url": "https://app.acquire.com/startup/invoicenudge"
}
```

## Not included

Agent 3 (API capacity arbitrage) is deliberately omitted. Reselling Anthropic
or OpenAI API quota through third-party routing networks breaches both
providers' terms of service, and the networks the cookbook names could not be
verified to exist.

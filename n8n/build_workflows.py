#!/usr/bin/env python3
"""Generate importable n8n workflow JSON for both agents.

    python3 n8n/build_workflows.py        # writes n8n/*.workflow.json

The JSON is generated rather than hand-edited so the Code-node JavaScript can
be kept readable here. Credentials are referenced by name only; after import,
open each HTTP Request node and attach your own credentials, then replace the
Slack webhook URL and Google Sheet ID placeholders.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

OUT_DIR = Path(__file__).resolve().parent

SLACK_WEBHOOK_PLACEHOLDER = "https://hooks.slack.com/services/REPLACE/WITH/YOUR-WEBHOOK"
SHEET_ID_PLACEHOLDER = "REPLACE_WITH_GOOGLE_SHEET_ID"
LISTINGS_URL_PLACEHOLDER = "https://api.apify.com/v2/datasets/REPLACE_DATASET_ID/items?token=REPLACE_TOKEN&clean=true"


def node_id(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"arbitrage-agents/{name}"))


def node(name: str, type_: str, version: float | int, params: dict, x: int, y: int, **extra) -> dict:
    out = {"parameters": params, "id": node_id(name), "name": name, "type": type_, "typeVersion": version, "position": [x, y]}
    out.update(extra)
    return out


def code(name: str, js: str, x: int, y: int, **extra) -> dict:
    return node(name, "n8n-nodes-base.code", 2, {"jsCode": js.strip() + "\n"}, x, y, **extra)


def schedule(name: str, cron: str, x: int, y: int) -> dict:
    return node(name, "n8n-nodes-base.scheduleTrigger", 1.2, {"rule": {"interval": [{"field": "cronExpression", "expression": cron}]}}, x, y)


def http_retry() -> dict:
    return {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 2000}


def http_get(name: str, url: str, query: list[tuple[str, str]], x: int, y: int, *, auth: dict | None = None, batch: int | None = None, text: bool = False, **extra) -> dict:
    params: dict = {
        "method": "GET",
        "url": url,
        "sendQuery": bool(query),
        "queryParameters": {"parameters": [{"name": k, "value": v} for k, v in query]},
        "options": {},
    }
    if text:
        params["options"]["response"] = {"response": {"responseFormat": "text"}}
    if batch:
        params["options"]["batching"] = {"batch": {"batchSize": batch, "batchInterval": 1000}}
    n = node(name, "n8n-nodes-base.httpRequest", 4.2, params, x, y, **http_retry(), **extra)
    if auth:
        n["parameters"].update(auth["parameters"])
        n["credentials"] = auth["credentials"]
    return n


def http_post_json(name: str, url: str, json_body_expr: str, x: int, y: int, *, auth: dict | None = None, batch: int | None = None, **extra) -> dict:
    params: dict = {
        "method": "POST",
        "url": url,
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": json_body_expr,
        "options": {},
    }
    if batch:
        params["options"]["batching"] = {"batch": {"batchSize": batch, "batchInterval": 1000}}
    n = node(name, "n8n-nodes-base.httpRequest", 4.2, params, x, y, **http_retry(), **extra)
    if auth:
        n["parameters"].update(auth["parameters"])
        n["credentials"] = auth["credentials"]
    return n


def cred(kind: str, label: str) -> dict:
    """Generic credential reference. kind: httpBasicAuth | httpHeaderAuth | httpQueryAuth."""
    return {
        "parameters": {"authentication": "genericCredentialType", "genericAuthType": kind},
        "credentials": {kind: {"id": "", "name": label}},
    }


def sheets_append(name: str, sheet_name: str, x: int, y: int) -> dict:
    return node(
        name,
        "n8n-nodes-base.googleSheets",
        4.5,
        {
            "operation": "append",
            "documentId": {"__rl": True, "mode": "id", "value": SHEET_ID_PLACEHOLDER},
            "sheetName": {"__rl": True, "mode": "name", "value": sheet_name},
            "columns": {"mappingMode": "autoMapInputData", "value": {}, "matchingColumns": [], "schema": []},
            "options": {},
        },
        x,
        y,
        credentials={"googleSheetsOAuth2Api": {"id": "", "name": "Google Sheets account"}},
    )


def connect(pairs: list[tuple[str, str | list[str]]]) -> dict:
    out: dict = {}
    for src, dst in pairs:
        targets = dst if isinstance(dst, list) else [dst]
        out[src] = {"main": [[{"node": t, "type": "main", "index": 0} for t in targets]]}
    return out


def workflow(name: str, nodes: list[dict], connections: dict, note: str) -> dict:
    return {
        "name": name,
        "nodes": nodes + [node("README", "n8n-nodes-base.stickyNote", 1, {"content": note, "height": 420, "width": 520}, -560, -120)],
        "connections": connections,
        "active": False,
        "settings": {"executionOrder": "v1", "timezone": "UTC"},
        "pinData": {},
        "meta": {"instanceId": "arbitrage-agents"},
        "versionId": node_id(name + "/version"),
        "tags": [],
    }


# ------------------------------------------------------------------ shared JS

JS_OPENAI_BODY_HELPER = r"""
// Build a strict-schema Chat Completions request. Strict mode rejects
// minimum/maximum keywords, so numeric ranges are clamped after parsing.
function openAiBody(model, temperature, maxTokens, system, user, schemaName, properties) {
  return JSON.stringify({
    model, temperature, max_tokens: maxTokens,
    messages: [{ role: 'system', content: system }, { role: 'user', content: user }],
    response_format: { type: 'json_schema', json_schema: { name: schemaName, strict: true, schema: {
      type: 'object', properties, required: Object.keys(properties), additionalProperties: false } } },
  });
}
"""

JS_PARSE_COMPLETION = r"""
function parseCompletion(resp) {
  const choice = resp && resp.choices && resp.choices[0];
  if (!choice) throw new Error('no choices in completion');
  if (choice.message && choice.message.refusal) throw new Error('refusal: ' + choice.message.refusal);
  if (choice.finish_reason === 'length') throw new Error('completion truncated');
  return JSON.parse(choice.message.content);
}
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, Math.round(Number(v) || lo)));
const esc = (s) => String(s ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
const trunc = (s, n) => (s.length <= n ? s : s.slice(0, n - 2) + ' …');
"""

# ------------------------------------------------------------- domain flipper

JS_DOMAIN_PARSE_FILTER = r"""
// Phase 2+3: parse the WhoisFreaks feed (JSON or CSV) and apply the
// zero-cost deterministic filters before anything paid runs.
const MAX_SLD = 20;
const TLDS = new Set(['com', 'ai']);
const TRADEMARKS = ['apple','google','meta','facebook','amazon','microsoft','nike','netflix','openai','anthropic','stripe','paypal','tesla','instagram','youtube','whatsapp','twitter','tiktok','samsung','adobe','disney','walmart'];
const MAX_ENRICH = 400;

const raw = String($input.first().json.data ?? '').trim();
let records = [];
if (raw.startsWith('[') || raw.startsWith('{')) {
  let data = JSON.parse(raw);
  if (!Array.isArray(data)) {
    data = data.domains || data.data || data.results || data.dropped_domains || Object.values(data).find(Array.isArray) || [];
  }
  records = data.map((r) => (typeof r === 'string' ? { domain: r } : r));
} else {
  const lines = raw.split(/\r?\n/).filter((l) => l.trim());
  const header = lines[0].split(/[,;\t|]/).map((h) => h.trim().toLowerCase());
  const idx = header.findIndex((h) => ['domain', 'domain_name', 'domainname', 'name'].includes(h));
  const body = idx >= 0 ? lines.slice(1) : lines;
  records = body.map((l) => {
    const cells = l.split(/[,;\t|]/);
    return { domain: cells[idx >= 0 ? idx : 0], drop_date: cells[header.indexOf('drop_date')] };
  });
}

const seen = new Set();
const out = [];
for (const r of records) {
  const domain = String(r.domain || r.domain_name || r.domainName || r.name || '').trim().toLowerCase().replace(/\.$/, '');
  if (!domain || !domain.includes('.') || seen.has(domain)) continue;
  seen.add(domain);
  const [sld, ...rest] = domain.split('.');
  const tld = rest.join('.');
  if (!TLDS.has(tld)) continue;
  if (sld.startsWith('xn--') || sld.length < 2 || sld.length > MAX_SLD) continue;
  if (sld.includes('-') || /[0-9]/.test(sld) || !/^[a-z]+$/.test(sld)) continue;
  if (TRADEMARKS.some((t) => sld.startsWith(t))) continue;
  out.push({ json: { domain, tld, drop_date: r.drop_date || r.dropDate || null, registrar: r.registrar || null } });
  if (out.length >= MAX_ENRICH) break;
}
return out;
"""

JS_DOMAIN_GATE = r"""
// Phase 4 gate. DataForSEO rank is 0..1000; scale to a 0..100 DR proxy.
const MIN_DR = 10, MIN_REFS = 5, DR_DIVISOR = 10;
const out = [];
for (const item of $input.all()) {
  const resp = item.json;
  const task = resp && resp.tasks && resp.tasks[0];
  if (!task || task.status_code !== 20000) continue;
  const result = (task.result && task.result[0]) || {};
  const rank = Number(result.rank || 0);
  const dr = Math.round((rank / DR_DIVISOR) * 10) / 10;
  const referring_domains = Number(result.referring_domains || 0);
  const total_backlinks = Number(result.backlinks || 0);
  if (dr < MIN_DR || referring_domains < MIN_REFS) continue;
  out.push({ json: { domain: result.target, dr, rank, referring_domains, total_backlinks, spam_score: result.backlinks_spam_score ?? null } });
}
return out;
"""

DOMAIN_SYSTEM_PROMPT = (
    "You are a conservative, veteran domain portfolio investor and aftermarket broker. Evaluate the commercial liquidation value of the provided expired domain.\\n\\n"
    "Evaluation Criteria:\\n1. Brandability: 1-10 (Pronounceable, memorable, passes the radio test, free of awkward syllable combinations, commercial utility).\\n"
    "2. Domain Rating & Link Equity:\\n   - DR >= 30 AND Referring Domains >= 50: Score range 9-10\\n   - DR 20-29 AND Referring Domains >= 20: Score range 7-8\\n"
    "   - DR 10-19 AND Referring Domains >= 5: Score range 5-6\\n   - DR < 10: Score range 1-4\\n"
    "3. Resale Estimate: Provide a realistic wholesale buy-now flip price in USD (typically $300 to $3,500).\\n\\n"
    "Return valid JSON adhering strictly to the required schema. No commentary outside JSON."
)

JS_DOMAIN_OPENAI_BODY = "={{ " + r"""(() => {
  const system = '""" + DOMAIN_SYSTEM_PROMPT + r"""';
  const user = `Evaluate the following metrics:\nDomain: ${$json.domain}\nDomain Rating (DR): ${$json.dr}\nReferring Domains: ${$json.referring_domains}\nTotal Backlinks: ${$json.total_backlinks}`;
  const properties = {
    score: { type: 'integer', description: 'Composite investment grade score from 1 (worthless) to 10 (prime acquisition).' },
    brandability: { type: 'integer', description: 'Brand suitability, memorability and commercial fit, 1 to 10.' },
    suggested_price: { type: 'integer', description: 'Estimated resale liquidation value in whole USD.' },
    reasoning: { type: 'string', description: 'Single-sentence justification synthesising backlink equity and brand phonetic appeal.' },
  };
  return JSON.stringify({
    model: 'gpt-4o-mini', temperature: 0.2, max_tokens: 300,
    messages: [{ role: 'system', content: system }, { role: 'user', content: user }],
    response_format: { type: 'json_schema', json_schema: { name: 'domain_score_evaluation', strict: true,
      schema: { type: 'object', properties, required: Object.keys(properties), additionalProperties: false } } },
  });
})() }}"""

JS_DOMAIN_RANK_DIGEST = JS_PARSE_COMPLETION + r"""
// Phase 6+7: merge scores with metrics (same order as the gate output),
// sort score DESC then suggested_price DESC, take the top 5, build Block Kit.
const TOP_N = 5;
const metrics = $('Authority gate').all().map((i) => i.json);
const scored = [];
$input.all().forEach((item, idx) => {
  const m = metrics[idx];
  if (!m) return;
  let s;
  try { s = parseCompletion(item.json); } catch (e) { return; }
  scored.push({
    ...m,
    score: clamp(s.score, 1, 10),
    brandability: clamp(s.brandability, 1, 10),
    suggested_price: clamp(s.suggested_price, 0, 1000000),
    reasoning: String(s.reasoning || '').slice(0, 500),
  });
});
scored.sort((a, b) => b.score - a.score || b.suggested_price - a.suggested_price);
const top = scored.slice(0, TOP_N);
const date = $today.minus({ days: 1 }).toFormat('yyyy-MM-dd');

const blocks = [
  { type: 'header', text: { type: 'plain_text', text: `🔍 High-Yield Dropped Domains (${date})`, emoji: true } },
  { type: 'divider' },
];
if (!top.length) blocks.push({ type: 'section', text: { type: 'mrkdwn', text: '_No domains cleared the authority gate today._' } });
top.forEach((d, i) => {
  const enc = encodeURIComponent(d.domain);
  const text = `*${i + 1}. ${esc(d.domain)}* — \`Score: ${d.score}/10\` — *Est. Flip: $${d.suggested_price.toLocaleString('en-US')}*\n` +
    `• Metrics: *DR ${d.dr}* | *${d.referring_domains} Ref Domains* | *${d.total_backlinks} Backlinks* | Brandability: *${d.brandability}/10*\n` +
    `• Rationale: _${esc(d.reasoning)}_\n` +
    `• Checkout: <https://www.godaddy.com/domainsearch/find?domainToCheck=${enc}|🛒 Register on GoDaddy> | <https://www.namecheap.com/domains/registration/results/?domain=${enc}|🛒 Register on Namecheap>`;
  blocks.push({ type: 'section', text: { type: 'mrkdwn', text: trunc(text, 3000) } });
});
blocks.push({ type: 'context', elements: [{ type: 'mrkdwn', text: `Funnel · gated: ${metrics.length} → scored: ${scored.length} → shortlisted: ${top.length}` }] });

const run_id = $execution.id;
const rows = scored.map((d) => ({ run_id, date, agent: 'domain_flipper', domain: d.domain, dr: d.dr, referring_domains: d.referring_domains, total_backlinks: d.total_backlinks, score: d.score, brandability: d.brandability, suggested_price: d.suggested_price, shortlisted: top.includes(d), reasoning: d.reasoning }));
return [{ json: { text: `🔍 Domain Flips Daily Shortlist — ${date}`, blocks: blocks.slice(0, 50), rows } }];
"""

JS_ROWS_TO_ITEMS = r"""
// One item per audit row for the Google Sheets append.
return ($input.first().json.rows || []).map((r) => ({ json: r }));
"""

DOMAIN_NOTE = """## Domain Flipper (Agent 1)
Daily 06:00 UTC: WhoisFreaks dropped feed → deterministic filter → DataForSEO backlinks → gate (DR>=10, refs>=5) → OpenAI strict JSON → top 5 → Slack.

After import:
1. Fetch node: attach an HTTP Query Auth credential (name `apiKey`, value = WhoisFreaks key).
2. DataForSEO node: attach HTTP Basic Auth (login/password).
3. OpenAI node: attach HTTP Header Auth (name `Authorization`, value `Bearer sk-...`).
4. Slack node: replace the webhook URL.
5. Sheets node: set the spreadsheet ID + sheet tab, or delete the audit branch.
Thresholds live in the Code nodes (MAX_SLD, TLDS, TRADEMARKS, MIN_DR, MIN_REFS, TOP_N).
Note: DataForSEO `rank` is 0..1000; it is divided by 10 to approximate a 0..100 DR."""


def build_domain_flipper() -> dict:
    nodes = [
        schedule("Daily 06:00 UTC", "0 6 * * *", -300, 200),
        http_get(
            "Fetch dropped domains (WhoisFreaks)",
            "https://files.whoisfreaks.com/v3.1/domains/dropped",
            [("date", "={{ $today.minus({days: 1}).toFormat('yyyy-MM-dd') }}"), ("tlds", "com,ai")],
            -60, 200,
            auth=cred("httpQueryAuth", "WhoisFreaks apiKey"),
            text=True,
        ),
        code("Parse + deterministic filter", JS_DOMAIN_PARSE_FILTER, 180, 200),
        http_post_json(
            "Backlink summary (DataForSEO)",
            "https://api.dataforseo.com/v3/backlinks/summary/live",
            '=[{"target": "{{ $json.domain }}", "internal_list_limit": 1, "backlinks_status_type": "live"}]',
            420, 200,
            auth=cred("httpBasicAuth", "DataForSEO"),
            batch=10,
            onError="continueRegularOutput",
        ),
        code("Authority gate", JS_DOMAIN_GATE, 660, 200),
        http_post_json(
            "Score (OpenAI structured)",
            "https://api.openai.com/v1/chat/completions",
            JS_DOMAIN_OPENAI_BODY,
            900, 200,
            auth=cred("httpHeaderAuth", "OpenAI Bearer"),
            batch=5,
            onError="continueRegularOutput",
        ),
        code("Rank + build digest", JS_DOMAIN_RANK_DIGEST, 1140, 200),
        http_post_json("Post Slack digest", SLACK_WEBHOOK_PLACEHOLDER, "={{ JSON.stringify({ text: $json.text, blocks: $json.blocks }) }}", 1380, 100),
        code("Audit rows", JS_ROWS_TO_ITEMS, 1380, 320),
        sheets_append("Append audit log (Google Sheets)", "domain_flipper", 1620, 320),
    ]
    connections = connect([
        ("Daily 06:00 UTC", "Fetch dropped domains (WhoisFreaks)"),
        ("Fetch dropped domains (WhoisFreaks)", "Parse + deterministic filter"),
        ("Parse + deterministic filter", "Backlink summary (DataForSEO)"),
        ("Backlink summary (DataForSEO)", "Authority gate"),
        ("Authority gate", "Score (OpenAI structured)"),
        ("Score (OpenAI structured)", "Rank + build digest"),
        ("Rank + build digest", ["Post Slack digest", "Audit rows"]),
        ("Audit rows", "Append audit log (Google Sheets)"),
    ])
    return workflow("Agent 1 — Domain Flipper", nodes, connections, DOMAIN_NOTE)


# ---------------------------------------------------------------- saas scout

JS_SAAS_FILTER = r"""
// Phase 3: deterministic listing filters. Input: one item per listing from
// the listings feed (any JSON array of objects with the fields below).
const MAX_PRICE = 10000, MIN_INACTIVE_DAYS = 540, MIN_USERS = 1000;
const BLOCKED = ['crypto', 'gambling', 'adult', 'vpn', 'dating', 'casino', 'betting'];
const num = (v) => (v == null || v === '' ? null : Number(String(v).replace(/[^\d.]/g, '')) || null);
const parseDate = (v) => { const d = new Date(v); return isNaN(d) ? null : d; };
const hostOf = (u) => { try { return new URL(u.includes('//') ? u : 'https://' + u).hostname.replace(/^www\./, ''); } catch { return null; } };

const seen = new Set();
const out = [];
for (const item of $input.all()) {
  const r = item.json;
  const source_url = r.source_url || r.url || '';
  if (seen.has(source_url)) continue;
  seen.add(source_url);
  const asking_price = num(r.asking_price ?? r.price);
  if (asking_price != null && asking_price > MAX_PRICE) continue;
  const updated = parseDate(r.last_updated_date || r.last_updated || r.updated);
  if (!updated) continue;
  if ((Date.now() - updated.getTime()) / 86400000 < MIN_INACTIVE_DAYS) continue;
  const hay = `${r.category || ''} ${r.name || r.title || ''}`.toLowerCase();
  if (BLOCKED.some((b) => hay.includes(b))) continue;
  const claimed_users = num(r.claimed_users ?? r.users);
  if (claimed_users == null || claimed_users < MIN_USERS) continue;
  out.push({ json: {
    asset_id: String(r.asset_id || r.id || source_url), name: r.name || r.title || source_url, category: r.category || 'unknown',
    source_url, asking_price, claimed_users, last_updated_date: updated.toISOString().slice(0, 10),
    domain: r.domain || hostOf(r.website || r.homepage || '') || null, description: String(r.description || '').slice(0, 1500),
    seller_contact_url: r.seller_contact_url || r.contact_url || source_url,
  } });
}
return out;
"""

JS_SAAS_ATTACH_STACK = r"""
// Attach BuiltWith technology names to each listing (same order as the filter output).
const listings = $('Deterministic filter').all().map((i) => i.json);
return $input.all().map((item, idx) => {
  const names = [];
  for (const res of (item.json.Results || [])) for (const p of ((res.Result || {}).Paths || [])) for (const t of (p.Technologies || [])) if (t.Name && !names.includes(t.Name)) names.push(t.Name);
  return { json: { ...listings[idx], tech_stack: names.slice(0, 25) } };
});
"""

JS_SAAS_GATE = r"""
// Phase 4 gate: average Similarweb monthly visits >= 800 OR installs >= 1200.
const MIN_VISITS = 800, MIN_INSTALLS = 1200;
const listings = $('Attach tech stack').all().map((i) => i.json);
const out = [];
$input.all().forEach((item, idx) => {
  const l = listings[idx];
  if (!l) return;
  const pts = (item.json.visits || []).map((p) => Number(p.visits || 0));
  const monthly_visits = pts.length ? Math.round(pts.reduce((a, b) => a + b, 0) / pts.length) : null;
  if ((monthly_visits || 0) < MIN_VISITS && (l.claimed_users || 0) < MIN_INSTALLS) return;
  out.push({ json: { ...l, monthly_visits } });
});
return out;
"""

SAAS_SYSTEM_PROMPT = (
    "You are an expert micro-private equity investor and full-stack AI engineer. You specialize in buying obsolete, abandoned web tools and converting their rigid dashboards into autonomous, agent-native workflows.\\n\\n"
    "Evaluation Objectives:\\n1. Feasibility of Agent-Native Rebuild: Can this tool\\'s manual user workflows (e.g. clicking buttons, copy-pasting text, running manual reports) be completely replaced by an LLM-driven agent (e.g. LangGraph, FastAPI, a Chrome extension calling a hosted model API) in under 16 hours of engineering?\\n"
    "2. Score: 1-10 overall investment grade.\\n3. Estimated Flip Value: Fair market value once refactored with a modern agent UI and active subscription billing.\\n"
    "4. One-Sentence Rebuild Blueprint: Concrete architecture statement (e.g. \"Replace brittle Selenium scraper with crawl4ai + structured LLM parsing\").\\n\\n"
    "Return strictly valid JSON matching the required schema."
)

JS_SAAS_OPENAI_BODY = "={{ " + r"""(() => {
  const system = '""" + SAAS_SYSTEM_PROMPT + r"""';
  const traffic = [$json.monthly_visits ? `${$json.monthly_visits} monthly visits` : null, $json.claimed_users ? `${$json.claimed_users} claimed users/installs` : null].filter(Boolean).join(' / ') || 'unknown';
  const user = `Analyze this neglected asset:\nProduct Name: ${$json.name}\nCategory: ${$json.category}\nCurrent Tech Stack: ${($json.tech_stack || []).join(', ') || 'unknown'}\nMonthly Traffic / Installs: ${traffic}\nLast Updated: ${$json.last_updated_date}\nAsking Price: ${$json.asking_price != null ? '$' + $json.asking_price : 'not listed (outreach target)'}\nProduct Description: ${$json.description || 'n/a'}`;
  const properties = {
    score: { type: 'integer', description: 'Composite acquisition viability score from 1 to 10.' },
    agent_rewrite_potential: { type: 'integer', description: 'How readily manual workflows convert into autonomous LLM functions, 1 to 10.' },
    estimated_flip_value: { type: 'integer', description: 'Projected resale valuation in USD following agent modernisation.' },
    rebuild_architecture_blueprint: { type: 'string', description: 'Precise technical stack prescription to build the replacement agent in one weekend.' },
    reasoning: { type: 'string', description: 'One-sentence investment summary weighing user retention against execution complexity.' },
  };
  return JSON.stringify({
    model: 'gpt-4o', temperature: 0.15, max_tokens: 500,
    messages: [{ role: 'system', content: system }, { role: 'user', content: user }],
    response_format: { type: 'json_schema', json_schema: { name: 'saas_rebuild_evaluation', strict: true,
      schema: { type: 'object', properties, required: Object.keys(properties), additionalProperties: false } } },
  });
})() }}"""

JS_SAAS_RANK_DIGEST = JS_PARSE_COMPLETION + r"""
// Phase 6+7: threshold (score>=7, rewrite potential>=8), sort, top 3, Block Kit.
const MIN_SCORE = 7, MIN_POTENTIAL = 8, TOP_N = 3;
const listings = $('Traffic gate').all().map((i) => i.json);
const scored = [];
$input.all().forEach((item, idx) => {
  const l = listings[idx];
  if (!l) return;
  let s;
  try { s = parseCompletion(item.json); } catch (e) { return; }
  scored.push({
    ...l,
    score: clamp(s.score, 1, 10),
    agent_rewrite_potential: clamp(s.agent_rewrite_potential, 1, 10),
    estimated_flip_value: clamp(s.estimated_flip_value, 0, 10000000),
    rebuild_architecture_blueprint: String(s.rebuild_architecture_blueprint || '').slice(0, 600),
    reasoning: String(s.reasoning || '').slice(0, 500),
  });
});
const eligible = scored.filter((s) => s.score >= MIN_SCORE && s.agent_rewrite_potential >= MIN_POTENTIAL);
eligible.sort((a, b) => b.score - a.score || b.estimated_flip_value - a.estimated_flip_value);
const top = eligible.slice(0, TOP_N);
const date = $today.toFormat('yyyy-MM-dd');

const blocks = [{ type: 'header', text: { type: 'plain_text', text: `🛠️ Dead SaaS Scout: Top ${top.length} Acquisition Opportunities`, emoji: true } }];
if (!top.length) blocks.push({ type: 'section', text: { type: 'mrkdwn', text: '_No listings cleared the score and rewrite-potential thresholds this week._' } });
top.forEach((l, i) => {
  if (i > 0) blocks.push({ type: 'divider' });
  const price = l.asking_price != null ? '$' + l.asking_price.toLocaleString('en-US') : 'not listed';
  const text = `*${i + 1}. ${esc(l.name)}* (${esc(l.category)}) — \`Score: ${l.score}/10\` | *Price: ${price}*\n` +
    `• *Agent Potential*: \`${l.agent_rewrite_potential}/10\` | *Est. Flip*: \`$${l.estimated_flip_value.toLocaleString('en-US')}\`\n` +
    `• *Current Footprint*: ${l.claimed_users ? l.claimed_users.toLocaleString('en-US') + ' users' : 'users n/a'} | ${l.monthly_visits ? l.monthly_visits.toLocaleString('en-US') + ' visits/mo' : 'traffic n/a'} | Last updated: ${esc(l.last_updated_date)}\n` +
    `• *Stack*: ${esc((l.tech_stack || []).join(', ') || 'unknown')}\n` +
    `• *Rebuild Plan*: \`${esc(l.rebuild_architecture_blueprint)}\`\n` +
    `• *Rationale*: ${esc(l.reasoning)}\n` +
    `• *Action*: <${l.seller_contact_url}|📩 Open Deal Room / Message Founder>`;
  blocks.push({ type: 'section', text: { type: 'mrkdwn', text: trunc(text, 3000) } });
});
blocks.push({ type: 'context', elements: [{ type: 'mrkdwn', text: `Funnel · gated: ${listings.length} → scored: ${scored.length} → eligible: ${eligible.length} → shortlisted: ${top.length}` }] });

const run_id = $execution.id;
const rows = scored.map((l) => ({ run_id, date, agent: 'saas_scout', asset_id: l.asset_id, name: l.name, category: l.category, asking_price: l.asking_price, claimed_users: l.claimed_users, monthly_visits: l.monthly_visits, tech_stack: (l.tech_stack || []).join(', '), score: l.score, agent_rewrite_potential: l.agent_rewrite_potential, estimated_flip_value: l.estimated_flip_value, shortlisted: top.includes(l), source_url: l.source_url }));
return [{ json: { text: `🛠️ Dead SaaS Scout — Weekly Modernization Targets (${date})`, blocks: blocks.slice(0, 50), rows } }];
"""

SAAS_NOTE = """## Dead SaaS Scout (Agent 2)
Weekly Monday 07:00 UTC: listings feed → deterministic filter → BuiltWith → Similarweb → gate → OpenAI strict JSON → score>=7 & potential>=8 → top 3 → Slack.

Neither Acquire.com nor the Chrome Web Store has a public API. Point the
"Fetch listings feed" node at any JSON array of listings (an Apify dataset
items URL works well) with fields: name, category, source_url, asking_price,
claimed_users, last_updated_date, domain, description, seller_contact_url.

After import:
1. Fetch node: replace the URL.
2. BuiltWith node: attach HTTP Query Auth (name `KEY`).
3. Similarweb node: attach HTTP Query Auth (name `api_key`).
4. OpenAI node: attach HTTP Header Auth (`Authorization: Bearer sk-...`).
5. Slack webhook URL + Google Sheet ID placeholders.
Thresholds live in the Code nodes."""


def build_saas_scout() -> dict:
    nodes = [
        schedule("Weekly Monday 07:00 UTC", "0 7 * * 1", -300, 200),
        http_get("Fetch listings feed", LISTINGS_URL_PLACEHOLDER, [], -60, 200),
        code("Deterministic filter", JS_SAAS_FILTER, 180, 200),
        http_get(
            "Tech stack (BuiltWith)",
            "https://api.builtwith.com/v21/api.json",
            [("LOOKUP", "={{ $json.domain || 'example.invalid' }}")],
            420, 200,
            auth=cred("httpQueryAuth", "BuiltWith KEY"),
            batch=5,
            onError="continueRegularOutput",
        ),
        code("Attach tech stack", JS_SAAS_ATTACH_STACK, 660, 200),
        http_get(
            "Traffic (Similarweb)",
            "=https://api.similarweb.com/v1/website/{{ $json.domain || 'example.invalid' }}/total-traffic-and-engagement/visits",
            [
                ("start_date", "={{ $today.minus({months: 3}).toFormat('yyyy-MM') }}"),
                ("end_date", "={{ $today.minus({months: 1}).toFormat('yyyy-MM') }}"),
                ("country", "world"),
                ("granularity", "monthly"),
                ("main_domain_only", "false"),
            ],
            900, 200,
            auth=cred("httpQueryAuth", "Similarweb api_key"),
            batch=5,
            onError="continueRegularOutput",
        ),
        code("Traffic gate", JS_SAAS_GATE, 1140, 200),
        http_post_json(
            "Score (OpenAI structured)",
            "https://api.openai.com/v1/chat/completions",
            JS_SAAS_OPENAI_BODY,
            1380, 200,
            auth=cred("httpHeaderAuth", "OpenAI Bearer"),
            batch=5,
            onError="continueRegularOutput",
        ),
        code("Rank + build digest", JS_SAAS_RANK_DIGEST, 1620, 200),
        http_post_json("Post Slack digest", SLACK_WEBHOOK_PLACEHOLDER, "={{ JSON.stringify({ text: $json.text, blocks: $json.blocks }) }}", 1860, 100),
        code("Audit rows", JS_ROWS_TO_ITEMS, 1860, 320),
        sheets_append("Append audit log (Google Sheets)", "saas_scout", 2100, 320),
    ]
    connections = connect([
        ("Weekly Monday 07:00 UTC", "Fetch listings feed"),
        ("Fetch listings feed", "Deterministic filter"),
        ("Deterministic filter", "Tech stack (BuiltWith)"),
        ("Tech stack (BuiltWith)", "Attach tech stack"),
        ("Attach tech stack", "Traffic (Similarweb)"),
        ("Traffic (Similarweb)", "Traffic gate"),
        ("Traffic gate", "Score (OpenAI structured)"),
        ("Score (OpenAI structured)", "Rank + build digest"),
        ("Rank + build digest", ["Post Slack digest", "Audit rows"]),
        ("Audit rows", "Append audit log (Google Sheets)"),
    ])
    return workflow("Agent 2 — Dead SaaS Scout", nodes, connections, SAAS_NOTE)


def main() -> None:
    for filename, builder in (("domain_flipper.workflow.json", build_domain_flipper), ("saas_scout.workflow.json", build_saas_scout)):
        path = OUT_DIR / filename
        path.write_text(json.dumps(builder(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(OUT_DIR.parent)}")


if __name__ == "__main__":
    main()

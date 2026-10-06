from __future__ import annotations

import io
import unittest
from datetime import date
from unittest import mock

from tests.helpers import NoNetworkTestCase

from agents.common.cache import Cache
from agents.common.slack import StdoutSender
from agents.saas_scout import digest, enrich, filters, pipeline, scoring, sources

TODAY = date(2026, 10, 6)


class SourceTests(unittest.TestCase):
    def test_normalise_listing(self):
        raw = {"title": "Tool", "url": "https://app.acquire.com/startup/tool", "price": "$4,500", "users": "3,200", "website": "https://www.tool.example/x"}
        listing = sources.normalise_listing(raw)
        self.assertEqual(listing["name"], "Tool")
        self.assertEqual(listing["asking_price"], 4500.0)
        self.assertEqual(listing["claimed_users"], 3200)
        self.assertEqual(listing["domain"], "tool.example")
        self.assertEqual(listing["seller_contact_url"], raw["url"])
        self.assertIsNone(sources.normalise_listing({"description": "nothing"}))

    def test_parse_acquire_json_ld(self):
        page = """<html><head><title>Fallback</title>
        <script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"InvoiceNudge","description":"Reminders","category":"SaaS","offers":{"@type":"Offer","price":"7800","priceCurrency":"USD"},"url":"https://invoicenudge.example"}</script>
        </head></html>"""
        listing = sources.parse_acquire_listing_html(page, "https://app.acquire.com/startup/invoicenudge")
        self.assertEqual(listing["name"], "InvoiceNudge")
        self.assertEqual(listing["asking_price"], 7800.0)
        self.assertEqual(listing["domain"], "invoicenudge.example")
        self.assertEqual(listing["source"], "acquire")
        fallback = sources.parse_acquire_listing_html("<title>Only Title</title>", "https://app.acquire.com/startup/x")
        self.assertEqual(fallback["name"], "Only Title")

    def test_acquire_sitemap_source_uses_injected_fetch(self):
        pages = {
            sources.ACQUIRE_SITEMAP_URL: "<urlset><url><loc>https://app.acquire.com/startup/a</loc></url><url><loc>https://app.acquire.com/startup/b</loc></url><url><loc>https://app.acquire.com/startup/a</loc></url></urlset>",
            "https://app.acquire.com/startup/a": '<script type="application/ld+json">{"@type":"Product","name":"A","offers":{"price":100}}</script>',
            "https://app.acquire.com/startup/b": "<title>B</title>",
        }
        src = sources.AcquireSitemapSource(max_listings=5, fetch=lambda url: pages[url])
        out = src.fetch()
        self.assertEqual([l["name"] for l in out], ["A", "B"])

    def test_parse_chrome_webstore_html(self):
        page = """<html><head><meta property="og:title" content="AutoTab Summarizer - Chrome Web Store"><meta name="description" content="Summaries &amp; more"></head>
        <body><div>12,000+ users</div><div>Updated</div><div>March 12, 2024</div><div>Version</div><div>2.4.1</div></body></html>"""
        listing = sources.parse_chrome_webstore_html(page, "https://chromewebstore.google.com/detail/autotab/abcdefghijklmnop")
        self.assertEqual(listing["name"], "AutoTab Summarizer")
        self.assertEqual(listing["claimed_users"], 12000)
        self.assertEqual(listing["last_updated_date"], "March 12, 2024")
        self.assertEqual(listing["description"], "Summaries & more")
        self.assertEqual(listing["source"], "chromewebstore")
        self.assertIsNone(listing["asking_price"])
        k_page = "<title>X</title> 3.5K users"
        self.assertEqual(sources.parse_chrome_webstore_html(k_page, "https://chromewebstore.google.com/detail/x/id")["claimed_users"], 3500)


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = filters.SaasFilterConfig()

    def test_parse_date_formats(self):
        self.assertEqual(filters.parse_date("2024-03-12"), date(2024, 3, 12))
        self.assertEqual(filters.parse_date("2024-03-12T00:00:00Z"), date(2024, 3, 12))
        self.assertEqual(filters.parse_date("2024-03-12T10:11:12.123Z"), date(2024, 3, 12))
        self.assertEqual(filters.parse_date("March 3, 2024"), date(2024, 3, 3))
        self.assertIsNone(filters.parse_date("yesterday"))
        self.assertIsNone(filters.parse_date(None))

    def test_rules(self):
        base = {"asset_id": "1", "name": "Tool", "category": "Productivity", "asking_price": 4000, "claimed_users": 2000, "last_updated_date": "2024-01-01"}
        self.assertIsNone(filters.check_listing(base, self.cfg, TODAY))
        self.assertEqual(filters.check_listing({**base, "asking_price": 20000}, self.cfg, TODAY), "price:20000")
        self.assertTrue(filters.check_listing({**base, "last_updated_date": "2026-08-01"}, self.cfg, TODAY).startswith("too-recent"))
        self.assertEqual(filters.check_listing({**base, "last_updated_date": None}, self.cfg, TODAY), "no-last-updated")
        self.assertEqual(filters.check_listing({**base, "category": "Crypto"}, self.cfg, TODAY), "category:crypto")
        self.assertEqual(filters.check_listing({**base, "claimed_users": 10}, self.cfg, TODAY), "users:10")
        self.assertIsNone(filters.check_listing({**base, "asking_price": None}, self.cfg, TODAY))
        self.assertEqual(filters.check_listing({**base, "asking_price": None}, filters.SaasFilterConfig(require_price=True), TODAY), "no-price")

    def test_apply_filters_dedupes(self):
        a = {"asset_id": "1", "name": "A", "category": "x", "asking_price": 100, "claimed_users": 5000, "last_updated_date": "2023-01-01", "source_url": "u"}
        kept, rejected = filters.apply_filters([a, dict(a)], self.cfg, TODAY)
        self.assertEqual(len(kept), 1)
        self.assertEqual(rejected[0][1], "duplicate")


class EnrichTests(unittest.TestCase):
    def test_parse_builtwith(self):
        payload = {"Results": [{"Result": {"Paths": [{"Technologies": [{"Name": "jQuery"}, {"Name": "PHP"}]}, {"Technologies": [{"Name": "jQuery"}]}]}}]}
        self.assertEqual(enrich.parse_builtwith(payload), ["jQuery", "PHP"])
        self.assertEqual(enrich.parse_builtwith({}), [])

    def test_last_full_months(self):
        self.assertEqual(enrich.last_full_months(date(2026, 10, 6)), ("2026-07", "2026-09"))
        self.assertEqual(enrich.last_full_months(date(2026, 1, 15)), ("2025-10", "2025-12"))
        self.assertEqual(enrich.last_full_months(date(2026, 2, 1), count=1), ("2026-01", "2026-01"))

    def test_parse_similarweb_and_gate(self):
        payload = {"visits": [{"date": "2026-07-01", "visits": 1000.0}, {"date": "2026-08-01", "visits": 2000.0}]}
        self.assertEqual(enrich.parse_similarweb(payload), 1500)
        self.assertIsNone(enrich.parse_similarweb({"visits": []}))
        gate = enrich.TrafficGate()
        self.assertTrue(enrich.passes_traffic_gate({"monthly_visits": 900, "claimed_users": 0}, gate))
        self.assertTrue(enrich.passes_traffic_gate({"monthly_visits": None, "claimed_users": 1300}, gate))
        self.assertFalse(enrich.passes_traffic_gate({"monthly_visits": 100, "claimed_users": 100}, gate))

    def test_fetch_visits_request_shape_and_cache(self):
        calls = []

        def fake_get(url, **kwargs):
            calls.append((url, kwargs["params"]))
            return {"visits": [{"visits": 900}]}

        cache = Cache(":memory:")
        with mock.patch.object(enrich.http, "get_json", fake_get):
            v1 = enrich.fetch_monthly_visits("tool.example", "KEY", today=TODAY, cache=cache)
            v2 = enrich.fetch_monthly_visits("tool.example", "KEY", today=TODAY, cache=cache)
        cache.close()
        self.assertEqual((v1, v2), (900, 900))
        self.assertEqual(len(calls), 1)
        url, params = calls[0]
        self.assertIn("/website/tool.example/total-traffic-and-engagement/visits", url)
        self.assertEqual(params["start_date"], "2026-07")
        self.assertEqual(params["end_date"], "2026-09")
        self.assertEqual(params["granularity"], "monthly")


class ScoringTests(unittest.TestCase):
    def test_heuristic_ranges(self):
        out = scoring.heuristic_score({"asset_id": "1", "claimed_users": 5000, "monthly_visits": 4000, "asking_price": 9500, "tech_stack": ["Ruby on Rails"]})
        self.assertTrue(1 <= out["score"] <= 10)
        self.assertTrue(1 <= out["agent_rewrite_potential"] <= 10)
        self.assertGreaterEqual(out["estimated_flip_value"], 2000)

    def test_score_listing_prompt_and_clamp(self):
        fake = mock.Mock()
        fake.structured.return_value = {"score": "9", "agent_rewrite_potential": 11, "estimated_flip_value": 25000, "rebuild_architecture_blueprint": "FastAPI + agent", "reasoning": "fine"}
        listing = {"name": "T", "category": "c", "tech_stack": ["PHP"], "monthly_visits": 1200, "claimed_users": 3000, "last_updated_date": "2024-01-01", "asking_price": 4500, "description": "d"}
        out = scoring.score_listing(listing, fake)
        self.assertEqual(out["score"], 9)
        self.assertEqual(out["agent_rewrite_potential"], 10)
        user = fake.structured.call_args.kwargs["user"]
        self.assertIn("1,200 monthly visits / 3,000 claimed users/installs", user)
        self.assertIn("Asking Price: $4,500", user)


class DigestAndPipelineTests(NoNetworkTestCase):
    def test_digest_blocks(self):
        item = {"name": "A & B", "category": "x", "score": 8, "asking_price": 4500, "agent_rewrite_potential": 9, "estimated_flip_value": 20000, "claimed_users": 3200, "monthly_visits": 1900, "last_updated_date": "2024-03-12", "tech_stack": ["PHP"], "rebuild_architecture_blueprint": "plan", "reasoning": "r", "seller_contact_url": "https://acquire.com/listing/1"}
        text, blocks = digest.build_digest([item, item], "2026-10-06")
        self.assertIn("2026-10-06", text)
        self.assertEqual([b["type"] for b in blocks], ["header", "section", "divider", "section"])
        self.assertIn("A &amp; B", blocks[1]["text"]["text"])
        self.assertIn("<https://acquire.com/listing/1|", blocks[1]["text"]["text"])

    def test_dry_run_end_to_end(self):
        sender = StdoutSender(io.StringIO())
        result = pipeline.run(pipeline.SaasScoutConfig(), dry_run=True, today=TODAY, sender=sender, log=lambda *a, **k: None)
        self.assertEqual(result.funnel["fetched"], 9)
        self.assertEqual(result.funnel["after_filters"], 5)
        self.assertEqual(result.funnel["shortlisted"], 2)
        self.assertEqual(result.shortlist[0]["name"], "SEO Snapshot")
        self.assertTrue(all(i["score"] >= 7 and i["agent_rewrite_potential"] >= 8 for i in result.shortlist))
        self.assertNotIn("_fixture_enrichment", result.shortlist[0])

    def test_live_mode_with_mocked_services(self):
        conf = pipeline.SaasScoutConfig(source_names=("file",), listings_file=str(pipeline.FIXTURE_PATH), builtwith_api_key="bw", similarweb_api_key="sw", openai_api_key="sk", cache_path=":memory:", audit_path=None)
        with mock.patch.object(pipeline.enrich, "fetch_tech_stack", return_value=["PHP"]), \
             mock.patch.object(pipeline.enrich, "fetch_monthly_visits", return_value=5000), \
             mock.patch.object(pipeline.scoring, "score_listing", return_value={"score": 8, "agent_rewrite_potential": 9, "estimated_flip_value": 15000, "rebuild_architecture_blueprint": "b", "reasoning": "r"}):
            result = pipeline.run(conf, today=TODAY, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)
        self.assertEqual(result.funnel["after_filters"], 5)
        self.assertEqual(result.funnel["shortlisted"], 3)
        self.assertTrue(all(i["tech_stack"] == ["PHP"] for i in result.shortlist))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import gzip
import io
import json
import unittest
import zipfile
from unittest import mock

from tests.helpers import NoNetworkTestCase

from agents.common.cache import Cache
from agents.common.slack import StdoutSender
from agents.domain_flipper import digest, enrich, filters, pipeline, scoring, sources


class SourceParsingTests(unittest.TestCase):
    def test_json_array_and_wrapped_object(self):
        body = json.dumps([{"domain": "Foo.COM.", "drop_date": "2026-10-05"}, "bar.ai", {"nope": 1}]).encode()
        records = sources.parse_dropped_payload(body)
        self.assertEqual([r["domain"] for r in records], ["foo.com", "bar.ai"])
        self.assertEqual(records[0]["tld"], "com")
        wrapped = json.dumps({"status": "ok", "domains": [{"domainName": "x.com"}]}).encode()
        self.assertEqual(sources.parse_dropped_payload(wrapped)[0]["domain"], "x.com")

    def test_csv_with_header_and_without(self):
        with_header = b"domain_name,drop_date,registrar\nalpha.com,2026-10-05,GoDaddy\nbeta.ai,2026-10-05,Dynadot\n"
        records = sources.parse_dropped_payload(with_header)
        self.assertEqual([(r["domain"], r["registrar"]) for r in records], [("alpha.com", "GoDaddy"), ("beta.ai", "Dynadot")])
        headerless = b"gamma.com\ndelta.ai\n"
        self.assertEqual([r["domain"] for r in sources.parse_dropped_payload(headerless)], ["gamma.com", "delta.ai"])

    def test_gzip_and_zip_payloads(self):
        raw = b'[{"domain": "zed.com"}]'
        self.assertEqual(sources.parse_dropped_payload(gzip.compress(raw))[0]["domain"], "zed.com")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("dropped.json", raw)
        self.assertEqual(sources.parse_dropped_payload(buf.getvalue())[0]["domain"], "zed.com")
        self.assertEqual(sources.parse_dropped_payload(b"  "), [])

    def test_fetch_builds_expected_request(self):
        captured = {}

        def fake_request(method, url, **kwargs):
            captured.update(method=method, url=url, params=kwargs["params"])
            from agents.common.http import Response
            return Response(200, {}, b'[{"domain": "a.com"}]', url)

        with mock.patch.object(sources.http, "request", fake_request):
            out = sources.fetch_dropped_domains("KEY", date="2026-10-05", tlds=("com", "ai"))
        self.assertEqual(captured["url"], sources.WHOISFREAKS_DROPPED_URL)
        self.assertEqual(captured["params"], {"apiKey": "KEY", "date": "2026-10-05", "tlds": "com,ai"})
        self.assertEqual(out[0]["domain"], "a.com")


    def test_fetch_auth_failure_is_actionable(self):
        from agents.common.http import HttpError

        def fake_request(method, url, **kwargs):
            raise HttpError("HTTP 401 for x", status=401, url=url, body=b'{"error":"invalid api key"}')

        with mock.patch.object(sources.http, "request", fake_request):
            with self.assertRaises(sources.SourceError) as ctx:
                sources.fetch_dropped_domains("bad", date="2026-10-05")
        message = str(ctx.exception)
        self.assertIn("WHOISFREAKS_API_KEY", message)
        self.assertIn("Domainer package", message)
        self.assertIn("invalid api key", message)

    def test_describe_error_covers_other_statuses(self):
        from agents.common.http import HttpError
        self.assertIn("earlier day", sources.describe_whoisfreaks_error(HttpError("x", status=404)))
        self.assertIn("HTTP 500", sources.describe_whoisfreaks_error(HttpError("x", status=500)))


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.cfg = filters.FilterConfig()

    def test_rules(self):
        cases = {
            "brightloom.com": None,
            "quillset.ai": None,
            "my-best.com": "hyphen",
            "shop24.com": "digit",
            "googleanalyticsguide.com": "trademark:google",
            "thisisaverylongdomainnameindeed.com": "length:31",
            "xn--80ak6aa92e.com": "idn",
            "sunny.net": "tld:net",
            "a.com": "too-short",
            "café.com": "non-ascii",
            "nodots": "malformed",
        }
        for domain, expected in cases.items():
            self.assertEqual(filters.check_domain(domain, self.cfg), expected, domain)

    def test_apply_filters_dedupes_and_reports(self):
        records = [{"domain": "a-b.com"}, {"domain": "good.com"}, {"domain": "good.com"}]
        kept, rejected = filters.apply_filters(records, self.cfg)
        self.assertEqual([r["domain"] for r in kept], ["good.com"])
        self.assertEqual([reason for _, reason in rejected], ["hyphen", "duplicate"])

    def test_extra_blocklist_and_overrides(self):
        cfg = filters.FilterConfig(allow_hyphens=True, allow_digits=True, extra_blocklist=("porn",), allowed_tlds=("io",))
        self.assertIsNone(filters.check_domain("web-3.io", cfg))
        self.assertEqual(filters.check_domain("bestporn.io", cfg), "blocklist:porn")


class EnrichTests(unittest.TestCase):
    def test_parse_summary_scales_rank(self):
        payload = {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"target": "x.com", "rank": 321, "backlinks": 900, "referring_domains": 64, "backlinks_spam_score": 3}]}]}
        metrics = enrich.parse_summary(payload)
        self.assertEqual(metrics["dr"], 32.1)
        self.assertEqual(metrics["referring_domains"], 64)
        self.assertEqual(metrics["total_backlinks"], 900)
        self.assertTrue(enrich.passes_authority_gate(metrics, enrich.AuthorityGate()))
        self.assertFalse(enrich.passes_authority_gate({"dr": 9.9, "referring_domains": 100}, enrich.AuthorityGate()))

    def test_parse_summary_errors_and_empty(self):
        with self.assertRaises(enrich.EnrichmentError):
            enrich.parse_summary({"status_code": 40101, "status_message": "auth"})
        with self.assertRaises(enrich.EnrichmentError):
            enrich.parse_summary({"status_code": 20000, "tasks": [{"status_code": 40400, "status_message": "x"}]})
        empty = enrich.parse_summary({"status_code": 20000, "tasks": [{"status_code": 20000, "result": None}]})
        self.assertEqual(empty["dr"], 0)

    def test_basic_auth_header(self):
        self.assertEqual(enrich.basic_auth_header("a", "b"), "Basic YTpi")
        self.assertEqual(enrich.basic_auth_header(None, None, "YTpi"), "Basic YTpi")
        self.assertEqual(enrich.basic_auth_header(None, None, "Basic YTpi"), "Basic YTpi")
        with self.assertRaises(enrich.EnrichmentError):
            enrich.basic_auth_header(None, None)

    def test_fetch_uses_cache(self):
        calls = []

        def fake_post(url, body, **kwargs):
            calls.append(body)
            return {"status_code": 20000, "tasks": [{"status_code": 20000, "result": [{"target": body[0]["target"], "rank": 150, "referring_domains": 12, "backlinks": 50}]}]}

        cache = Cache(":memory:")
        with mock.patch.object(enrich.http, "post_json", fake_post):
            first = enrich.fetch_backlink_summary("x.com", "Basic abc", cache=cache)
            second = enrich.fetch_backlink_summary("x.com", "Basic abc", cache=cache)
        cache.close()
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0]["target"], "x.com")

    def test_synthetic_metrics_deterministic(self):
        self.assertEqual(enrich.synthetic_metrics("a.com"), enrich.synthetic_metrics("a.com"))


class ScoringTests(unittest.TestCase):
    def test_heuristic_ranges_and_rubric(self):
        strong = scoring.heuristic_score({"domain": "lumen.com", "tld": "com", "dr": 41, "referring_domains": 120})
        weak = scoring.heuristic_score({"domain": "verylongunpronounceablexyz.com", "tld": "com", "dr": 5, "referring_domains": 1})
        for result in (strong, weak):
            self.assertTrue(1 <= result["score"] <= 10)
            self.assertTrue(1 <= result["brandability"] <= 10)
            self.assertGreaterEqual(result["suggested_price"], 300)
        self.assertGreater(strong["score"], weak["score"])

    def test_score_domain_clamps_llm_output(self):
        fake = mock.Mock()
        fake.structured.return_value = {"score": 14, "brandability": -2, "suggested_price": "1200", "reasoning": "x" * 800}
        out = scoring.score_domain({"domain": "a.com", "dr": 20, "referring_domains": 30, "total_backlinks": 100}, fake)
        self.assertEqual(out, {"score": 10, "brandability": 1, "suggested_price": 1200, "reasoning": "x" * 500})
        kwargs = fake.structured.call_args.kwargs
        self.assertIn("Domain: a.com", kwargs["user"])
        self.assertEqual(kwargs["schema_name"], scoring.SCHEMA_NAME)


class DigestTests(unittest.TestCase):
    def test_links_and_blocks(self):
        item = {"domain": "neuro.ai", "score": 9, "suggested_price": 3200, "dr": 32.0, "referring_domains": 64, "total_backlinks": 910, "brandability": 8, "reasoning": "a <b> & c"}
        text, blocks = digest.build_digest([item], "2026-10-05", funnel={"fetched": 10, "shortlisted": 1})
        self.assertIn("2026-10-05", text)
        self.assertEqual(blocks[0]["type"], "header")
        body = blocks[2]["text"]["text"]
        self.assertIn("domainToCheck=neuro.ai", body)
        self.assertIn("namecheap.com", body)
        self.assertIn("a &lt;b&gt; &amp; c", body)
        self.assertEqual(blocks[-1]["type"], "context")
        _, empty_blocks = digest.build_digest([], "2026-10-05")
        self.assertIn("No domains", empty_blocks[2]["text"]["text"])


class PipelineTests(NoNetworkTestCase):
    def test_dry_run_end_to_end_without_network(self):
        sender = StdoutSender(io.StringIO())
        conf = pipeline.DomainFlipperConfig()
        result = pipeline.run(conf, dry_run=True, sender=sender, log=lambda *a, **k: None)
        self.assertEqual(result.funnel["fetched"], 24)
        self.assertEqual(result.funnel["after_filters"], 15)
        self.assertEqual(result.funnel["after_authority_gate"], 10)
        self.assertEqual(result.funnel["shortlisted"], 5)
        scores = [i["score"] for i in result.shortlist]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertEqual(result.shortlist[0]["domain"], "lumenpath.com")
        self.assertEqual(len(sender.sent), 1)
        self.assertNotIn("_fixture_metrics", result.shortlist[0])

    def test_live_mode_without_key_exits(self):
        conf = pipeline.DomainFlipperConfig(whoisfreaks_api_key=None)
        with self.assertRaises(SystemExit):
            pipeline.run(conf, dry_run=False, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)

    def test_live_mode_feed_failure_exits_cleanly(self):
        conf = pipeline.DomainFlipperConfig(whoisfreaks_api_key="wf", cache_path=":memory:", audit_path=None)
        with mock.patch.object(pipeline.sources, "fetch_dropped_domains", side_effect=sources.SourceError("boom")):
            with self.assertRaises(SystemExit) as ctx:
                pipeline.run(conf, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)
        self.assertIn("boom", str(ctx.exception))

    def test_live_mode_with_mocked_services(self):
        conf = pipeline.DomainFlipperConfig(
            whoisfreaks_api_key="wf", dataforseo_auth="Basic x", openai_api_key="sk", cache_path=":memory:", audit_path=None, top_n=2,
        )
        feed = [{"domain": "lumenpath.com"}, {"domain": "weak.com"}, {"domain": "bad-name.com"}]
        metrics = {"lumenpath.com": (400, 100, 900), "weak.com": (50, 2, 5)}

        def fake_fetch(api_key, **kw):
            return [sources.normalise_record(r) for r in feed]

        def fake_summary(domain, auth, **kw):
            rank, refs, links = metrics[domain]
            return {"rank": rank, "dr": rank / 10, "referring_domains": refs, "total_backlinks": links, "spam_score": 0, "first_seen": None}

        def fake_score(record, llm):
            return {"score": 9, "brandability": 8, "suggested_price": 2500, "reasoning": "ok"}

        sender = StdoutSender(io.StringIO())
        with mock.patch.object(pipeline.sources, "fetch_dropped_domains", fake_fetch), \
             mock.patch.object(pipeline.enrich, "fetch_backlink_summary", fake_summary), \
             mock.patch.object(pipeline.scoring, "score_domain", fake_score):
            result = pipeline.run(conf, drop_date="2026-10-05", sender=sender, log=lambda *a, **k: None)
        self.assertEqual(result.funnel, {"fetched": 3, "after_filters": 2, "after_authority_gate": 1, "scored": 1, "shortlisted": 1})
        self.assertEqual(result.shortlist[0]["domain"], "lumenpath.com")
        self.assertEqual(result.shortlist[0]["score"], 9)


if __name__ == "__main__":
    unittest.main()

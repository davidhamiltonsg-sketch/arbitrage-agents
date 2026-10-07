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


    def test_free_feed_parses_headerless_csv_and_falls_back_to_latest(self):
        from agents.common.http import HttpError, Response

        calls = []

        def fake_request(method, url, **kwargs):
            calls.append(url)
            if url.endswith("2026-10-05-free-dropped-domains.csv"):
                raise HttpError("HTTP 404", status=404, url=url)
            return Response(200, {}, b"alpha.com\nbeta.xyz\nGamma.AI\n", url)

        with mock.patch.object(sources.http, "request", fake_request):
            records = sources.fetch_free_dropped_domains(date="2026-10-05")
        self.assertEqual([r["domain"] for r in records], ["alpha.com", "beta.xyz", "gamma.ai"])
        self.assertTrue(all(r["source"] == "whoisfreaks-free" for r in records))
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1].endswith("0-latest-free-dropped-domains.csv"))

    def test_free_feed_other_errors_are_source_errors(self):
        from agents.common.http import HttpError

        with mock.patch.object(sources.http, "request", side_effect=HttpError("HTTP 500", status=500)):
            with self.assertRaises(sources.SourceError):
                sources.fetch_free_dropped_domains()


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

    def test_parse_openpagerank_and_batch_fetch(self):
        payload = {"status_code": 200, "response": [
            {"status_code": 200, "error": "", "page_rank_integer": 5, "page_rank_decimal": 5.23, "rank": "12345", "domain": "Lumen.com"},
            {"status_code": 404, "error": "Domain not found", "page_rank_integer": 0, "page_rank_decimal": 0, "rank": None, "domain": "nobody.com"},
        ]}
        parsed = enrich.parse_openpagerank(payload)
        self.assertEqual(parsed["lumen.com"]["dr"], 52.3)
        self.assertEqual(parsed["lumen.com"]["rank"], 12345)
        self.assertIsNone(parsed["lumen.com"]["referring_domains"])
        self.assertEqual(parsed["nobody.com"]["dr"], 0.0)
        with self.assertRaises(enrich.EnrichmentError):
            enrich.parse_openpagerank({"status_code": 401, "error": "bad key"})

        seen = []

        def fake_get(url, **kwargs):
            seen.append((kwargs["params"]["domains[]"], kwargs["headers"]["API-OPR"]))
            return {"status_code": 200, "response": [{"status_code": 200, "page_rank_decimal": 1.5, "rank": "9", "domain": d} for d in kwargs["params"]["domains[]"]]}

        cache = Cache(":memory:")
        domains = [f"d{i}.com" for i in range(150)]
        with mock.patch.object(enrich.http, "get_json", fake_get):
            first = enrich.fetch_openpagerank(domains, "KEY", cache=cache, batch_size=100)
            second = enrich.fetch_openpagerank(domains[:5], "KEY", cache=cache, batch_size=100)
        cache.close()
        self.assertEqual(len(first), 150)
        self.assertEqual([len(c[0]) for c in seen], [100, 50])
        self.assertEqual(seen[0][1], "KEY")
        self.assertEqual(second["d0.com"]["dr"], 15.0)

    def test_gate_is_dr_only_without_link_counts(self):
        gate = enrich.AuthorityGate(min_dr=10, min_referring_domains=5)
        self.assertTrue(enrich.passes_authority_gate({"dr": 12.0, "referring_domains": None}, gate))
        self.assertFalse(enrich.passes_authority_gate({"dr": 9.0, "referring_domains": None}, gate))

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

    def test_heuristic_handles_missing_authority(self):
        none = scoring.heuristic_score({"domain": "lumen.com", "tld": "com", "dr": None, "referring_domains": None})
        opr = scoring.heuristic_score({"domain": "lumen.com", "tld": "com", "dr": 45.0, "referring_domains": None})
        self.assertIn("no authority data", none["reasoning"])
        self.assertIn("no link counts", opr["reasoning"])
        self.assertGreater(opr["score"], none["score"])

    def test_score_domain_clamps_llm_output(self):
        fake = mock.Mock()
        fake.structured.return_value = {"score": 14, "brandability": -2, "suggested_price": "1200", "reasoning": "x" * 800}
        out = scoring.score_domain({"domain": "a.com", "dr": 20, "referring_domains": 30, "total_backlinks": 100}, fake)
        self.assertEqual(out, {"score": 10, "brandability": 1, "suggested_price": 1200, "reasoning": "x" * 500})
        kwargs = fake.structured.call_args.kwargs
        self.assertIn("Domain: a.com", kwargs["user"])
        self.assertEqual(kwargs["schema_name"], scoring.SCHEMA_NAME)


class DiligenceTests(unittest.TestCase):
    def test_cdx_summary(self):
        from agents.domain_flipper import diligence
        payload = [["timestamp", "statuscode", "mimetype"], ["20110305000000", "200", "text/html"], ["20150101000000", "200", "text/html"], ["20230707000000", "200", "text/html"]]
        w = diligence.wayback_summary("x.com", fetch=lambda d: payload)
        self.assertEqual((w["status"], w["first_year"], w["last_year"], w["snapshot_months"], w["years_active"]), ("ok", 2011, 2023, 3, 3))
        self.assertTrue(w["latest_url"].startswith("https://web.archive.org/web/20230707000000/"))
        self.assertEqual(diligence.wayback_summary("x.com", fetch=lambda d: [])["status"], "none")
        err = diligence.wayback_summary("x.com", fetch=lambda d: (_ for _ in ()).throw(RuntimeError("down")))
        self.assertEqual(err["status"], "error")
        self.assertIn("archived 2011–2023", diligence.describe_wayback(w))

    def test_trademark_screen(self):
        from agents.domain_flipper import diligence
        self.assertEqual(diligence.trademark_screen("lumenpath.com")["risk"], "low")
        mid = diligence.trademark_screen("bestnikeshoes.com")
        self.assertEqual(mid["risk"], "medium")
        self.assertIn("nike", mid["flags"])
        self.assertIn("query=bestnikeshoes", mid["links"]["uspto"])
        self.assertEqual(diligence.trademark_screen("nikeoutlet.com")["risk"], "high")
        self.assertEqual(diligence.trademark_screen("bestpixarmovies.com")["risk"], "high")
        self.assertEqual(diligence.trademark_screen("climbing.com")["risk"], "medium")
        self.assertEqual(diligence.trademark_screen("stamped.com")["risk"], "low")  # "amd" is too short to count mid-word
        self.assertEqual(diligence.trademark_screen("hp.com")["risk"], "high")

    def test_export_and_dry_run_diligence(self):
        sender = StdoutSender(io.StringIO())
        result = pipeline.run(pipeline.DomainFlipperConfig(), dry_run=True, sender=sender, log=lambda *a, **k: None)
        record = result.export()
        self.assertEqual(record["mode"], "dry-run")
        self.assertEqual(len(record["shortlist"]), 5)
        first = record["shortlist"][0]
        self.assertIn("wayback", first)
        self.assertIn("trademark", first)
        self.assertIn("godaddy", first["links"])
        self.assertFalse(any(k.startswith("_") for k in first))
        import json
        json.dumps(record)
        self.assertIn("History:", sender.sent[0]["blocks"][2]["text"]["text"])
        self.assertIn("Trademark:", sender.sent[0]["blocks"][2]["text"]["text"])


class AvailabilityTests(unittest.TestCase):
    def test_bootstrap_parsing_prefers_https_and_keeps_known_bases(self):
        from agents.domain_flipper import availability
        boot = {"services": [[["ai"], ["http://plain.example/", "https://rdap.nic.ai/"]], [["xyz"], ["https://rdap.example/xyz"]], ["bad"]]}
        bases = availability.rdap_bases(boot)
        self.assertEqual(bases["ai"], "https://rdap.nic.ai/")
        self.assertEqual(bases["xyz"], "https://rdap.example/xyz/")
        with mock.patch.object(availability.http, "get_json", return_value=boot):
            merged = availability.load_rdap_bases(None)
        self.assertEqual(merged["com"], "https://rdap.verisign.com/com/v1/")
        self.assertEqual(merged["ai"], "https://rdap.nic.ai/")
        with mock.patch.object(availability.http, "get_json", side_effect=availability.http.HttpError("down", status=503)):
            self.assertEqual(availability.load_rdap_bases(None), availability.KNOWN_RDAP_BASES)

    def test_404_means_available_and_200_is_parsed(self):
        from agents.domain_flipper import availability
        seen = []

        def not_found(url):
            seen.append(url)
            raise availability.http.HttpError("HTTP 404", status=404)

        free = availability.check_availability("lumenpath.com", fetch=not_found)
        self.assertEqual(free["status"], "available")
        self.assertEqual(seen, ["https://rdap.verisign.com/com/v1/domain/lumenpath.com"])
        registered = {
            "status": ["client delete prohibited", "client transfer prohibited"],
            "events": [{"eventAction": "registration", "eventDate": "2026-10-06T04:01:00Z"}, {"eventAction": "expiration", "eventDate": "2027-10-06T04:01:00Z"}],
            "entities": [{"roles": ["registrar"], "vcardArray": ["vcard", [["version", {}, "text", "4.0"], ["fn", {}, "text", "Drop Catcher LLC"]]]}],
        }
        taken = availability.check_availability("lumenpath.com", fetch=lambda url: registered)
        self.assertEqual(taken["status"], "taken")
        self.assertEqual(taken["registrar"], "Drop Catcher LLC")
        self.assertEqual(taken["registered"], "2026-10-06T04:01:00Z")
        self.assertIn("Drop Catcher", availability.describe(taken))
        pending = availability.check_availability("lumenpath.com", fetch=lambda url: {"status": ["pending delete"]})
        self.assertEqual(pending["status"], "pending-delete")
        self.assertIn("backorder", availability.describe(pending))

    def test_errors_and_unknown_tlds_never_raise(self):
        from agents.domain_flipper import availability
        flaky = availability.check_availability("lumenpath.com", fetch=lambda url: (_ for _ in ()).throw(availability.http.HttpError("HTTP 429", status=429)))
        self.assertEqual(flaky["status"], "unknown")
        self.assertIn("429", flaky["note"])
        self.assertEqual(availability.check_availability("name.zz", bases={})["status"], "unknown")
        self.assertEqual(availability.check_availability("x.com", fetch=lambda url: "garbage")["status"], "unknown")
        self.assertEqual(availability.sample_availability("x.com")["status"], "sample")


class AppraisalTests(unittest.TestCase):
    def test_parse_and_describe(self):
        from agents.domain_flipper import diligence
        payload = {"domain": "lumenpath.com", "govalue": 1234.4, "comparable_sales": [{"domain": "lumen.io", "price": "900", "year": 2023}, {"nodomain": 1}],
                   "reasons": [{"type": "COMPS", "description": "Similar names sold recently"}, "short"]}
        parsed = diligence.parse_appraisal(payload)
        self.assertEqual(parsed["value"], 1234)
        self.assertEqual(parsed["comparables"], [{"domain": "lumen.io", "price": 900, "year": 2023}])
        self.assertEqual(parsed["reasons"], ["Similar names sold recently", "short"])
        text = diligence.describe_appraisal({**parsed, "status": "ok"})
        self.assertIn("$1,234", text)
        self.assertIn("lumen.io $900 (2023)", text)
        self.assertEqual(diligence.parse_appraisal({"govalue": "n/a"})["status"], "none")

    def test_appraise_handles_auth_and_caches(self):
        from agents.domain_flipper import diligence
        self.assertIsNone(diligence.godaddy_auth_header("k", None))
        self.assertEqual(diligence.godaddy_auth_header(" k ", "s"), "sso-key k:s")
        calls = []

        def ok(url):
            calls.append(url)
            return {"govalue": 500, "comparable_sales": []}

        cache = Cache(":memory:")
        first = diligence.appraise("lumenpath.com", "sso-key k:s", cache=cache, fetch=ok)
        second = diligence.appraise("lumenpath.com", "sso-key k:s", cache=cache, fetch=ok)
        self.assertEqual((first["status"], first["value"], second["value"]), ("ok", 500, 500))
        self.assertEqual(calls, ["https://api.godaddy.com/v1/appraisal/lumenpath.com"])

        def denied(url):
            raise diligence.http.HttpError("HTTP 403", status=403, body=b'{"code":"ACCESS_DENIED","message":"Authenticated user is not allowed access"}')

        refused = diligence.appraise("x.com", "sso-key k:s", fetch=denied)
        self.assertEqual(refused["status"], "denied")
        self.assertIn("ACCESS_DENIED", refused["error"])
        self.assertIn("HTTP 403", refused["error"])

        def missing(url):
            raise diligence.http.HttpError("HTTP 404", status=404)

        self.assertEqual(diligence.appraise("x.com", "sso-key k:s", fetch=missing)["status"], "none")
        self.assertEqual(diligence.appraise("x.com", "sso-key k:s", fetch=lambda url: "nope")["status"], "error")
        self.assertIn("GODADDY_API_KEY", diligence.describe_appraisal({"status": "unconfigured"}))


class DigestTests(unittest.TestCase):
    def test_availability_and_appraisal_lines(self):
        item = {"domain": "lumenpath.com", "dr": 25.0, "referring_domains": 40, "total_backlinks": 300, "spam_score": 3,
                "availability": {"status": "pending-delete"}, "appraisal": {"status": "ok", "value": 1500, "comparables": [{"domain": "a.com", "price": 1200, "year": 2024}]}}
        lines = digest.diligence_lines(item)
        self.assertIn("⏳", lines)
        self.assertIn("backorder", lines)
        self.assertIn("GoValue $1,500", lines)
        self.assertIn("spam 3", digest.metrics_line(item))
        self.assertNotIn("Appraisal", digest.diligence_lines({"domain": "x.com", "appraisal": {"status": "unconfigured"}}))

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
    def setUp(self) -> None:
        super().setUp()
        # Live-mode tests: the registry says every name is free unless a test overrides this.
        self.availability_calls: list[str] = []

        def fake_check(domain, **kw):
            self.availability_calls.append(domain)
            return {"status": "available", "source": "rdap", "checked_at": "now", "server": "https://rdap.test/"}

        for target, value in (("check_availability", fake_check), ("load_rdap_bases", lambda cache=None, **kw: dict(pipeline.availability.KNOWN_RDAP_BASES))):
            patcher = mock.patch.object(pipeline.availability, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

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

    def test_live_mode_paid_source_without_key_exits(self):
        conf = pipeline.DomainFlipperConfig(domain_source="whoisfreaks", whoisfreaks_api_key=None, cache_path=":memory:", audit_path=None)
        with self.assertRaises(SystemExit):
            pipeline.run(conf, dry_run=False, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)

    def test_source_resolution(self):
        self.assertEqual(pipeline.DomainFlipperConfig().resolved_domain_source(), "whoisfreaks-free")
        self.assertEqual(pipeline.DomainFlipperConfig(whoisfreaks_api_key="k").resolved_domain_source(), "whoisfreaks")
        self.assertEqual(pipeline.DomainFlipperConfig().resolved_authority_source(), "none")
        self.assertEqual(pipeline.DomainFlipperConfig(openpagerank_api_key="o").resolved_authority_source(), "openpagerank")
        both = pipeline.DomainFlipperConfig(openpagerank_api_key="o", dataforseo_auth="Basic x")
        self.assertEqual(both.resolved_authority_source(), "openpagerank")  # free source gates the field
        self.assertEqual(both.resolved_deep_authority(), "dataforseo")      # paid counts go deep on the best names
        only_paid = pipeline.DomainFlipperConfig(dataforseo_auth="Basic x")
        self.assertEqual(only_paid.resolved_authority_source(), "dataforseo")
        self.assertEqual(only_paid.resolved_deep_authority(), "none")
        self.assertEqual(pipeline.DomainFlipperConfig().resolved_availability_check(), "rdap")
        self.assertEqual(pipeline.DomainFlipperConfig(availability_check="none").resolved_availability_check(), "none")
        with self.assertRaises(SystemExit):
            pipeline.DomainFlipperConfig(availability_check="bogus").resolved_availability_check()
        with self.assertRaises(SystemExit):
            pipeline.DomainFlipperConfig(domain_source="bogus").resolved_domain_source()

    def test_live_free_mode_with_zero_keys(self):
        conf = pipeline.DomainFlipperConfig(cache_path=":memory:", audit_path=None, top_n=3)
        feed = [sources.normalise_record({"domain": d}) for d in ("lumenpath.com", "bad-name.com", "quillset.ai", "zephyrgrid.ai", "shop24.com")]
        sender = StdoutSender(io.StringIO())
        with mock.patch.object(pipeline.sources, "fetch_free_dropped_domains", return_value=feed):
            result = pipeline.run(conf, sender=sender, log=lambda *a, **k: None)
        self.assertEqual(result.funnel["after_filters"], 3)
        self.assertEqual(result.funnel["after_authority_gate"], 3)  # gate skipped without an authority source
        self.assertEqual(result.funnel["shortlisted"], 3)
        self.assertTrue(all(i["dr"] is None for i in result.shortlist))
        self.assertIn("no authority data", sender.sent[0]["blocks"][2]["text"]["text"])

    def test_auto_falls_back_to_free_feed_on_plan_error(self):
        conf = pipeline.DomainFlipperConfig(whoisfreaks_api_key="bad", cache_path=":memory:", audit_path=None)
        feed = [sources.normalise_record({"domain": "fallback.com"})]
        with mock.patch.object(pipeline.sources, "fetch_dropped_domains", side_effect=sources.SourceError("401 no package", status=401)), \
             mock.patch.object(pipeline.sources, "fetch_free_dropped_domains", return_value=feed):
            result = pipeline.run(conf, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)
        self.assertEqual(result.funnel["fetched"], 1)
        self.assertEqual(result.shortlist[0]["domain"], "fallback.com")
        self.assertEqual(result.sources["domain"], "whoisfreaks-free")  # the record names the feed actually used

    def test_explicit_paid_source_does_not_fall_back(self):
        conf = pipeline.DomainFlipperConfig(domain_source="whoisfreaks", whoisfreaks_api_key="bad", cache_path=":memory:", audit_path=None)
        with mock.patch.object(pipeline.sources, "fetch_dropped_domains", side_effect=sources.SourceError("401", status=401)):
            with self.assertRaises(SystemExit):
                pipeline.run(conf, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)

    def test_auto_does_not_fall_back_on_outage(self):
        conf = pipeline.DomainFlipperConfig(whoisfreaks_api_key="good", cache_path=":memory:", audit_path=None)
        with mock.patch.object(pipeline.sources, "fetch_dropped_domains", side_effect=sources.SourceError("503", status=503)):
            with self.assertRaises(SystemExit):
                pipeline.run(conf, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)

    def test_llm_scoring_is_capped_with_heuristic_preranking(self):
        conf = pipeline.DomainFlipperConfig(openai_api_key="sk", cache_path=":memory:", audit_path=None, max_llm_score=2, top_n=5)
        feed = [sources.normalise_record({"domain": d}) for d in ("aaaaaaaaaaaaaaaaaaa.com", "lumen.com", "bbbbbbbbbbbbbbbbbbb.com", "quill.ai")]
        llm_calls = []

        def fake_score(record, llm):
            llm_calls.append(record["domain"])
            return {"score": 8, "brandability": 8, "suggested_price": 1000, "reasoning": "llm"}

        with mock.patch.object(pipeline.sources, "fetch_free_dropped_domains", return_value=feed), \
             mock.patch.object(pipeline.scoring, "score_domain", fake_score):
            result = pipeline.run(conf, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)
        self.assertEqual(len(llm_calls), 2)
        self.assertEqual(set(llm_calls), {"lumen.com", "quill.ai"})  # short, pronounceable names pre-rank highest
        self.assertEqual(result.funnel["scored"], 2)

    def test_llm_circuit_breaker_falls_back_to_heuristic(self):
        conf = pipeline.DomainFlipperConfig(openai_api_key="sk", cache_path=":memory:", audit_path=None, max_llm_score=50, top_n=3)
        feed = [sources.normalise_record({"domain": f"name{chr(97 + i)}.com"}) for i in range(8)]
        calls = []

        def failing(record, llm):
            calls.append(record["domain"])
            raise RuntimeError("OpenAI rate limit or no credit (429)")

        with mock.patch.object(pipeline.sources, "fetch_free_dropped_domains", return_value=feed), \
             mock.patch.object(pipeline.scoring, "score_domain", failing):
            result = pipeline.run(conf, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)
        self.assertEqual(len(calls), 3)  # breaker opens after three consecutive failures
        self.assertEqual(result.funnel["scored"], 8)  # everything still scored, heuristically
        self.assertEqual(result.funnel["shortlisted"], 3)

    def test_enrich_cap_applies_only_to_metered_sources(self):
        feed = [sources.normalise_record({"domain": f"name{chr(97 + i)}.com"}) for i in range(6)]
        with mock.patch.object(pipeline.sources, "fetch_free_dropped_domains", return_value=feed):
            none = pipeline.run(pipeline.DomainFlipperConfig(cache_path=":memory:", audit_path=None, max_enrich=2), sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)
            with mock.patch.object(pipeline.enrich, "fetch_openpagerank", lambda ds, k, **kw: {d: {"rank": 1, "dr": 50.0, "referring_domains": None, "total_backlinks": None, "spam_score": None, "first_seen": None} for d in ds}):
                opr = pipeline.run(pipeline.DomainFlipperConfig(openpagerank_api_key="o", cache_path=":memory:", audit_path=None, max_enrich=2), sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)
        self.assertEqual(none.funnel["scored"], 6)
        self.assertEqual(opr.funnel["scored"], 6)  # the OPR cap is separate and generous
        with mock.patch.object(pipeline.sources, "fetch_free_dropped_domains", return_value=feed), \
             mock.patch.object(pipeline.enrich, "fetch_openpagerank", lambda ds, k, **kw: {d: {"rank": 1, "dr": 50.0, "referring_domains": None, "total_backlinks": None, "spam_score": None, "first_seen": None} for d in ds}):
            capped = pipeline.run(pipeline.DomainFlipperConfig(openpagerank_api_key="o", cache_path=":memory:", audit_path=None, max_openpagerank=2), sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)
        self.assertEqual(capped.funnel["scored"], 2)

    def test_gate_relaxes_when_nothing_passes(self):
        conf = pipeline.DomainFlipperConfig(openpagerank_api_key="opr", cache_path=":memory:", audit_path=None, top_n=5)
        feed = [sources.normalise_record({"domain": d}) for d in ("alpha.com", "beta.com", "gamma.ai")]
        low = {"alpha.com": 0.0, "beta.com": 0.4, "gamma.ai": 0.2}

        def fake_opr(domains, key, **kw):
            return {d: {"rank": None, "dr": low[d] * 10, "referring_domains": None, "total_backlinks": None, "spam_score": None, "first_seen": None} for d in domains}

        sender = StdoutSender(io.StringIO())
        with mock.patch.object(pipeline.sources, "fetch_free_dropped_domains", return_value=feed), \
             mock.patch.object(pipeline.enrich, "fetch_openpagerank", fake_opr):
            result = pipeline.run(conf, sender=sender, log=lambda *a, **k: None)
        self.assertEqual(result.funnel["gate_relaxed"], 1)
        self.assertEqual([i["domain"] for i in result.shortlist][:1], ["beta.com"])  # highest measurable authority first
        self.assertEqual(result.funnel["shortlisted"], 2)  # alpha.com has zero signal and is left out
        self.assertTrue(any("Gate relaxed" in b.get("elements", [{}])[0].get("text", "") for b in sender.sent[0]["blocks"] if b["type"] == "context"))

        all_zero = {d: 0.0 for d in low}
        with mock.patch.object(pipeline.sources, "fetch_free_dropped_domains", return_value=feed), \
             mock.patch.object(pipeline.enrich, "fetch_openpagerank", lambda ds, k, **kw: {d: {"rank": None, "dr": 0.0, "referring_domains": None, "total_backlinks": None, "spam_score": None, "first_seen": None} for d in ds}):
            result = pipeline.run(conf, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)
        self.assertEqual(result.funnel["shortlisted"], 3)  # name quality alone

    def test_live_free_mode_with_openpagerank(self):
        conf = pipeline.DomainFlipperConfig(openpagerank_api_key="opr", cache_path=":memory:", audit_path=None, top_n=5)
        feed = [sources.normalise_record({"domain": d}) for d in ("strong.com", "weak.com", "middling.ai")]
        scores = {"strong.com": 4.5, "weak.com": 0.3, "middling.ai": 1.2}

        def fake_opr(domains, key, **kw):
            return {d: {"rank": 100, "dr": scores[d] * 10, "referring_domains": None, "total_backlinks": None, "spam_score": None, "first_seen": None, "authority_source": "openpagerank"} for d in domains}

        with mock.patch.object(pipeline.sources, "fetch_free_dropped_domains", return_value=feed), \
             mock.patch.object(pipeline.enrich, "fetch_openpagerank", fake_opr):
            result = pipeline.run(conf, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)
        self.assertEqual(result.funnel["after_authority_gate"], 2)  # weak.com (3.0) fails DR >= 10
        self.assertEqual(result.shortlist[0]["domain"], "strong.com")
        self.assertIn("Open PageRank", pipeline.digest.metrics_line(result.shortlist[0]))

    def test_taken_names_are_dropped_and_the_cap_leaves_the_rest_unchecked(self):
        conf = pipeline.DomainFlipperConfig(openpagerank_api_key="opr", cache_path=":memory:", audit_path=None, top_n=5, max_availability=2)
        feed = [sources.normalise_record({"domain": d}) for d in ("alpha.com", "beta.com", "gamma.com", "delta.com")]
        opr = lambda ds, k, **kw: {d: {"rank": 1, "dr": 40.0, "referring_domains": None, "total_backlinks": None, "spam_score": None, "first_seen": None, "authority_source": "openpagerank"} for d in ds}
        statuses = {"alpha.com": "taken", "beta.com": "available", "gamma.com": "unknown", "delta.com": "available"}

        def fake_check(domain, **kw):
            return {"status": statuses[domain], "source": "rdap", "checked_at": "now", "registrar": "Catcher Inc" if statuses[domain] == "taken" else None}

        with mock.patch.object(pipeline.sources, "fetch_free_dropped_domains", return_value=feed), \
             mock.patch.object(pipeline.enrich, "fetch_openpagerank", opr), \
             mock.patch.object(pipeline.availability, "check_availability", fake_check):
            result = pipeline.run(conf, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)
        by_domain = {i["domain"]: i["availability"]["status"] for i in result.shortlist}
        # Cap of two: alpha and beta were looked up (heuristic tie keeps feed order); alpha was taken and is gone.
        self.assertEqual(by_domain, {"beta.com": "available", "gamma.com": "unchecked", "delta.com": "unchecked"})
        self.assertEqual(result.funnel["available"], len(result.shortlist))
        self.assertEqual(result.sources["availability"], "rdap")

        conf_off = pipeline.DomainFlipperConfig(openpagerank_api_key="opr", cache_path=":memory:", audit_path=None, availability_check="none")
        with mock.patch.object(pipeline.sources, "fetch_free_dropped_domains", return_value=feed), \
             mock.patch.object(pipeline.enrich, "fetch_openpagerank", opr):
            off = pipeline.run(conf_off, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)
        self.assertNotIn("available", off.funnel)
        self.assertNotIn("availability", off.shortlist[0])

    def test_deep_enrichment_and_appraisal_feed_the_model(self):
        conf = pipeline.DomainFlipperConfig(
            openpagerank_api_key="opr", dataforseo_auth="Basic x", godaddy_api_key="k", godaddy_api_secret="s", openai_api_key="sk",
            cache_path=":memory:", audit_path=None, top_n=3, max_deep_enrich=2,
        )
        feed = [sources.normalise_record({"domain": d}) for d in ("lumenpath.com", "zzqxv.com", "neuro.ai")]
        opr = lambda ds, k, **kw: {d: {"rank": 1, "dr": 30.0, "referring_domains": None, "total_backlinks": None, "spam_score": None, "first_seen": None, "authority_source": "openpagerank"} for d in ds}
        deep_calls, appraisal_calls, prompts = [], [], []

        def fake_summary(domain, auth, **kw):
            deep_calls.append(domain)
            return {"rank": 250, "dr": 25.0, "referring_domains": 44, "total_backlinks": 310, "spam_score": 4, "first_seen": "2015-01-01"}

        def fake_appraise(domain, auth, **kw):
            appraisal_calls.append(domain)
            return {"status": "ok", "source": "godaddy", "value": 1800, "currency": "USD", "comparables": [{"domain": "lumen.io", "price": 1500, "year": 2024}], "reasons": [], "checked_at": "now"}

        class FakeLLM:
            def structured(self, *, system, user, schema_name, schema):
                prompts.append(user)
                return {"score": 8, "brandability": 7, "suggested_price": 1700, "reasoning": "ok"}

        with mock.patch.object(pipeline.sources, "fetch_free_dropped_domains", return_value=feed), \
             mock.patch.object(pipeline.enrich, "fetch_openpagerank", opr), \
             mock.patch.object(pipeline.enrich, "fetch_backlink_summary", fake_summary), \
             mock.patch.object(pipeline.diligence, "appraise", fake_appraise), \
             mock.patch.object(pipeline, "LLMClient", lambda **kw: FakeLLM()), \
             mock.patch.object(pipeline.diligence, "wayback_summary", lambda d, **kw: {"status": "none", "timeline_url": "t"}):
            result = pipeline.run(conf, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)
        self.assertEqual(len(deep_calls), 2)                                   # DOMAIN_MAX_DEEP_ENRICH
        self.assertEqual(result.funnel["deep_enriched"], 2)
        self.assertEqual(sorted(set(appraisal_calls)), sorted(set(deep_calls) | {i["domain"] for i in result.shortlist}))
        deep = next(i for i in result.shortlist if i["domain"] in deep_calls)
        self.assertEqual((deep["referring_domains"], deep["gate_dr"], deep["authority_source"]), (44, 30.0, "openpagerank+dataforseo"))
        self.assertTrue(all(i["appraisal"]["value"] == 1800 for i in result.shortlist))
        deep_prompt = next(p for p in prompts if deep["domain"] in p)
        self.assertIn("Referring Domains: 44", deep_prompt)
        self.assertIn("Market appraisal (GoDaddy GoValue, USD): 1800", deep_prompt)
        self.assertIn("lumen.io $1,500 (2024)", deep_prompt)
        self.assertEqual(result.sources["deep_authority"], "dataforseo")
        self.assertEqual(result.sources["appraisal"], "godaddy")
        self.assertIn("Ref Domains", digest.metrics_line(deep))

    def test_deep_enrichment_credential_failure_warns_and_continues(self):
        from agents.common import http as common_http
        conf = pipeline.DomainFlipperConfig(openpagerank_api_key="opr", dataforseo_auth="Basic bad", cache_path=":memory:", audit_path=None, top_n=3, max_deep_enrich=3)
        feed = [sources.normalise_record({"domain": d}) for d in ("lumenpath.com", "neuro.ai", "orbitly.com")]
        opr = lambda ds, k, **kw: {d: {"rank": 1, "dr": 30.0, "referring_domains": None, "total_backlinks": None, "spam_score": None, "first_seen": None, "authority_source": "openpagerank"} for d in ds}
        calls, logs = [], []

        def unauthorised(domain, auth, **kw):
            calls.append(domain)
            raise common_http.HttpError("HTTP 401", status=401)

        with mock.patch.object(pipeline.sources, "fetch_free_dropped_domains", return_value=feed), \
             mock.patch.object(pipeline.enrich, "fetch_openpagerank", opr), \
             mock.patch.object(pipeline.enrich, "fetch_backlink_summary", unauthorised):
            result = pipeline.run(conf, sender=StdoutSender(io.StringIO()), log=logs.append)
        self.assertEqual(len(calls), 1)                      # one rejection is enough; the rest are not attempted
        self.assertEqual(result.funnel["deep_enriched"], 0)
        self.assertEqual(result.funnel["shortlisted"], 3)    # the run still delivers on free data
        self.assertTrue(any("DataForSEO rejected the credentials" in line for line in logs))
        self.assertIsNone(result.shortlist[0]["referring_domains"])

        def flaky(domain, auth, **kw):
            raise common_http.HttpError("HTTP 500", status=500)

        with mock.patch.object(pipeline.sources, "fetch_free_dropped_domains", return_value=feed), \
             mock.patch.object(pipeline.enrich, "fetch_openpagerank", opr), \
             mock.patch.object(pipeline.enrich, "fetch_backlink_summary", flaky):
            result = pipeline.run(conf, sender=StdoutSender(io.StringIO()), log=lambda *a, **k: None)
        self.assertEqual(result.funnel["shortlisted"], 3)    # transient errors are per-domain and never sink the run

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
        self.assertEqual(result.funnel, {"fetched": 3, "after_filters": 2, "after_authority_gate": 1, "available": 1, "scored": 1, "shortlisted": 1})
        self.assertEqual(self.availability_calls, ["lumenpath.com"])
        self.assertEqual(result.shortlist[0]["availability"]["status"], "available")
        self.assertEqual(result.shortlist[0]["appraisal"]["status"], "unconfigured")
        self.assertEqual(result.shortlist[0]["domain"], "lumenpath.com")
        self.assertEqual(result.shortlist[0]["score"], 9)


if __name__ == "__main__":
    unittest.main()

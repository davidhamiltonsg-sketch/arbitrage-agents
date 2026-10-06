from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.helpers import NoNetworkTestCase

from agents.common import cache as cache_mod
from agents.common import config, http, llm, markdown, ranking, slack
from agents.common.audit import AuditLog


class ConfigTests(unittest.TestCase):
    def test_load_dotenv_respects_existing_and_strips_quotes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text('# comment\nexport FOO="bar baz"\nEMPTY=\nKEEP=\'x\'\nBAD LINE\n')
            with mock.patch.dict(os.environ, {"KEEP": "orig"}, clear=False):
                loaded = config.load_dotenv(path)
                self.assertEqual(loaded["FOO"], "bar baz")
                self.assertEqual(os.environ["FOO"], "bar baz")
                self.assertEqual(os.environ["KEEP"], "orig")
                self.assertEqual(config.env("EMPTY", "dflt"), "dflt")
            os.environ.pop("FOO", None)
            os.environ.pop("EMPTY", None)

    def test_env_helpers(self):
        with mock.patch.dict(os.environ, {"N": "7", "F": "1.5", "B": "yes", "L": "Com, AI ,"}):
            self.assertEqual(config.env_int("N", 1), 7)
            self.assertEqual(config.env_float("F", 1), 1.5)
            self.assertTrue(config.env_bool("B", False))
            self.assertEqual(config.env_list("L", ()), ("com", "ai"))
        self.assertEqual(config.env_int("MISSING_N", 3), 3)


class HttpTests(unittest.TestCase):
    def test_build_url_drops_empty_params(self):
        self.assertEqual(http.build_url("https://x.test/a", {"k": "v", "e": None, "z": ""}), "https://x.test/a?k=v")
        self.assertEqual(http.build_url("https://x.test/a?x=1", {"k": "v w"}), "https://x.test/a?x=1&k=v+w")

    def test_redact_url_masks_credentials(self):
        self.assertEqual(http.redact_url("https://x.test/a?apiKey=SECRET&date=1"), "https://x.test/a?apiKey=%2A%2A%2A&date=1")
        self.assertEqual(http.redact_url("https://x.test/a"), "https://x.test/a")

    def test_http_error_str_includes_body(self):
        err = http.HttpError("HTTP 401 for u", status=401, body=b'{"m":"bad key"}')
        self.assertIn("bad key", str(err))

    def test_retries_on_429_then_succeeds(self):
        import urllib.error

        calls = {"n": 0}

        class FakeResp:
            status = 200
            headers = {"Content-Type": "application/json"}

            def read(self):
                return b'{"ok": true}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"Retry-After": "0"}, io.BytesIO(b"rate"))
            return FakeResp()

        sleeps = []
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            resp = http.request("GET", "https://x.test", retries=3, sleep=sleeps.append)
        self.assertEqual(resp.json(), {"ok": True})
        self.assertEqual(calls["n"], 3)
        self.assertEqual(sleeps, [0.0, 0.0])

    def test_non_retryable_status_raises_immediately(self):
        import urllib.error

        def fake_urlopen(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 401, "nope", {}, io.BytesIO(b"denied"))

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            with self.assertRaises(http.HttpError) as ctx:
                http.request("GET", "https://x.test", retries=3, sleep=lambda s: None)
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.body, b"denied")


class CacheTests(unittest.TestCase):
    def test_remember_caches_and_expires(self):
        c = cache_mod.Cache(":memory:")
        calls = []
        value = c.remember("ns", "Key", lambda: calls.append(1) or {"v": 1}, ttl_seconds=100)
        again = c.remember("ns", "key", lambda: calls.append(1) or {"v": 2}, ttl_seconds=100)
        self.assertEqual(value, again)
        self.assertEqual(len(calls), 1)
        c.set("k", "v", ttl_seconds=-1)
        self.assertIsNone(c.get("k"))


class LLMTests(unittest.TestCase):
    def test_strict_schema_strips_ranges_and_sets_required(self):
        schema = {
            "type": "object",
            "properties": {"score": {"type": "integer", "minimum": 1, "maximum": 10, "title": "x"}, "nested": {"type": "object", "properties": {"a": {"type": "string", "pattern": "x"}}}},
            "required": ["score"],
        }
        strict = llm.strict_schema(schema)
        self.assertNotIn("minimum", strict["properties"]["score"])
        self.assertNotIn("title", strict["properties"]["score"])
        self.assertEqual(strict["required"], ["score", "nested"])
        self.assertFalse(strict["additionalProperties"])
        self.assertEqual(strict["properties"]["nested"]["required"], ["a"])
        self.assertNotIn("pattern", strict["properties"]["nested"]["properties"]["a"])
        self.assertIn("minimum", schema["properties"]["score"])  # original untouched

    def test_parse_structured_response(self):
        ok = {"choices": [{"finish_reason": "stop", "message": {"content": '{"score": 7}'}}]}
        self.assertEqual(llm.parse_structured_response(ok), {"score": 7})
        with self.assertRaises(llm.LLMError):
            llm.parse_structured_response({"choices": [{"message": {"refusal": "no"}}]})
        with self.assertRaises(llm.LLMError):
            llm.parse_structured_response({"choices": [{"finish_reason": "length", "message": {"content": "{"}}]})
        with self.assertRaises(llm.LLMError):
            llm.parse_structured_response({"choices": [{"message": {"content": "```json\n{}\n```"}}]})

    def test_clamp_int(self):
        self.assertEqual(llm.clamp_int("12", 1, 10), 10)
        self.assertEqual(llm.clamp_int(3.6, 1, 10), 4)
        self.assertEqual(llm.clamp_int(None, 1, 10, default=5), 5)
        with self.assertRaises(llm.LLMError):
            llm.clamp_int("abc", 1, 10)

    def test_structured_sends_strict_schema(self):
        captured = {}

        def fake_post(url, body, **kwargs):
            captured["url"] = url
            captured["body"] = body
            return {"choices": [{"finish_reason": "stop", "message": {"content": '{"a": 1}'}}]}

        with mock.patch.object(llm.http, "post_json", fake_post):
            client = llm.LLMClient(api_key="k", model="m")
            out = client.structured(system="s", user="u", schema_name="n", schema={"type": "object", "properties": {"a": {"type": "integer", "minimum": 0}}})
        self.assertEqual(out, {"a": 1})
        rf = captured["body"]["response_format"]
        self.assertEqual(rf["type"], "json_schema")
        self.assertTrue(rf["json_schema"]["strict"])
        self.assertNotIn("minimum", rf["json_schema"]["schema"]["properties"]["a"])
        self.assertTrue(captured["url"].endswith("/chat/completions"))


class SlackTests(unittest.TestCase):
    def test_truncate_and_clamp(self):
        self.assertEqual(len(slack.truncate("x" * 5000, 3000)), 3000)
        blocks = [slack.divider() for _ in range(60)]
        clamped = slack.clamp_blocks(blocks)
        self.assertEqual(len(clamped), 50)
        self.assertEqual(clamped[-1]["type"], "context")

    def test_escape(self):
        self.assertEqual(slack.escape("a<b>&c"), "a&lt;b&gt;&amp;c")

    def test_stdout_sender_records(self):
        buf = io.StringIO()
        sender = slack.StdoutSender(buf)
        sender.send(text="t", blocks=[slack.section("hi")])
        self.assertEqual(sender.sent[0]["text"], "t")
        self.assertIn('"hi"', buf.getvalue())

    def test_webhook_sender_requires_ok(self):
        with mock.patch.object(slack.http, "request", return_value=http.Response(200, {}, b"ok", "u")):
            slack.SlackClient(webhook_url="https://hooks.slack.test/x").send(text="t", blocks=[])
        with mock.patch.object(slack.http, "request", return_value=http.Response(200, {}, b"invalid_payload", "u")):
            with self.assertRaises(http.HttpError):
                slack.SlackClient(webhook_url="https://hooks.slack.test/x").send(text="t", blocks=[])


class MarkdownTests(unittest.TestCase):
    def test_mrkdwn_conversion(self):
        src = "*1. a.com* — `Score: 9/10` — *Est. Flip: $3,340*\n• Rationale: _solid &lt;x&gt; &amp; y_\n• Checkout: <https://g.test/?d=a.com|🛒 GoDaddy> | <https://n.test/|Namecheap>"
        out = markdown.mrkdwn_to_markdown(src)
        self.assertIn("**1. a.com**", out)
        self.assertIn("**Est. Flip: $3,340**", out)
        self.assertIn("- Rationale: *solid <x> & y*", out)
        self.assertIn("[🛒 GoDaddy](https://g.test/?d=a.com)", out)
        self.assertIn("[Namecheap](https://n.test/)", out)
        self.assertIn("`Score: 9/10`", out)

    def test_blocks_to_markdown(self):
        blocks = [slack.header("Title"), slack.divider(), slack.section("*x*\n• one"), slack.context("Funnel · a: 1")]
        out = markdown.blocks_to_markdown("fallback", blocks)
        self.assertTrue(out.startswith("## Title\n\n---\n\n**x**\n- one\n\n_Funnel · a: 1_"))
        self.assertTrue(markdown.blocks_to_markdown("fb", [slack.section("hi")]).startswith("## fb\n\nhi"))


class RankingTests(unittest.TestCase):
    def test_multi_key_desc_and_top(self):
        items = [{"score": 8, "p": 100}, {"score": 9, "p": 50}, {"score": 8, "p": 300}, {"score": None, "p": 999}]
        out = ranking.rank(items, keys=(("score", True), ("p", True)), top=3)
        self.assertEqual([(i["score"], i["p"]) for i in out], [(9, 50), (8, 300), (8, 100)])
        self.assertEqual(len(ranking.rank(items, keys=(("score", True),), top=0)), 4)


class AuditTests(NoNetworkTestCase):
    def test_writes_jsonl_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = AuditLog("t", Path(tmp) / "sub" / "a.jsonl")
            log.record("fetched", "a.com", x=1)
            log.record("fetched", "b.com")
            log.record("scored", "a.com", score=5)
            lines = (Path(tmp) / "sub" / "a.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertEqual(json.loads(lines[0])["asset_id"], "a.com")
            self.assertEqual(log.counts(), {"fetched": 2, "scored": 1})
            self.assertFalse(log.flush_to_sheets())


if __name__ == "__main__":
    unittest.main()

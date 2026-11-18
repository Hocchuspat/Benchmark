from __future__ import annotations

import json
import os
import ssl
import tempfile
import threading
import time
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from benchmark.codex import execute
from benchmark.provenance import enforce_opened_sources
from benchmark.runner import prepare_card
from benchmark.web_fetch import (MANIFEST, MAX_BYTES, PageText, PublicRedirects,
                                 collect_local_pages, decode_html, public_url, url_variants, verified_pages)

BASE = "https://acme.example.org/"
BODY = '<html><body><h1>Компания</h1><p>ИНН 0123456789. Монтаж и обслуживание инженерных систем зданий и сооружений.</p>{}</body></html>'
FAKE = Path(__file__).parent.resolve() / "fake_codex.py"


class Response:
    def __init__(self, url, body):
        self.url, self.status = url, 200
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=utf-8"
        self.raw = body.encode("utf-8") if isinstance(body, str) else body

    def read(self, size):
        return self.raw[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class Opener:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def open(self, req, timeout):
        self.calls.append(req.full_url)
        result = self.routes.get(req.full_url)
        if result is None:
            result = HTTPError(req.full_url, 403, "Forbidden", {}, None)
        if isinstance(result, list):
            result = result.pop(0)
        if isinstance(result, Exception):
            raise result
        return Response(req.full_url, result)


class LocalWebTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.attempt = self.root / "attempt"
        self.attempt.mkdir()
        self.company = {"company_id": "local-test", "name": "Компания", "inn": "0123456789", "website": [BASE]}
        resolver = patch("benchmark.web_fetch.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))])
        resolver.start()
        self.addCleanup(resolver.stop)

    def collect(self, routes, **kwargs):
        opener = Opener(routes)
        kwargs.setdefault("browser_fetch", lambda *args, **options: {"pages": [], "attempts": []})
        result = collect_local_pages(self.company, self.attempt, opener=opener, **kwargs)
        return result, opener

    def test_stop_does_not_wait_for_stalled_http_or_save_its_late_response(self):
        event, started, release, finished = [threading.Event() for _ in range(4)]
        response = {"url": BASE, "status": 200, "content_type": "text/html", "raw": BODY.format("").encode(),
                    "text": "Компания ИНН 0123456789. Монтаж инженерных систем зданий.", "links": []}
        def stalled(*args):
            started.set()
            try:
                release.wait(timeout=5)
                return response
            finally:
                finished.set()
        def cancel():
            if started.wait(timeout=3):
                event.set()
        trigger = threading.Thread(target=cancel); trigger.start()
        try:
            with patch("benchmark.web_fetch.read_page", side_effect=stalled), patch("benchmark.browser_fetch.render_pages") as browser:
                beginning = time.monotonic()
                manifest = collect_local_pages(self.company, self.attempt, cancel_event=event)
                self.assertLess(time.monotonic() - beginning, 2)
                browser.assert_not_called()
            self.assertEqual(manifest["pages"], [])
            self.assertEqual(manifest["attempts"][0]["outcome"], "cancelled")
        finally:
            release.set(); trigger.join(timeout=3)
            self.assertTrue(finished.wait(timeout=3))
        self.assertFalse((self.attempt / "pages").exists())

    def test_www_failure_uses_working_host_and_prioritises_requisites(self):
        self.company["website"] = ["http://www.acme.example.org/"]
        body = BODY.format('<a href="/about">О компании</a><a href="/services">Услуги</a>'
                           '<a href="/contacts">Контакты</a><a href="/rekvizity">Реквизиты</a>'
                           '<a href="https://other.example.org/contacts">Контакты партнера</a>')
        evidence, opener = self.collect({"https://www.acme.example.org/": TimeoutError(), BASE: body,
            BASE + "rekvizity": BODY.format("Реквизиты"), BASE + "contacts": BODY.format("Контакты"),
            BASE + "services": BODY.format("Услуги")})
        self.assertEqual([p["url"] for p in evidence["pages"]], [BASE, BASE + "rekvizity", BASE + "contacts", BASE + "services"])
        self.assertNotIn("https://other.example.org/contacts", opener.calls)
        self.assertEqual(len(verified_pages(self.attempt)), 4)

    def test_tls_failure_uses_public_http_without_disabling_verification(self):
        evidence, _ = self.collect({BASE: URLError(ssl.SSLCertVerificationError("bad chain")),
            "https://www.acme.example.org/": URLError(ssl.SSLCertVerificationError("bad chain")),
            "http://acme.example.org/": BODY.format("")})
        self.assertEqual(evidence["pages"][0]["url"], "http://acme.example.org/")
        self.assertEqual(evidence["attempts"][0]["outcome"], "access_error")

    def test_transient_section_failure_retried_once(self):
        evidence, opener = self.collect({BASE: BODY.format('<a href="/contacts">Контакты</a>'),
            BASE + "contacts": [TimeoutError(), BODY.format("Контакты")]})
        self.assertEqual(opener.calls.count(BASE + "contacts"), 2)
        self.assertEqual(len(evidence["pages"]), 2)

    def test_transient_root_failure_retried_once_after_variants(self):
        evidence, opener = self.collect({BASE: [URLError(TimeoutError()), BODY.format("")]})
        self.assertEqual(opener.calls.count(BASE), 2)
        self.assertEqual(len(evidence["pages"]), 1)
        self.assertEqual(evidence["attempts"][0]["outcome"], "timeout")

    def test_short_requisites_page_is_still_readable(self):
        evidence, _ = self.collect({BASE: "<p>ИНН 0123456789</p>"})
        self.assertIn("0123456789", evidence["pages"][0]["text"])

    def test_unknown_or_damaged_manifest_is_not_evidence(self):
        for value in [None, [], {"version": 2}, {"version": 1, "pages": "wrong"}, {"version": 1, "pages": [None]}]:
            with self.subTest(value=value):
                (self.attempt / MANIFEST).write_text(json.dumps(value))
                self.assertEqual(verified_pages(self.attempt), [])

    def test_true_errors_and_challenges_never_become_evidence(self):
        for routes in [{}, {BASE: "<html><h1>Just a moment</h1><p>Verify you are human</p></html>"},
                       {BASE: "<p>" + "x" * MAX_BYTES + "</p>"}]:
            with self.subTest(routes=list(routes)):
                evidence, _ = self.collect(routes)
                self.assertEqual(evidence["pages"], [])
                self.assertEqual(verified_pages(self.attempt), [])

    def test_reserved_and_private_addresses_never_requested(self):
        for url in ["http://127.0.0.1", "http://169.254.169.254", "file:///etc/passwd", "http://localhost", "https://example.test"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                public_url(url)
        with patch("benchmark.web_fetch.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("192.168.1.1", 80))]):
            with self.assertRaises(ValueError):
                public_url(BASE)
        with self.assertRaises(ValueError):
            PublicRedirects().redirect_request(None, None, 302, "", {}, "http://127.0.0.1/private")

    def test_tampering_and_missing_copy_exclude_only_affected_page(self):
        self.collect({BASE: BODY.format('<a href="/contacts">Контакты</a>'), BASE + "contacts": BODY.format("Контакты")})
        (self.attempt / "pages/001.txt").write_text("invented", encoding="utf-8")
        self.assertEqual([p["url"] for p in verified_pages(self.attempt)], [BASE + "contacts"])
        (self.attempt / "pages/002.html").unlink()
        self.assertEqual(verified_pages(self.attempt), [])

    def test_snapshot_paths_cannot_escape_attempt(self):
        self.collect({BASE: BODY.format("")})
        manifest = json.loads((self.attempt / MANIFEST).read_text())
        manifest["pages"][0]["html_file"] = "../../outside.html"
        (self.attempt / MANIFEST).write_text(json.dumps(manifest))
        self.assertEqual(verified_pages(self.attempt), [])

    def test_parser_preserves_russian_content_and_ignores_scripts(self):
        html = '<meta charset="windows-1251"><p>Монтаж систем</p><script>fake inn</script><style>fake CSS</style><a href="/contacts">Контакты</a>'
        parser = PageText()
        parser.feed(decode_html(html.encode("cp1251")))
        self.assertIn("Монтаж систем", parser.text())
        self.assertNotIn("fake", parser.text())
        self.assertEqual(parser.links, [("/contacts", "Контакты")])
        self.assertEqual(url_variants("http://www.acme.example.org/contacts?q=1")[1], BASE + "contacts?q=1")

    def test_budget_exhaustion_does_not_request_or_lose_manifest(self):
        evidence, opener = self.collect({BASE: BODY.format("")}, budget=0)
        self.assertEqual(evidence, {"pages": [], "attempts": []})
        self.assertEqual(opener.calls, [])
        self.assertTrue((self.attempt / MANIFEST).is_file())

    def test_forbidden_http_falls_back_to_browser_with_saved_dom_evidence(self):
        browser = unittest.mock.Mock(return_value={"pages": [{"url": BASE, "status": 200, "content_type": "text/html",
            "html": BODY.format(""), "text": "ИНН 0123456789. Монтаж инженерных систем.", "links": []}],
            "attempts": [{"url": BASE, "method": "browser", "outcome": "loaded"}]})
        evidence, _ = self.collect({}, browser_fetch=browser)
        browser.assert_called_once()
        self.assertIn(BASE, browser.call_args.args[0])
        self.assertEqual(evidence["pages"][0]["url"], BASE)
        self.assertIn("0123456789", evidence["pages"][0]["text"])
        self.assertEqual(verified_pages(self.attempt)[0]["method"], "browser")

    def test_javascript_shell_is_not_saved_as_company_evidence(self):
        browser = unittest.mock.Mock(return_value={"pages": [], "attempts": [{"method": "browser", "outcome": "timeout"}]})
        evidence, opener = self.collect({BASE: '<div id="root">Загрузка...</div><script src="app.js"></script>'}, browser_fetch=browser)
        self.assertEqual(opener.calls, [BASE])
        self.assertEqual(evidence["attempts"][0]["outcome"], "needs_browser")
        browser.assert_called_once()
        self.assertEqual(verified_pages(self.attempt), [])

    def test_browser_tries_http_after_tls_timeouts_instead_of_second_https_host(self):
        self.company["website"] = [BASE.replace("https:", "http:")]
        browser = unittest.mock.Mock(return_value={"pages": [], "attempts": []})
        routes = {url: TimeoutError() for url in url_variants(BASE)}
        self.collect(routes, browser_fetch=browser)
        targets = browser.call_args.args[0]
        self.assertEqual(targets, [BASE.replace("https:", "http:"), BASE])

    def test_previously_discovered_candidate_is_loaded_without_site_in_excel(self):
        self.company["website"] = []
        self.company["candidate_websites"] = [BASE]
        evidence, opener = self.collect({BASE: BODY.format("")})
        self.assertEqual(opener.calls, [BASE])
        self.assertIn(self.company["inn"], evidence["pages"][0]["text"])

    def test_browser_errors_and_malformed_or_private_results_never_become_evidence(self):
        for page in [None, {"url": BASE, "html": 123, "text": "invalid"},
                     {"url": BASE, "status": 403, "content_type": "text/html", "html": BODY.format(""), "text": "Access denied"},
                     {"url": "http://127.0.0.1/private", "status": 200, "content_type": "text/html", "html": BODY.format(""), "text": "private"},
                     {"url": BASE, "status": 0, "content_type": "text/html", "html": BODY.format(""), "text": "Unverified"}]:
            with self.subTest(page=page):
                evidence, _ = self.collect({}, browser_fetch=lambda *args, **kwargs: {"pages": [page], "attempts": []})
                self.assertEqual(evidence["pages"], [])

    def test_successful_browser_load_with_unknown_http_code_keeps_explicit_method(self):
        page = {"url": BASE, "status": 0, "content_type": "text/html", "html": BODY.format(""),
                "text": "ИНН 0123456789", "method": "browser", "load_succeeded": True}
        self.collect({}, browser_fetch=lambda *args, **kwargs: {"pages": [page], "attempts": []})
        saved = verified_pages(self.attempt)[0]
        self.assertEqual(saved["status"], 0)
        self.assertTrue(saved["load_succeeded"])

    def test_readable_http_does_not_start_a_browser(self):
        browser = unittest.mock.Mock(side_effect=AssertionError("No browser needed"))
        evidence, _ = self.collect({BASE: BODY.format("")}, browser_fetch=browser)
        self.assertEqual(len(evidence["pages"]), 1)
        browser.assert_not_called()

    def test_working_host_variant_does_not_trigger_unnecessary_browser_retry(self):
        self.company["website"] = ["https://www.acme.example.org/"]
        browser = unittest.mock.Mock(side_effect=AssertionError("The alternate host already loaded"))
        evidence, _ = self.collect({BASE: BODY.format("")}, browser_fetch=browser)
        self.assertEqual(evidence["pages"][0]["url"], BASE)
        browser.assert_not_called()

    def test_browser_prioritises_unread_requisites_over_failed_host_variants(self):
        self.company["refined_website"] = ["https://other.example.org/"]
        browser = unittest.mock.Mock(return_value={"pages": [], "attempts": []})
        self.collect({BASE: BODY.format('<a href="/rekvizity">Реквизиты</a>')}, browser_fetch=browser)
        targets = browser.call_args.args[0]
        self.assertEqual(targets[0], BASE + "rekvizity")
        self.assertNotIn(BASE, targets)

    def local_card(self):
        self.attempt.rmdir()
        opener = Opener({BASE: BODY.format("")})
        original = collect_local_pages
        def collector(company, attempt_dir, **kwargs):
            return original(company, attempt_dir, opener=opener, **kwargs)
        with patch("benchmark.codex.collect_local_pages", side_effect=collector), patch.dict(os.environ, {"BENCHMARK_TEST_MODE": "local_only"}):
            return execute(self.company, self.attempt, str(FAKE), timeout=5)

    def test_local_copy_reaches_codex_and_is_accepted_without_web_events(self):
        result = self.local_card()
        self.assertEqual(result["web_calls"], 0)
        card, quality = prepare_card(result, self.company, self.attempt)
        self.assertEqual(card["facts"]["inn"]["value"], self.company["inn"])
        self.assertFalse(quality["needs_review"])
        self.assertEqual(quality["provenance"]["local_page_urls"], [BASE.rstrip("/")])
        with patch("benchmark.web_fetch.socket.getaddrinfo", side_effect=AssertionError("Recheck must stay offline")):
            rechecked, repeated_quality = prepare_card(result, self.company, self.attempt)
        self.assertEqual(rechecked, card)
        self.assertEqual(repeated_quality, quality)
        (self.attempt / "pages/001.html").write_text("changed")
        checked, _ = enforce_opened_sources(result["card"], self.attempt / "events.jsonl")
        self.assertIsNone(checked["facts"]["inn"]["value"])

    def test_loaded_candidate_cannot_be_marked_wholly_unavailable(self):
        result = self.local_card()
        result["card"]["identity"]["status"] = "unconfirmed"
        result["card"]["research_status"] = "site_unavailable"
        card, quality = prepare_card(result, self.company, self.attempt)
        self.assertEqual(card["research_status"], "needs_review")
        self.assertTrue(quality["needs_review"])
        result["card"]["official_site"] = "https://unavailable.example.org/"
        card, quality = prepare_card(result, self.company, self.attempt)
        self.assertEqual(card["research_status"], "needs_review")
        self.assertEqual(quality["original_research_status"], "site_unavailable")

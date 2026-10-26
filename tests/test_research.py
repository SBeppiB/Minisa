import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from runtime import research
from runtime.cognition.dream_context import source_manifest


def response(payload, content_type="application/json"):
    return json.dumps(payload).encode(), content_type, "https://search.example/search"


class ResearchTests(unittest.TestCase):
    def test_missing_credentials_explains_free_options(self):
        with self.assertRaisesRegex(research.ResearchError, "TAVILY_API_KEY or MINISA_SEARXNG_URL"):
            research.search_web("Minisa", environ={})

    def test_tavily_search_normalizes_results_without_exposing_key(self):
        seen = {}
        def opener(url, **kwargs):
            seen.update(kwargs)
            return response({"results": [{"title": "Example", "url": "https://example.org/page", "content": "Summary"}]})
        with patch.object(research, "_public_url", return_value=("https", "example.org", 443, ("93.184.216.34", "/", "https://example.org/"))):
            result = research.search_web("query", environ={"TAVILY_API_KEY": "never-print-this"}, opener=opener)
        self.assertEqual(result[0]["title"], "Example")
        self.assertNotIn("never-print-this", json.dumps(result))
        self.assertEqual(seen["headers"]["Authorization"], "Bearer never-print-this")

    def test_searxng_fallback_and_malformed_results(self):
        def opener(url, **kwargs):
            self.assertIn("format=json", url)
            return response({"results": [{"title": "A", "url": "https://example.org", "content": "x"}]})
        with patch.object(research, "_public_url", return_value=("https", "example.org", 443, ("93.184.216.34", "/", "https://example.org/"))):
            self.assertEqual(len(research.search_web("q", environ={"MINISA_SEARXNG_URL": "https://search.example"}, opener=opener)), 1)
            with self.assertRaisesRegex(research.ResearchError, "malformed"):
                research.search_web("q", environ={"MINISA_SEARXNG_URL": "https://search.example"}, opener=lambda *a, **k: (b"{}", "application/json", ""))

    def test_quota_error_is_safe_and_specific(self):
        def exhausted(*args, **kwargs):
            raise research.ResearchError("Search provider denied the request or its free quota may be exhausted (HTTP 429).")
        with self.assertRaisesRegex(research.ResearchError, "quota may be exhausted"):
            research.search_web("q", environ={"TAVILY_API_KEY": "secret"}, opener=exhausted)

    def test_private_and_local_urls_rejected_before_transport(self):
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        with patch.object(research.socket, "getaddrinfo", return_value=private):
            with self.assertRaisesRegex(research.ResearchError, "public HTTP"):
                research._public_url("http://localhost/private")
        with patch.object(research.socket, "getaddrinfo", return_value=private):
            with self.assertRaises(research.ResearchError):
                research._request("http://127.0.0.1/")

    def test_redirect_destination_is_revalidated_before_second_connection(self):
        from unittest.mock import MagicMock
        public = ("http", "public.example", 80, ("93.184.216.34", "/", "http://public.example/"))
        redirect = MagicMock(status=302)
        redirect.getheader.return_value = "http://127.0.0.1/admin"
        connection = MagicMock()
        connection.getresponse.return_value = redirect
        with patch.object(research, "_public_url", side_effect=[public, research.ResearchError("private redirect blocked")]) as validate, \
             patch.object(research.socket, "create_connection", return_value=MagicMock()), \
             patch.object(research, "HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(research.ResearchError, "private redirect"):
                research._request("http://public.example/")
        self.assertEqual(validate.call_count, 2)

    def test_extracts_readable_page_and_rejects_bad_content_type_and_empty_html(self):
        with patch.object(research, "_request", return_value=(b"<html><title>Title</title><script>bad()</script><p>Readable text.</p></html>", "text/html", "https://example.org")):
            page = research.extract_page("https://example.org")
            self.assertEqual(page["title"], "Title")
            self.assertIn("Readable text", page["text"])
            self.assertNotIn("bad()", page["text"])
        with patch.object(research, "_request", return_value=(b"binary", "application/octet-stream", "https://example.org")):
            with self.assertRaisesRegex(research.ResearchError, "Only HTML"):
                research.extract_page("https://example.org")
        with patch.object(research, "_request", return_value=(b"<html><script>none</script></html>", "text/html", "https://example.org")):
            with self.assertRaisesRegex(research.ResearchError, "no readable"):
                research.extract_page("https://example.org")

    def test_save_deduplicates_and_becomes_dream_memory_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "MEMORY" / "active").mkdir(parents=True)
            fake = lambda url: {"title": "Page title", "url": "https://example.org/source", "text": "page"}
            first = research.save_research(root, "https://example.org/source", "A short summary.",
                                           key_points=["A cited claim."], fetcher=fake)
            second = research.save_research(root, "https://example.org/source", "Different duplicate.", fetcher=fake)
            self.assertEqual(first, second)
            text = first.read_text()
            self.assertIn("Source: https://example.org/source", text)
            self.assertIn("Retrieved:", text)
            self.assertIn("Retrieved summary (claims from the cited page; unverified)", text)
            manifest = source_manifest(root)
            self.assertIn(str(first.relative_to(root)), manifest["files"])
            self.assertIn(str(first.relative_to(root)), manifest["members"]["MEMORY/active"])


if __name__ == "__main__":
    unittest.main()

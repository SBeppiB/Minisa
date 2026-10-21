"""Bounded public web research saved as attributed Minisa MEMORY notes.

Fetched page text is untrusted data. This module never executes page content.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import ssl
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit
from http.client import HTTPConnection, HTTPSConnection

from runtime.memory.manager import MemoryManager
from runtime.storage import writer_lock

MAX_RESPONSE = 1_000_000
MAX_PAGE_TEXT = 24_000
MAX_NOTE = 5_000
MAX_RESULTS = 5
TIMEOUT = 8
USER_AGENT = "MinisaResearch/1.0 (public webpage reader)"


class ResearchError(RuntimeError):
    """Safe, user-displayable research failure."""


def _public_url(url: str) -> tuple[str, str, int, str]:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        if (parsed.scheme not in {"http", "https"} or not host or parsed.username
                or parsed.password):
            raise ValueError
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not 1 <= port <= 65535:
            raise ValueError
        addresses = {ipaddress.ip_address(item[4][0].split("%", 1)[0])
                     for item in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError
        address = sorted(addresses, key=lambda item: (item.version, int(item)))[0]
        path = urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        canonical = urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""))
        return parsed.scheme, host, port, (str(address), path, canonical)
    except (ValueError, OSError, UnicodeError) as error:
        raise ResearchError("URL must use a public HTTP or HTTPS destination.") from error


def _request(url: str, *, method="GET", body=None, headers=None,
             max_bytes=MAX_RESPONSE, redirects=3) -> tuple[bytes, str, str]:
    current = url
    for redirect_count in range(redirects + 1):
        scheme, host, port, pin = _public_url(current)
        address, target, canonical = pin
        try:
            sock = socket.create_connection((address, port), timeout=TIMEOUT)
            if scheme == "https":
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
            connection = (HTTPSConnection(host, port, timeout=TIMEOUT) if scheme == "https"
                          else HTTPConnection(host, port, timeout=TIMEOUT))
            connection.sock = sock
            request_headers = {"User-Agent": USER_AGENT, "Accept": "application/json,text/html,text/plain;q=0.9,*/*;q=0.1",
                               "Connection": "close", **(headers or {})}
            connection.request(method, target, body=body, headers=request_headers)
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                response.read(4096)
                connection.close()
                if not location or redirect_count >= redirects:
                    raise ResearchError("The webpage redirected too many times or omitted its destination.")
                current = urljoin(current, location)
                continue
            content_type = response.getheader("Content-Type", "").split(";", 1)[0].strip().lower()
            length = response.getheader("Content-Length")
            if length and int(length) > max_bytes:
                raise ResearchError("The response exceeds the size limit.")
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ResearchError("The response exceeds the size limit.")
            if not 200 <= response.status < 300:
                if response.status in {402, 403, 429, 432, 433}:
                    raise ResearchError(f"Search provider denied the request or its free quota may be exhausted (HTTP {response.status}).")
                raise ResearchError(f"Public service returned HTTP {response.status}.")
            return data, content_type, canonical
        except ResearchError:
            raise
        except (OSError, TimeoutError, ssl.SSLError, ValueError) as error:
            raise ResearchError("The public webpage could not be retrieved safely.") from error
        finally:
            try:
                connection.close()
            except (UnboundLocalError, AttributeError):
                pass
    raise ResearchError("The webpage could not be retrieved.")


class _TextExtractor(HTMLParser):
    BLOCKED = {"script", "style", "noscript", "svg", "form", "iframe", "object"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr", "section", "article"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.parts = []
        self.title = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKED:
            self.depth += 1
        if tag == "title": self.in_title = True
        if tag in self.BLOCK and self.depth == 0: self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.BLOCKED and self.depth: self.depth -= 1
        if tag == "title": self.in_title = False
        if tag in self.BLOCK and self.depth == 0: self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title: self.title.append(data)
        if not self.depth: self.parts.append(data)


def extract_page(url: str) -> dict:
    data, content_type, canonical_url = _request(url, max_bytes=MAX_RESPONSE)
    if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
        raise ResearchError("Only HTML and plain text webpages are supported.")
    try:
        text = data.decode("utf-8", errors="replace")
        if content_type == "text/plain":
            title = canonical_url
            readable = text
        else:
            parser = _TextExtractor()
            parser.feed(text)
            title = " ".join(" ".join(parser.title).split())[:200] or canonical_url
            readable = " ".join(" ".join(parser.parts).split())
    except Exception as error:
        raise ResearchError("The webpage content is malformed or unreadable.") from error
    readable = readable[:MAX_PAGE_TEXT]
    if not readable.strip():
        raise ResearchError("The webpage contains no readable text.")
    return {"title": title, "url": canonical_url, "text": readable}


def search_web(query: str, *, environ=None, opener=_request) -> list[dict]:
    query = " ".join(query.split())
    if not query or len(query) > 300:
        raise ResearchError("Search query must contain 1 to 300 characters.")
    env = os.environ if environ is None else environ
    api_key = env.get("TAVILY_API_KEY", "").strip()
    if api_key:
        body = json.dumps({"query": query, "max_results": MAX_RESULTS,
                           "search_depth": "basic", "include_answer": False,
                           "auto_parameters": False}).encode()
        raw, content_type, _ = opener("https://api.tavily.com/search", method="POST", body=body,
            headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
            max_bytes=256_000, redirects=0)
        if content_type != "application/json":
            raise ResearchError("Search provider returned an unsupported response.")
        try:
            payload = json.loads(raw)
            rows = payload.get("results")
            if not isinstance(rows, list): raise ValueError
            return _normalise_results(rows)
        except (ValueError, TypeError, AttributeError) as error:
            raise ResearchError("Search provider returned malformed results.") from error
    endpoint = env.get("MINISA_SEARXNG_URL", "").strip()
    if not endpoint:
        raise ResearchError("Configure TAVILY_API_KEY or MINISA_SEARXNG_URL to enable web search.")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ResearchError("MINISA_SEARXNG_URL must be an HTTP or HTTPS endpoint.")
    url = endpoint.rstrip("/") + "/search?" + urlencode({"q": query, "format": "json", "pageno": "1"})
    raw, content_type, _ = opener(url, max_bytes=256_000)
    if content_type != "application/json":
        raise ResearchError("SearXNG did not return JSON; its administrator must enable the JSON format.")
    try:
        payload = json.loads(raw)
        rows = payload.get("results")
        if not isinstance(rows, list): raise ValueError
        return _normalise_results(rows)
    except (ValueError, TypeError, AttributeError) as error:
        raise ResearchError("Search provider returned malformed results.") from error


def _normalise_results(rows):
    result = []
    for row in rows[:MAX_RESULTS]:
        if not isinstance(row, dict): continue
        title, url = row.get("title"), row.get("url")
        if not isinstance(title, str) or not isinstance(url, str): continue
        # Reject private destinations before showing a result as fetchable.
        _public_url(url)
        result.append({"title": title[:200], "url": url[:2048],
                       "snippet": str(row.get("content", row.get("snippet", "")))[:500]})
    return result


def save_research(root: Path, url: str, summary: str, *, key_points=(), title=None,
                  fetcher=extract_page) -> Path:
    summary = " ".join(summary.split())
    points = [" ".join(str(item).split()) for item in list(key_points)[:8]]
    if not summary or len(summary) > 1200 or any(not p or len(p) > 400 for p in points):
        raise ResearchError("Provide a concise summary (up to 1,200 characters) and key points (up to 400 each).")
    page = fetcher(url)
    if len(page["text"]) > MAX_PAGE_TEXT:
        raise ResearchError("Extracted page exceeds the research limit.")
    root = Path(root).resolve()
    manager = MemoryManager(root)
    note_title = "Research: " + " ".join((title or page["title"]).split())[:65]
    retrieved = datetime.now(timezone.utc).isoformat(timespec="seconds")
    fact_lines = ["Retrieved summary (claims from the cited page; unverified):", summary]
    if points:
        fact_lines += ["", "Key points (retrieved claims; verify at source):", *["- " + p for p in points]]
    content = (f"# {note_title}\n\nType: web research\nSource: {page['url']}\n"
               f"Retrieved: {retrieved}\n"
               "Minisa conclusions: none saved; keep separate from retrieved claims.\n\n"
               "## Fact\n\n" + "\n".join(fact_lines) + "\n")
    if len(content) > MAX_NOTE:
        raise ResearchError("Research note exceeds the storage limit.")
    with writer_lock(root):
        for existing in manager.list_active_memories():
            if existing.is_symlink() or not existing.is_file(): continue
            match = re.search(r"^Source: (.+)$", existing.read_text(encoding="utf-8"), re.MULTILINE)
            if match and match.group(1).rstrip("/") == page["url"].rstrip("/"):
                return existing
        note_id = manager.next_memory_id()
        return manager.create_memory(note_id, note_title, content)


"""Fetch public company pages locally and keep auditable evidence for Codex."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import ssl
import time
import queue
import threading
from http.client import HTTPException
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, Request, build_opener

import certifi

from .excel import normalize_url

MAX_BYTES = 1_500_000
MAX_PAGES = 8
MAX_REQUESTS = 12
MANIFEST = "local-web.json"


class BrowserRequired(ValueError):
    pass


class FetchCancelled(Exception):
    pass


def cancellable_read(function, cancel_event, *args, **kwargs):
    """Wait interruptibly for read-only network I/O, including DNS lookups.

    The reader never writes files. If interrupted, its late result is ignored;
    the worker process can exit without waiting for a stalled socket.
    """
    if cancel_event is None:
        return function(*args, **kwargs)
    if cancel_event.is_set():
        raise FetchCancelled()
    result = queue.Queue(maxsize=1)
    def read():
        try:
            result.put((True, function(*args, **kwargs)))
        except BaseException as exc:
            result.put((False, exc))
    threading.Thread(target=read, name="benchmark-http-reader", daemon=True).start()
    while not cancel_event.is_set():
        try:
            success, value = result.get(timeout=.1)
        except queue.Empty:
            continue
        if not success:
            raise value
        if cancel_event.is_set():
            raise FetchCancelled()
        return value
    raise FetchCancelled()


def readable_text(text):
    challenge = re.search(r"checking your browser|just a moment|verify you are human|подтвердите, что вы|я не робот|access denied|доступ запрещ[её]н", text[:2500], re.I)
    if not text or (challenge and len(text) < 4000):
        raise ValueError("Нет доступного содержимого страницы или показана проверка доступа")


def needs_browser(html, text):
    script = bool(re.search(r"<script\b", html, re.I))
    shell = bool(re.search(r'id=["\'](?:root|app|__next)["\']', html, re.I))
    return (not text or bool(re.search(r"enable javascript|включите javascript|requires javascript", text, re.I))
            or script and (len(text) < 250 or shell and len(text) < 600))


def waiting_for_content(text):
    return (not text or bool(re.fullmatch(r"\s*(loading|загрузка)[\s.…!]*", text, re.I))
            or bool(re.search(r"enable javascript|включите javascript|requires javascript", text, re.I)))


def successful_page(page):
    status = page.get("status", 0)
    return 200 <= status < 300 or (status == 0 and page.get("method") == "browser" and page.get("load_succeeded") is True)


def public_url(url, *, resolve=True):
    normalized = normalize_url(url)
    if not normalized:
        raise ValueError("Разрешены только публичные HTTP/HTTPS-адреса")
    p = urlsplit(normalized)
    host = p.hostname.rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".test", ".invalid", ".example")):
        raise ValueError("Локальные и тестовые адреса не загружаются")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        addresses = [ipaddress.ip_address(row[4][0]) for row in socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)] if resolve else []
    if any(not address.is_global for address in addresses):
        raise ValueError("Адрес не относится к публичной сети")
    return normalized


class PublicRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def url_variants(url):
    normalized = normalize_url(url)
    if not normalized:
        return []
    p = urlsplit(normalized)
    bare = p.hostname.removeprefix("www.")
    hosts = [p.hostname, bare if p.hostname.startswith("www.") else "www." + bare]
    # HTTPS first; a public HTTP version can still work when TLS is misconfigured.
    return list(dict.fromkeys(urlunsplit((scheme, host + (f":{p.port}" if p.port else ""), p.path or "/", p.query, ""))
                              for scheme in ["https", "http"] for host in hosts))


class PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self.skip = 0
        self.anchor = None

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        if self.skip:
            return
        if tag in {"p", "div", "br", "li", "h1", "h2", "h3", "tr"}:
            self.parts.append("\n")
        if tag == "a":
            self.anchor = [dict(attrs).get("href", ""), []]

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1
            return
        if tag == "a" and self.anchor:
            self.links.append((self.anchor[0], " ".join(self.anchor[1])))
            self.anchor = None

    def handle_data(self, text):
        if not self.skip:
            self.parts.append(text + " ")
            if self.anchor:
                self.anchor[1].append(text)

    def text(self):
        return "\n".join(line for row in "".join(self.parts).splitlines() if (line := " ".join(row.split())))


def decode_html(raw, charset=None):
    meta = re.search(br'charset\s*=\s*["\']?([a-zA-Z0-9_-]+)', raw[:8192], re.I)
    for encoding in [charset, meta[1].decode("ascii") if meta else None, "utf-8", "cp1251"]:
        if encoding:
            try:
                return raw.decode(encoding)
            except (UnicodeError, LookupError):
                continue
    return raw.decode("utf-8", errors="replace")


def read_page(url, timeout, opener):
    public_url(url)
    req = Request(url, headers={"User-Agent": "Benchmark/0.1 (public company research)",
                                "Accept": "text/html,application/xhtml+xml,text/plain", "Accept-Encoding": "identity"})
    with opener.open(req, timeout=timeout) as response:
        final = public_url(response.url)
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
            raise ValueError("Формат страницы не поддерживается локальным загрузчиком: " + content_type)
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Страница превышает ограничение размера")
        if not 200 <= response.status < 300:
            raise ValueError("Неуспешный HTTP-ответ")
        parser = PageText()
        html = decode_html(raw, response.headers.get_content_charset())
        parser.feed(html)
        text = parser.text()
        if needs_browser(html, text):
            raise BrowserRequired("Для чтения страницы требуется выполнение JavaScript")
        readable_text(text)
        return {"url": final, "status": response.status, "content_type": content_type, "raw": raw, "text": text, "links": parser.links}


def relevant_links(page):
    host = urlsplit(page["url"]).hostname.removeprefix("www.")
    found = []
    for href, label in page["links"]:
        url = normalize_url(urljoin(page["url"], href))
        if not url or urlsplit(url).hostname.removeprefix("www.") != host:
            continue
        if re.search(r"контакт|реквиз|о компании|деятельн|услуг|продук|contact|about|service|product|rekviz", label + " " + urlsplit(url).path, re.I):
            found.append(url)
    def priority(url):
        path = urlsplit(url).path
        if re.search(r"реквиз|rekviz|requisit", path, re.I):
            return 0
        if re.search(r"контакт|contact", path, re.I):
            return 1
        if re.search(r"деятельн|услуг|продук|deyatel|uslug|service|product|activit", path, re.I):
            return 2
        return 3
    return sorted(dict.fromkeys(found), key=priority)


def collect_local_pages(company, attempt_dir: Path, *, budget=40, opener=None, browser_fetch=None, cancel_event=None):
    """Bounded, read-only requests; failures never stop the company queue."""
    if opener is None:
        context = ssl.create_default_context()
        context.load_verify_locations(certifi.where())
        opener = build_opener(HTTPSHandler(context=context), PublicRedirects())
    started = time.monotonic()
    manifest = {"version": 1, "pages": [], "attempts": []}
    seen = set()
    is_cancelled = lambda: cancel_event is not None and cancel_event.is_set()

    def snapshot(page, requested_url):
        cancellable_read(public_url, cancel_event, page["url"])
        if not successful_page(page):
            raise ValueError("Нет успешного HTTP-ответа")
        readable_text(page["text"])
        if len(page["raw"]) > MAX_BYTES:
            raise ValueError("Страница превышает ограничение размера")
        folder = attempt_dir / "pages"
        folder.mkdir(exist_ok=True)
        stem = f"{len(manifest['pages']) + 1:03d}"
        html_file, text_file = f"pages/{stem}.html", f"pages/{stem}.txt"
        (attempt_dir / html_file).write_bytes(page["raw"])
        (attempt_dir / text_file).write_text(page["text"], encoding="utf-8")
        manifest["pages"].append({"url": page["url"], "requested_url": requested_url, "accessed_at": date.today().isoformat(),
            "status": page["status"], "content_type": page["content_type"], "method": page.get("method", "http"),
            "load_succeeded": page.get("load_succeeded", False),
            "html_file": html_file, "text_file": text_file, "html_sha256": hashlib.sha256(page["raw"]).hexdigest(),
            "text_sha256": hashlib.sha256(page["text"].encode("utf-8")).hexdigest()})

    def fetch(url):
        # Reserve time and requests for browser rendering if ordinary HTTP fails.
        remaining = budget * .6 - (time.monotonic() - started)
        if is_cancelled() or url in seen or remaining <= 0 or len(manifest["attempts"]) >= MAX_REQUESTS - 4 or len(manifest["pages"]) >= MAX_PAGES:
            return None
        seen.add(url)
        entry = {"url": url}
        manifest["attempts"].append(entry)
        try:
            page = cancellable_read(read_page, cancel_event, url, min(6, remaining), opener)
            snapshot(page, url)
            entry.update(outcome="loaded", final_url=page["url"])
            return page
        except FetchCancelled:
            entry.update(outcome="cancelled")
        except HTTPError as exc:
            entry.update(outcome="http_error", status=exc.code, error=f"HTTP {exc.code}")
            exc.close()
        except BrowserRequired as exc:
            entry.update(outcome="needs_browser", error=str(exc))
        except (TimeoutError, socket.timeout):
            entry.update(outcome="timeout", error="Превышено время локального запроса")
        except URLError as exc:
            entry.update(outcome="timeout" if isinstance(exc.reason, TimeoutError) else "access_error", error=str(exc)[:400])
        except (OSError, ValueError, HTTPException) as exc:
            entry.update(outcome="access_error", error=str(exc)[:400])
        return None

    candidates = list(dict.fromkeys(company.get("refined_website", []) + company.get("website", []) + company.get("candidate_websites", [])))[:3]
    roots = []
    for candidate in candidates:
        first_attempt = len(manifest["attempts"])
        for url in url_variants(candidate):
            page = fetch(url)
            if page:
                roots.append(page)
                break
            if manifest["attempts"] and manifest["attempts"][-1].get("outcome") in {"needs_browser", "cancelled"}:
                break
        else:
            transient = next((a["url"] for a in manifest["attempts"][first_attempt:]
                              if a.get("outcome") == "timeout" or a.get("status") in {502, 503, 504}), None)
            if transient:
                seen.discard(transient)
                if page := fetch(transient):
                    roots.append(page)
    # Visit key sections only after giving both supplied sites a chance to open.
    for root in roots:
        for url in relevant_links(root)[:3]:
            page = fetch(url)
            last = manifest["attempts"][-1] if manifest["attempts"] else {}
            if page is None and last.get("url") == url and (last.get("outcome") == "timeout" or last.get("status") in {502, 503, 504}):
                seen.discard(url)
                fetch(url)  # One bounded retry for a transient section failure.
    def page_key(url):
        parts = urlsplit(url)
        return (parts.hostname.removeprefix("www."), parts.path.rstrip("/"), parts.query)

    loaded_keys = {page_key(page[key]) for page in manifest["pages"] for key in ["url", "requested_url"]}
    targets = []
    schemes_per_page = {}
    preferred_urls = {normalize_url(url) for url in candidates}
    tls_failures = {page_key(entry["url"]) for entry in manifest["attempts"]
                    if urlsplit(entry["url"]).scheme == "https" and entry["outcome"] in {"timeout", "access_error"}}
    attempts = sorted(manifest["attempts"], key=lambda entry: (
        entry["outcome"] != "needs_browser", not bool(urlsplit(entry["url"]).path.strip("/")),
        not (page_key(entry["url"]) in tls_failures and urlsplit(entry["url"]).scheme == "http"),
        entry["url"] not in preferred_urls))
    for entry in attempts:
        if is_cancelled():
            break
        key = page_key(entry["url"])
        scheme = urlsplit(entry["url"]).scheme
        if key in loaded_keys or scheme in schemes_per_page.get(key, set()):
            continue
        if entry["outcome"] not in {"needs_browser", "timeout", "access_error"} and not (
                entry["outcome"] == "http_error" and entry.get("status") in {403, 406, 502, 503, 504}):
            continue
        try:
            cancellable_read(public_url, cancel_event, entry["url"])
        except FetchCancelled:
            break
        except (OSError, ValueError):
            continue
        if entry["url"] not in targets:
            targets.append(entry["url"])
            schemes_per_page.setdefault(key, set()).add(scheme)
    remaining = budget - (time.monotonic() - started)
    if targets and not is_cancelled() and remaining >= 2 and len(manifest["pages"]) < MAX_PAGES:
        if browser_fetch is None:
            from .browser_fetch import render_pages
            browser_fetch = render_pages
        try:
            rendered = browser_fetch(targets[:4], timeout=min(20, remaining), max_pages=MAX_PAGES - len(manifest["pages"]),
                                     max_requests=MAX_REQUESTS - len(manifest["attempts"]), cancel_event=cancel_event)
            manifest["attempts"].extend(rendered.get("attempts", []))
            for page in rendered.get("pages", [])[:MAX_PAGES - len(manifest["pages"])]:
                try:
                    if is_cancelled():
                        break
                    if not isinstance(page, dict) or not isinstance(page.get("html"), str) or not isinstance(page.get("text"), str):
                        raise ValueError("Браузер вернул неверный формат страницы")
                    page = {**page, "raw": page["html"].encode("utf-8"), "method": "browser"}
                    snapshot(page, page["url"])
                except FetchCancelled:
                    break
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    manifest["attempts"].append({"method": "browser", "outcome": "rejected", "error": str(exc)[:300]})
        except (OSError, ValueError, RuntimeError) as exc:
            manifest["attempts"].append({"method": "browser", "outcome": "engine_error", "error": str(exc)[:300]})
    (attempt_dir / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return local_evidence(attempt_dir)


def verified_pages(attempt_dir: Path):
    """Offline verification, also used by recheck; never trust a URL alone."""
    try:
        manifest = json.loads((attempt_dir / MANIFEST).read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("pages"), list):
            return []
        pages = manifest["pages"]
    except (OSError, ValueError, KeyError, TypeError):
        return []
    verified = []
    folder = (attempt_dir / "pages").resolve()
    if not folder.is_relative_to(attempt_dir.resolve()):
        return []
    for page in pages:
        if not isinstance(page, dict):
            continue
        try:
            public_url(page["url"], resolve=False)
            if not successful_page(page):
                continue
            texts = {}
            for kind in ["html", "text"]:
                path = (attempt_dir / page[kind + "_file"]).resolve()
                if not path.is_relative_to(folder) or path.stat().st_size > MAX_BYTES * 3:
                    break
                raw = path.read_bytes()
                if hashlib.sha256(raw).hexdigest() != page[kind + "_sha256"]:
                    break
                texts[kind] = raw
            else:
                verified.append({**page, "text": texts["text"].decode("utf-8")})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return verified


def local_evidence(attempt_dir: Path):
    pages = []
    verified = verified_pages(attempt_dir)
    per_page = min(12000, 40000 // max(1, len(verified)))
    for page in verified:
        text = page["text"]
        if len(text) > per_page:
            head = per_page * 2 // 3
            text = text[:head] + "\n[Часть текста опущена; полная копия сохранена локально]\n" + text[-(per_page - head):]
        pages.append({"url": page["url"], "accessed_at": page["accessed_at"], "method": page.get("method", "http"), "text": text})
    try:
        manifest = json.loads((attempt_dir / MANIFEST).read_text(encoding="utf-8"))
        attempts = manifest.get("attempts", []) if isinstance(manifest, dict) else []
        if not isinstance(attempts, list):
            attempts = []
    except (OSError, ValueError):
        attempts = []
    return {"pages": pages, "attempts": attempts}

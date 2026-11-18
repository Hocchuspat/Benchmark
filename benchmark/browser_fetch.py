"""Isolated Chromium rendering for public pages that need a real browser."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time

from .runtime import hidden_process, worker_executable, frozen, PROCESS_LAUNCH_LOCK, stop_child_process


def browser_command():
    if frozen():
        return [str(worker_executable()), "--benchmark-browser-worker"]
    return [sys.executable, "-u", "-m", "benchmark.browser_fetch"]


def render_pages(urls, *, timeout=18, max_pages=4, max_requests=6, cancel_event=None):
    if not urls or timeout < 2 or max_pages <= 0 or max_requests <= 0:
        return {"pages": [], "attempts": []}
    if cancel_event is not None and cancel_event.is_set():
        return {"pages": [], "attempts": [{"method": "browser", "outcome": "cancelled"}]}
    if importlib.util.find_spec("PySide6") is None or importlib.util.find_spec("PySide6.QtWebEngineCore") is None:
        return {"pages": [], "attempts": [{"method": "browser", "outcome": "engine_unavailable"}]}
    request = {"urls": urls, "timeout": max(1, timeout - 1), "deadline": time.monotonic() + timeout - 1,
               "max_pages": max_pages, "max_requests": max_requests}
    proc = None
    try:
        with PROCESS_LAUNCH_LOCK:
            proc = subprocess.Popen(browser_command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", start_new_session=os.name != "nt", **hidden_process())
        deadline = time.monotonic() + timeout
        pending_input = json.dumps(request)
        while True:
            if cancel_event is not None and cancel_event.is_set():
                stop_child_process(proc)
                proc.communicate()
                return {"pages": [], "attempts": [{"method": "browser", "outcome": "cancelled"}]}
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(browser_command(), timeout)
            try:
                output, errors = proc.communicate(pending_input, timeout=min(.2, remaining))
                break
            except subprocess.TimeoutExpired:
                pending_input = None
        if proc.returncode:
            return {"pages": [], "attempts": [{"method": "browser", "outcome": "engine_error", "error": errors[-600:]}]}
        result = json.loads(output)
        if not isinstance(result, dict) or not isinstance(result.get("pages"), list) or not isinstance(result.get("attempts"), list):
            raise ValueError("Неверный ответ браузерного процесса")
        return result
    except subprocess.TimeoutExpired:
        stop_child_process(proc)
        proc.communicate()
        return {"pages": [], "attempts": [{"method": "browser", "outcome": "timeout"}]}
    except (OSError, ValueError) as exc:
        return {"pages": [], "attempts": [{"method": "browser", "outcome": "engine_error", "error": str(exc)[:400]}]}


def run_engine(request, *, seed_html=None):
    """Run only in a child process. seed_html permits offline engine tests, not HTTP evidence."""
    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineCore import (QWebEnginePage, QWebEngineProfile, QWebEngineSettings,
                                         QWebEngineUrlRequestInterceptor)
    from .web_fetch import MAX_BYTES, public_url, readable_text, waiting_for_content, relevant_links, successful_page

    class PublicRequests(QWebEngineUrlRequestInterceptor):
        def interceptRequest(self, info):
            if seed_html is not None:
                # Offline fixture: no network requests are permitted.
                info.block(info.requestUrl().scheme() not in {"data", "about"})
                return
            if bytes(info.requestMethod()) not in {b"GET", b"HEAD"}:
                info.block(True)
                return
            try:
                public_url(info.requestUrl().toString())
            except (OSError, ValueError):
                info.block(True)

    class Page(QWebEnginePage):
        def acceptNavigationRequest(self, url, kind, is_main):
            if seed_html is not None:
                return url.scheme() in {"data", "about"}
            try:
                public_url(url.toString())
                return True
            except (OSError, ValueError):
                return False

        def javaScriptConsoleMessage(self, *args):
            pass

        def chooseFiles(self, *args):
            return []

    app = QApplication.instance() or QApplication(["Benchmark browser"])
    app.setQuitOnLastWindowClosed(False)
    profile = QWebEngineProfile(app)  # Off-the-record: no personal browser cookies.
    interceptor = PublicRequests(profile)
    profile.setUrlRequestInterceptor(interceptor)
    profile.downloadRequested.connect(lambda download: download.cancel())
    page = Page(profile, app)
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False)
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, False)
    page.settings().setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False)
    page.certificateError.connect(lambda error: error.rejectCertificate())
    page.permissionRequested.connect(lambda permission: permission.deny())
    result = {"pages": [], "attempts": []}
    pending = list(dict.fromkeys(request.get("urls", [])))[:4]
    visited = set()
    timeout = min(35, max(.1, float(request.get("timeout", 18))))
    if "deadline" in request:
        timeout = min(timeout, max(.1, float(request["deadline"]) - time.monotonic()))
    max_pages = min(8, max(1, int(request.get("max_pages", 4))))
    max_requests = min(8, max(1, int(request.get("max_requests", 6))))
    started = time.monotonic()
    current = {"url": "", "status": 0, "generation": 0, "polls": 0, "finished": False}
    timer = QTimer(); timer.setSingleShot(True)

    def finish():
        current["finished"] = True
        timer.stop()
        page.triggerAction(QWebEnginePage.WebAction.Stop)
        app.quit()

    def next_page():
        if len(result["pages"]) >= max_pages or len(result["attempts"]) >= max_requests or time.monotonic() - started >= timeout:
            finish(); return
        while pending:
            url = pending.pop(0)
            if url not in visited:
                break
        else:
            finish(); return
        visited.add(url)
        current.update(url=url, status=0, generation=current["generation"] + 1, polls=0)
        result["attempts"].append({"url": url, "method": "browser", "outcome": "loading"})
        if seed_html is not None:
            page.setHtml(seed_html)
        else:
            try:
                public_url(url)
                page.load(QUrl(url))
            except (OSError, ValueError) as exc:
                result["attempts"][-1].update(outcome="access_error", error=str(exc)[:300])
                QTimer.singleShot(0, next_page)

    def loading(info):
        # Qt exposes load/error status, not an HTTP status getter. Navigation
        # Timing supplies responseStatus when Chromium makes it available.
        code = info.errorCode()
        current["status"] = code if 100 <= code <= 599 else 0

    def capture(generation):
        if current["finished"] or generation != current["generation"]:
            return
        script = f"""JSON.stringify(({{html: document.documentElement.outerHTML.slice(0, {MAX_BYTES + 1}),
            text: (document.body ? document.body.innerText : '').slice(0, 200000),
            response_status: (performance.getEntriesByType('navigation')[0] || {{}}).responseStatus || 0,
            links: Array.from(document.querySelectorAll('a[href]')).slice(0, 300).map(a => [a.href, a.innerText])}}))"""

        def received(data):
            if current["finished"] or generation != current["generation"]:
                return
            try:
                if isinstance(data, str):
                    data = json.loads(data)
                if not isinstance(data, dict):
                    raise ValueError("Браузер не вернул содержимое страницы")
                html, text = data["html"], data["text"].strip()
                if waiting_for_content(text) and current["polls"] < 4:
                    current["polls"] += 1
                    QTimer.singleShot(600, lambda: capture(generation)); return
                readable_text(text)
                if waiting_for_content(text) or len(html.encode("utf-8")) > MAX_BYTES:
                    raise ValueError("Страница ещё не показала читаемое содержимое или превышает ограничение размера")
                url = page.url().toString()
                status = 0 if seed_html is not None else data.get("response_status", 0) or current["status"]
                if seed_html is None:
                    public_url(url)
                    if not successful_page({"status": status, "method": "browser", "load_succeeded": True}):
                        raise ValueError("Нет успешного HTTP-ответа браузера")
                record = {"url": url, "status": status, "content_type": "text/html", "method": "browser", "load_succeeded": seed_html is None,
                          "html": html, "text": text, "links": data["links"]}
                result["pages"].append(record)
                visited.add(url)
                result["attempts"][-1].update(outcome="loaded", final_url=url, status=status)
                if seed_html is None:
                    # Once one host variant works, read its key sections next.
                    from urllib.parse import urlsplit
                    key = lambda value: (urlsplit(value).hostname or "").removeprefix("www.") + urlsplit(value).path.rstrip("/")
                    pending[:] = [value for value in pending if key(value) != key(url)]
                    pending.extend(value for value in relevant_links(record)[:3] if value not in visited and value not in pending)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                result["attempts"][-1].update(outcome="access_error", error=str(exc)[:300])
            QTimer.singleShot(0, next_page)

        page.runJavaScript(script, 1, received)

    def loaded(ok):
        if current["finished"]:
            return
        if not ok:
            result["attempts"][-1].update(outcome="http_error" if current["status"] else "access_error", status=current["status"])
            QTimer.singleShot(0, next_page)
            return
        generation = current["generation"]
        QTimer.singleShot(1200, lambda: capture(generation))

    def expired():
        if result["attempts"] and result["attempts"][-1]["outcome"] == "loading":
            result["attempts"][-1]["outcome"] = "timeout"
        finish()

    page.loadingChanged.connect(loading)
    page.loadFinished.connect(loaded)
    timer.timeout.connect(expired); timer.start(int(timeout * 1000))
    QTimer.singleShot(0, next_page)
    app.exec()
    from shiboken6 import delete
    delete(page); delete(profile)
    app.processEvents()
    return result


def main():
    for stream in [sys.stdin, sys.stdout, sys.stderr]:
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        request = json.load(sys.stdin)
        result = run_engine(request)
    except (ImportError, OSError, ValueError, RuntimeError) as exc:
        result = {"pages": [], "attempts": [{"method": "browser", "outcome": "engine_error", "error": str(exc)[:400]}]}
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

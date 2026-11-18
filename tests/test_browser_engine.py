"""Opt-in Chromium tests: real JavaScript, offline fixture, no model calls."""
from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import unittest

ENGINE = (importlib.util.find_spec("PySide6") is not None
          and importlib.util.find_spec("PySide6.QtWebEngineCore") is not None)


@unittest.skipUnless(ENGINE and os.environ.get("BENCHMARK_BROWSER_INTEGRATION") == "1",
                     "Enable BENCHMARK_BROWSER_INTEGRATION=1 where Chromium helper processes are permitted")
class ChromiumIntegrationTests(unittest.TestCase):
    def render(self, html, timeout=6):
        code = """import json,sys
from benchmark.browser_fetch import run_engine
data=json.load(sys.stdin)
result=run_engine({'urls':['https://example.org/'],'timeout':data['timeout'],'max_pages':1,'max_requests':1},seed_html=data['html'])
for page in result['pages']:page.pop('url',None)
for attempt in result['attempts']:attempt.pop('final_url',None)
print(json.dumps(result,ensure_ascii=False))
"""
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        child = subprocess.run([sys.executable, "-c", code], input=json.dumps({"html": html, "timeout": timeout}),
                               capture_output=True, text=True, encoding="utf-8", env=env, timeout=20)
        self.assertEqual(child.returncode, 0, child.stderr[-2000:])
        return json.loads(child.stdout)

    def test_javascript_content_is_read_after_rendering(self):
        result = self.render('<html><body><div id="root">Загрузка...</div><script>'
            'setTimeout(()=>document.getElementById("root").innerText="ИНН 0123456789. Монтаж инженерных систем.",300)'
            '</script></body></html>')
        self.assertEqual(len(result["pages"]), 1)
        self.assertIn("ИНН 0123456789", result["pages"][0]["text"])
        self.assertEqual(result["pages"][0]["status"], 0)  # setHtml is not HTTP evidence.
        self.assertFalse(result["pages"][0]["load_succeeded"])

    def test_access_challenge_is_not_company_content(self):
        result = self.render('<html><body><h1>Verify you are human</h1></body></html>')
        self.assertEqual(result["pages"], [])
        self.assertEqual(result["attempts"][0]["outcome"], "access_error")

    def test_render_timeout_is_bounded(self):
        result = self.render('<html><body>Загрузка...</body></html>', timeout=.4)
        self.assertEqual(result["pages"], [])
        self.assertEqual(result["attempts"][0]["outcome"], "timeout")

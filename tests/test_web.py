"""Web UI tests (Flask test client). Skipped when Flask is not installed (stdlib-only environments)."""
import re
import tempfile
import unittest
from pathlib import Path

try:
    import flask  # noqa: F401
    HAVE_FLASK = True
except ImportError:  # pragma: no cover
    HAVE_FLASK = False

from rcw import db, demo
from tests.helpers import fixed_clock


def csrf_of(html: str) -> str:
    m = re.search(r'name="csrf_token" value="([0-9a-f]+)"', html)
    assert m, "no csrf token in page"
    return m.group(1)


@unittest.skipUnless(HAVE_FLASK, "Flask not installed")
class WebTests(unittest.TestCase):
    def setUp(self):
        from rcw.web import create_app
        fixed_clock()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "web.db")
        self.app = create_app(self.path, testing=True)
        self.c = self.app.test_client()

    def tearDown(self):
        self.tmp.cleanup()

    def token(self, url="/"):
        return csrf_of(self.c.get(url).get_data(as_text=True))

    @staticmethod
    def form_page(url, data):
        """The page a real user submits this form from (D-006: use that form's own token)."""
        if url.startswith("/periods/"):
            return "/periods/" + url.split("/")[2]
        if url.startswith("/events/"):
            return "/periods/" + data.get("back_period", "")
        if url.startswith("/variance/"):
            return "/variance?period=" + data.get("period", "")
        if url.startswith("/imports"):
            return "/imports"
        return "/"

    def post(self, url, data=None, **kw):
        data = dict(data or {})
        html = self.c.get(self.form_page(url, data)).get_data(as_text=True)
        forms = {m.group(1).replace("&amp;", "&"): m.group(2) for m in
                 re.finditer(r'<form[^>]*action="([^"]+)"[^>]*>(.*?)</form>', html, re.S)}
        if url in forms:
            m = re.search(r'name="csrf_token" value="([^"]*)"', forms[url])
            self.assertTrue(m and m.group(1), f"form {url} renders an empty CSRF token")
            data["csrf_token"] = m.group(1)
        else:   # action not rendered in the current state (e.g. a refused step): use the page token
            data["csrf_token"] = csrf_of(html)
        return self.c.post(url, data=data, follow_redirects=True, **kw)

    def act(self, who):
        self.post("/actor", {"actor": who})

    def fingerprint(self):
        conn = db.connect(self.path)
        try:
            return db.db_fingerprint(conn)
        finally:
            conn.close()

    def test_empty_state_and_security_headers(self):
        r = self.c.get("/")
        html = r.get_data(as_text=True)
        self.assertIn("No data yet", html)
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(self.c.get("/variance").status_code, 200)
        self.assertEqual(self.c.get("/periods/2026-13").status_code, 404)
        self.assertEqual(self.c.get("/periods/0000-01").status_code, 404)

    def test_csrf_and_trusted_host(self):
        r = self.c.post("/demo/load-stage1", data={})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.c.get("/", headers={"Host": "evil.example"}).status_code, 400)

    def test_full_story_over_http(self):
        r = self.post("/demo/load-stage1")
        self.assertIn("Stage 1 loaded", r.get_data(as_text=True))
        r = self.post("/periods/2026-04/calculate")
        self.assertIn("Calculated: run #1", r.get_data(as_text=True))
        self.post("/periods/2026-04/submit")
        r = self.post("/periods/2026-04/close")                       # still analyst-1 -> refused
        self.assertIn("SAME_ACTOR", r.get_data(as_text=True))
        self.act("manager-1")
        r = self.post("/periods/2026-04/close")
        html = r.get_data(as_text=True)
        self.assertIn("Closed and frozen", html)
        april_csv = self.c.get("/exports/2026-04/statement.csv").data
        r = self.post("/periods/2026-04/close")
        self.assertIn("ALREADY_CLOSED", r.get_data(as_text=True))
        r = self.post("/demo/load-stage2")
        self.assertIn("LATE_EVENT_PENDING", r.get_data(as_text=True))
        self.act("analyst-1")
        self.post("/periods/2026-05/calculate")
        r = self.post("/periods/2026-05/submit")
        self.assertIn("BLOCKING_HOLDS", r.get_data(as_text=True))
        r = self.post("/events/E-2026-0431/decide", {"decision": "POST_LATE", "reason": "short", "back_period": "2026-05"})
        self.assertIn("REASON_REQUIRED", r.get_data(as_text=True))
        self.post("/events/E-2026-0431/decide", {"decision": "POST_LATE", "back_period": "2026-05",
                                                 "reason": "Bank confirmed the 29 April receipt; post in May"})
        self.post("/periods/2026-05/calculate")
        self.post("/periods/2026-05/submit")
        self.act("manager-1")
        r = self.post("/periods/2026-05/close")
        self.assertIn("Closed and frozen", r.get_data(as_text=True))
        r = self.post("/variance/refresh", {"period": "2026-05"})
        html = r.get_data(as_text=True)
        self.assertIn("+80.00", html.replace("SGD ", "").replace("SGD ", ""))
        self.assertEqual(self.c.get("/exports/2026-04/statement.csv").data, april_csv)
        # statement + evidence drill-down
        html = self.c.get("/periods/2026-05/statements/REP-AURORA/SGD").get_data(as_text=True)
        self.assertIn("Refund clawback", html)
        html = self.c.get("/periods/2026-05/lines/CLAWBACK:E-2026-0502:REP-AURORA").get_data(as_text=True)
        self.assertIn("original earning E = 160.00", html)
        self.assertIn("EARNING:E-2026-0404:REP-AURORA", html)

    def test_get_pages_are_read_only(self):
        conn = demo.fresh_db(self.path)
        demo.play_full_story(conn)
        conn.close()
        before = self.fingerprint()
        seen, todo = set(), ["/"]
        while todo:
            u = todo.pop()
            if u in seen:
                continue
            seen.add(u)
            r = self.c.get(u)
            self.assertEqual(r.status_code, 200, u)
            if "text/html" in r.content_type:
                for h in re.findall(r'href="(/[^"#]*)"', r.get_data(as_text=True)):
                    h = h.replace("&amp;", "&")
                    if not h.startswith("/static") and h not in seen:
                        todo.append(h)
        self.assertGreater(len(seen), 50)
        self.assertEqual(self.fingerprint(), before, "a GET request changed the database")

    def test_every_post_form_carries_a_nonempty_csrf_token(self):
        """Regression for D-006: macro-rendered hidden inputs were empty in a real browser."""
        conn = demo.fresh_db(self.path)
        demo.load_stage(conn, 1)
        conn.close()
        forms = 0
        for url in ["/", "/periods/2026-04", "/imports", "/quarantine", "/variance?period=2026-04", "/imports/5"]:
            html = self.c.get(url).get_data(as_text=True)
            for m in re.finditer(r'<form[^>]*method="post"[^>]*>(.*?)</form>', html, re.S):
                forms += 1
                tok = re.search(r'name="csrf_token" value="([^"]*)"', m.group(1))
                self.assertTrue(tok and len(tok.group(1)) == 32, f"{url}: POST form without CSRF token")
        self.assertGreater(forms, 8)

    def test_upload_duplicate_bad_file_and_escaping(self):
        self.post("/demo/load-stage1")
        data = (demo.DEMO_DIR / "stage1" / "01_reps.csv").read_bytes()
        import io
        r = self.post("/imports", {"kind": "reps", "mode": "quarantine",
                                   "file": (io.BytesIO(data), "renamed_copy.csv")}, content_type="multipart/form-data")
        self.assertIn("duplicate file", r.get_data(as_text=True))
        bad = b"rep_id,rep_id,team\nX,Y,Z\n"
        r = self.post("/imports", {"kind": "reps", "mode": "strict", "file": (io.BytesIO(bad), "bad.csv")},
                      content_type="multipart/form-data")
        self.assertIn("HEADER_DUPLICATE", r.get_data(as_text=True))
        evil = b"rep_id,display_name,team\nREP-EVIL,<script>alert(1)</script>,=cmd\n"
        self.post("/imports", {"kind": "reps", "mode": "quarantine", "file": (io.BytesIO(evil), "evil.csv")},
                  content_type="multipart/form-data")
        html = self.c.get("/policy").get_data(as_text=True) + self.c.get("/audit").get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", html)

    def test_reset_requires_confirmation(self):
        self.post("/demo/load-stage1")
        r = self.post("/demo/reset")
        self.assertIn("Tick the confirmation box", r.get_data(as_text=True))
        r = self.post("/demo/reset", {"confirm": "yes"})
        self.assertIn("Demo database reset", r.get_data(as_text=True))
        self.assertIn("No data yet", self.c.get("/").get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()

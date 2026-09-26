"""Flask web UI. Thin layer over rcw.services — no business rule lives here.

Security posture for a local demo: binds to 127.0.0.1 (CLI), trusted Host header only
(DNS-rebinding guard), CSRF token on every POST, autoescaped templates, strict CSP,
upload size cap. GET routes never write to the database.
"""
from __future__ import annotations

import secrets
import sqlite3
from pathlib import Path
from typing import Any

from flask import Flask, abort, g, request, session

from .. import db
from ..money import format_bps, format_minor, format_plain
from ..periods_util import is_period, period_label

ACTORS = {
    "analyst-1": "analyst-1 · preparer",
    "manager-1": "manager-1 · reviewer",
    "ops-1": "ops-1 · payroll liaison",
}


def create_app(db_path: str | Path, *, testing: bool = False) -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=secrets.token_hex(32),
        DB_PATH=str(db_path),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_HTTPONLY=True,
        TRUSTED_HOSTS=["127.0.0.1", "localhost"],
        TESTING=testing,
    )
    conn = db.connect(db_path)
    db.init_db(conn)
    conn.close()

    @app.before_request
    def _before() -> None:
        g.conn = db.connect(app.config["DB_PATH"])
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        if session.get("actor") not in ACTORS:
            session["actor"] = "analyst-1"
        if request.method == "POST":
            sent = request.form.get("csrf_token", "")
            if not secrets.compare_digest(sent, session["csrf"]):
                abort(400, description="Missing or invalid CSRF token - reload the page and try again.")

    @app.teardown_request
    def _teardown(_exc: BaseException | None) -> None:
        c: sqlite3.Connection | None = g.pop("conn", None)
        if c is not None:
            c.close()

    @app.after_request
    def _headers(resp):
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    @app.context_processor
    def _ctx() -> dict[str, Any]:
        bd = db.get_setting(g.conn, "business_date") if "conn" in g else None
        return {"csrf_token": session.get("csrf", ""), "actor": session.get("actor", "analyst-1"),
                "actors": ACTORS, "business_date": bd}

    app.add_template_filter(lambda v, c=None: format_minor(int(v), c) if v is not None else "", "money")
    app.add_template_filter(lambda v, c=None: format_minor(int(v), c, signed=True) if v is not None else "", "smoney")
    app.add_template_filter(lambda v: format_minor(int(v)) if v is not None else "", "amt")
    app.add_template_filter(lambda v: format_plain(int(v)) if v is not None else "", "plain")
    app.add_template_filter(lambda v: format_bps(int(v)) if v is not None else "", "bps")
    app.add_template_filter(lambda p: period_label(p) if p and is_period(p) else (p or ""), "plabel")

    from .routes import bp
    app.register_blueprint(bp)
    return app

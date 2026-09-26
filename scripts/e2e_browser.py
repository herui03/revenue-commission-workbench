"""Real-browser end-to-end run of the guided demo (Playwright + Chromium).

Starts the Flask app on 127.0.0.1 with a fresh temporary database, clicks through the whole
story like a user would, saves screenshots to docs/evidence/screenshots/, and checks every
visited page for console errors and horizontal overflow at desktop and phone widths.

    python scripts/e2e_browser.py            # needs: pip install -r requirements-dev.txt
Exit code 0 = all checks passed. Results: docs/evidence/e2e_results.json
"""
from __future__ import annotations

import json
import platform
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from werkzeug.serving import make_server  # noqa: E402

from rcw.web import create_app  # noqa: E402

SHOTS = ROOT / "docs" / "evidence" / "screenshots"
RESULTS = ROOT / "docs" / "evidence" / "e2e_results.json"


def main() -> int:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.TemporaryDirectory()
    app = create_app(str(Path(tmp.name) / "e2e.db"))
    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    checks: list[dict] = []
    console_errors: list[str] = []
    visited: set[str] = set()
    t0 = time.time()

    def check(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "passed": bool(ok), "detail": detail})
        print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""))

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page = ctx.new_page()
        page.on("console", lambda m: console_errors.append(f"{page.url}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: console_errors.append(f"{page.url}: {e}"))
        page.on("dialog", lambda d: d.accept())

        def go(path: str) -> None:
            page.goto(base + path)
            visited.add(path)

        def shot(name: str, full: bool = True) -> None:
            page.screenshot(path=str(SHOTS / f"{name}.png"), full_page=full)

        def flash() -> str:
            return " | ".join(page.locator(".flash").all_inner_texts())

        def act(label: str) -> None:
            page.select_option("#actor", label)
            page.wait_for_load_state("networkidle")

        # 1. empty state -> load demo
        go("/")
        check("empty state offers one-click demo load", page.get_by_text("No data yet").is_visible())
        page.get_by_role("button", name="Load demo data (stage 1)").click()
        check("stage 1 loaded", "Stage 1 loaded" in flash(), flash()[:160])
        shot("01_overview_stage1")

        # 2. April: calculate -> submit -> close
        page.get_by_role("link", name="Close April 2026").click()
        page.get_by_role("button", name="Calculate draft").click()
        check("April calculated with no blocking holds", "0 blocking holds" in flash(), flash()[:160])
        page.get_by_role("button", name="Submit for review as analyst-1").click()
        check("April submitted", "Submitted run" in flash())
        shot("02_april_in_review")
        act("manager-1")
        page.get_by_role("button", name="Close 2026-04 as manager-1").click()
        check("April closed by a different label", "Closed." in flash(), flash()[:160])
        shot("03_april_closed")
        april_csv = page.request.get(base + "/exports/2026-04/statement.csv").body()

        # 3. statement for the split + threshold crossing, and the evidence chain
        go("/periods/2026-04/statements/REP-CEDAR/USD")
        body = page.inner_text("main")
        check("Cedar split credit 7,407.40 (60% of 12,345.67)", "7,407.40" in body)
        check("Cedar expected payout USD 692.59", "692.59" in body)
        shot("04_statement_cedar_split_crossing")
        go("/periods/2026-04/lines/EARNING:E-2026-0405:REP-CEDAR")
        body = page.inner_text("main")
        check("evidence shows bracket portions 2,592.60 @5% and 2,407.40 @8%", "2,592.60" in body and "2,407.40" in body)
        shot("05_line_evidence_threshold_crossing")

        # 4. stage 2: late data after close
        go("/")
        page.get_by_role("button", name="Load late-arriving data (stage 2)").click()
        body = page.inner_text("main")
        check("May shows the late April receipt as a blocking hold", "LATE_EVENT_PENDING" in body)
        act("analyst-1")
        page.get_by_role("button", name="Calculate draft").click()
        shot("06_may_late_hold_blocks_close")
        submit_disabled = page.get_by_role("button", name="Submit for review as analyst-1").is_disabled()
        check("submit is disabled while a blocking hold exists", submit_disabled)
        page.fill("input[name=reason]", "Bank confirmed the 29 April receipt; April is closed, post in May")
        page.get_by_role("button", name="Post as prior-period adjustment in 2026-05").click()
        check("late receipt posted with a reason", "Decision recorded" in flash())
        page.get_by_role("button", name="Recalculate").click()
        page.get_by_role("button", name="Submit for review as analyst-1").click()
        act("manager-1")
        page.get_by_role("button", name="Close 2026-05 as manager-1").click()
        check("May closed", "Closed." in flash(), flash()[:160])
        shot("07_may_closed")
        april_after = page.request.get(base + "/exports/2026-04/statement.csv").body()
        check("April export byte-identical after plan v2, late data and May close", april_csv == april_after)

        go("/periods/2026-05/lines/CLAWBACK:E-2026-0502:REP-AURORA")
        body = page.inner_text("main")
        check("partial refund claws back -80.00 against the original 8% line", "SGD −80.00" in body)
        shot("08_clawback_evidence")

        # 5. variance
        go("/variance?period=2026-05")
        act("ops-1")
        page.get_by_role("button", name="Refresh cases for 2026-05").click()
        body = page.inner_text("main")
        check("May variances +80.00 and -120.00 visible", "+80.00" in body and "−120.00" in body)
        shot("09_variance_may")
        page.get_by_role("link", name="VC-0002").click()
        body = page.inner_text("main")
        check("case suggests MISSED_CLAWBACK exactly", "MISSED_CLAWBACK" in body)
        page.fill("#owner", "ops-1")
        page.select_option("#rc", "MISSED_CLAWBACK")
        page.select_option("#stt", "RESOLVED")
        page.fill("#rn", "Register paid 590.00; the -80.00 clawback for E-2026-0502 was not applied.")
        page.get_by_role("button", name="Save as ops-1").click()
        check("case resolved (explained, not paid)", "Resolved" in page.inner_text("main"))
        shot("10_case_resolved")

        for path in ["/imports", "/imports/6", "/quarantine?status=ALL", "/policy", "/contracts/C-1003",
                     "/events/E-2026-0431", "/variance?period=2026-04", "/periods/2026-05"]:
            go(path)
        go("/audit")
        shot("11_audit")
        go("/imports/6")
        shot("12_import_batch_control_totals")

        # 6. layout: no horizontal page overflow at desktop and phone widths
        overflow = []
        for vw, vh in ((1440, 900), (390, 844)):
            page.set_viewport_size({"width": vw, "height": vh})
            for path in sorted(visited):
                page.goto(base + path)
                sw = page.evaluate("document.documentElement.scrollWidth")
                if sw > vw + 1:
                    overflow.append(f"{path} @{vw}px scrollWidth={sw}")
        check("no horizontal page overflow on visited pages (1440px and 390px)", not overflow, "; ".join(overflow[:8]))
        page.set_viewport_size({"width": 390, "height": 844})
        go("/periods/2026-05")
        shot("13_mobile_period_may")
        check("no console errors or CSP violations", not console_errors, "; ".join(console_errors[:5]))
        browser.close()
    server.shutdown()
    result = {
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "environment": {"python": platform.python_version(), "platform": platform.platform(),
                        "browser": "Playwright Chromium (headless)"},
        "pages_visited": sorted(visited), "checks": checks, "passed": sum(c["passed"] for c in checks),
        "failed": sum(not c["passed"] for c in checks), "seconds": round(time.time() - t0, 1),
    }
    RESULTS.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\n{result['passed']} passed, {result['failed']} failed in {result['seconds']}s -> {RESULTS}")
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

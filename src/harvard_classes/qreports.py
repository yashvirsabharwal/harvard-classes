"""Scrape FAS Q Reports (course evaluations) into SQLite.

    uv run qreports                                  # every term students can see
    uv run qreports --terms "2025 Fall" --limit 20   # quick test
    uv run qreports --list-only                      # just record which reports exist
    uv run qreports --reparse                        # re-parse stored pages, no network

Sign-in: if there is no valid HarvardKey session saved in .auth/state.json, a
Chrome window opens for you to sign in, then closes. The session only lasts a
few hours; if it expires mid-run the window reopens and the run continues
once you sign in (or exits after 15 minutes, resumable). Everything else runs
headless as plain HTTP requests with that session's cookies; no pages are
rendered. Runs resume: reports already downloaded are skipped.
"""

import argparse
import asyncio
import json
import re
import sys
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from playwright.async_api import BrowserContext
from playwright.async_api import Error as PWError
from playwright.async_api import async_playwright

from . import db
from .qparse import parse_report
from .session import SessionExpired, get_html, log, new_context, save_state, sign_in

QR = "https://qreports.fas.harvard.edu"
BROWSE = f"{QR}/browse/index"
MAX_SESSIONS = 5  # sign-ins per run before giving up
TERM_RE = re.compile(r"^\d{4} (Fall|Spring|Summer|Winter)$")

# "AFRAMER 10-Introduction to African American Studies 001 (McCarthy)"
LINK_RE = re.compile(
    r"^(?P<subject>[A-Z][A-Z&\-]*(?: [A-Z&\-]+)*?) (?P<catalog>\S+?)-(?P<title>.*) (?P<section>\S+)\s*\((?P<instructor>.*)\)$"
)


def squash(s: str) -> str:
    return " ".join(s.split())


# --- browse pages -------------------------------------------------------------

def parse_link(link: dict, term: str) -> dict:
    # link id: <school>-<course_id>-<strm>-<n>-<n>-<section>
    school, course_id, strm, *_ = link["id"].split("-", 5)
    m = LINK_RE.match(link["text"])
    parts = m.groupdict() if m else {"subject": None, "catalog": None, "title": link["text"], "section": None, "instructor": None}
    return {
        "report_key": f"{link['id']}|{parts['instructor']}",
        "url": link["href"],
        "link_id": link["id"],
        "school": school,
        "course_id": int(course_id) if course_id.isdigit() else None,
        "strm": strm,
        "term": term,
        "department": link["department"],
        "subject": parts["subject"],
        "catalog_number": parts["catalog"],
        "title": parts["title"],
        "section": parts["section"],
        "instructor_short": parts["instructor"],
    }


def parse_browse(html: str, base_url: str) -> tuple[dict[str, str], list[dict]]:
    """Term links ({label: url}) and report links on a browse page."""
    soup = BeautifulSoup(html, "lxml")
    terms = {}
    for a in soup.find_all("a", href=True):
        label = squash(a.get_text())
        if TERM_RE.match(label):
            terms.setdefault(label, urljoin(base_url, a["href"]))
    links = []
    for a in soup.select("#bluecourses a[id][href]"):
        card = a.find_parent(class_="card")
        header = card.select_one(".card-header") if card else None
        links.append({"id": a["id"], "href": urljoin(base_url, a["href"]), "text": squash(a.get_text()),
                      "department": squash(header.get_text()) if header else None})
    return terms, links


def record_links(conn, rows: list[dict]) -> None:
    # Refresh browse metadata (and the possibly-rotated URL) without touching parsed columns.
    for row in rows:
        cols = list(row)
        conn.execute(
            f"INSERT INTO qr_reports ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
            f"ON CONFLICT(report_key) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in cols[1:])}",
            list(row.values()),
        )
    conn.commit()


# --- session ------------------------------------------------------------------

async def sign_in_qreports(p) -> None:
    await sign_in(p, f"{QR}/home/index", done_selector="#bluecourses")  # /home/index starts HarvardKey sign-in


async def open_session(p, force_sign_in: bool = False):
    """Headless browser context with a working Q Reports session, signing in if needed."""
    if force_sign_in:
        await sign_in_qreports(p)
    for _ in range(2):
        browser, ctx = await new_context(p)
        try:
            html = await get_html(ctx, BROWSE)
            if 'id="bluecourses"' in html:
                return browser, ctx, html
        except SessionExpired:
            pass
        await browser.close()
        await sign_in_qreports(p)
    sys.exit("Could not get a signed-in Q Reports session.")


async def warm_up_reports(ctx: BrowserContext, url: str) -> None:
    # The first visit to the report site does a single sign-on handshake that
    # may need JavaScript, so load one report in a (headless) page.
    page = await ctx.new_page()
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        await page.wait_for_selector("h3.ReportBlockTitle", timeout=20000)
    except PWError:
        pass
    await page.close()
    await save_state(ctx)


# --- listing and fetching -----------------------------------------------------

async def list_reports(ctx: BrowserContext, conn, first_html: str, wanted_terms: set[str] | None) -> list[str]:
    terms, _ = parse_browse(first_html, BROWSE)
    log(f"terms available: {', '.join(terms)}")
    if wanted_terms:
        missing = wanted_terms - set(terms)
        if missing:
            log(f"not available to you, skipping: {', '.join(sorted(missing))}")
        terms = {k: v for k, v in terms.items() if k in wanted_terms}
    keys = []
    for label, href in terms.items():
        _, links = parse_browse(await get_html(ctx, href), href)
        rows = {}
        for link in links:
            row = parse_link(link, label)
            rows.setdefault(row["report_key"], row)  # cross-listed courses repeat; first department wins
        record_links(conn, list(rows.values()))
        keys += rows
        log(f"  {label}: {len(rows)} reports")
    return keys


def classify(html: str, row) -> str:
    """'ok' if the page is the report we asked for, 'mismatch' if it is a different
    report, 'invalid' if it has no report content (e.g. the session went stale and
    only the table of contents came back)."""
    heads = [squash(h.get_text(" ")) for h in BeautifulSoup(html, "lxml").select("h3")]
    course = next((h.removeprefix("Course Feedback for ") for h in heads if h.startswith("Course Feedback for ")), None)
    instructor = next((h.removeprefix("Instructor Feedback for ") for h in heads if h.startswith("Instructor Feedback for ")), "")
    if course is None:
        return "invalid"
    if not row["subject"]:
        return "ok"
    # Cross-listed courses share one report ("APMTH 115, ENG-SCI 115"); some
    # courses (e.g. language tutorials) have no instructor section at all.
    surname = (row["instructor_short"] or "").split()[-1:] or [""]
    code_ok = f"{row['subject']} {row['catalog_number']}" in course.split(", ")
    return "ok" if code_ok and (not instructor or surname[0].lower() in instructor.lower()) else "mismatch"


def matches(html: str, row) -> bool:
    return classify(html, row) == "ok"


STALE_AFTER = 10  # consecutive content-less pages before assuming the session expired


async def fetch_all(ctx: BrowserContext, conn, keys: list[str], workers: int) -> list[str]:
    """Fetch reports concurrently; returns keys whose page didn't match (retried serially later)."""
    queue: asyncio.Queue = asyncio.Queue()
    for k in keys:
        queue.put_nowait(k)
    total, done, failed, mismatched = len(keys), 0, 0, []
    invalid_streak = 0

    async def worker() -> None:
        nonlocal done, failed, invalid_streak
        while not queue.empty() and invalid_streak < STALE_AFTER:
            key = queue.get_nowait()
            row = conn.execute("SELECT * FROM qr_reports WHERE report_key = ?", (key,)).fetchone()
            try:
                html = await get_html(ctx, row["url"])
            except PWError as e:  # not stored, so the next run retries it
                failed += 1
                conn.execute("UPDATE qr_reports SET parse_status = 'error' WHERE report_key = ?", (key,))
                log(f"  failed {key}: {e.message.splitlines()[0]}")
                html = None
            kind = classify(html, row) if html is not None else None
            invalid_streak = invalid_streak + 1 if kind == "invalid" else 0
            if kind == "ok":
                store(conn, key, html)
            elif kind == "mismatch":
                mismatched.append(key)
            done += 1
            if done % 100 == 0 or done == total:
                conn.commit()
                log(f"  fetched {done}/{total} ({len(mismatched)} to re-check, {failed} failed)")

    await asyncio.gather(*(worker() for _ in range(workers)))
    conn.commit()
    if invalid_streak >= STALE_AFTER:
        raise SessionExpired()
    return mismatched


async def recheck(ctx: BrowserContext, conn, keys: list[str]) -> None:
    """One at a time, so nothing can interfere; accept what comes back (e.g. cross-listed codes)."""
    if keys:
        log(f"re-checking {len(keys)} reports one at a time")
    for key in keys:
        row = conn.execute("SELECT * FROM qr_reports WHERE report_key = ?", (key,)).fetchone()
        html = await get_html(ctx, row["url"])
        kind = classify(html, row)
        if kind == "invalid":
            log(f"  {key}: no report content; skipped (next run retries it)")
            continue
        store(conn, key, html)
        if kind == "mismatch":
            log(f"  {key}: report title differs from browse listing (kept; check if suspicious)")
    conn.commit()


def store(conn, key: str, html: str) -> None:
    db.save_raw(conn, f"qr:{key}", "qr_report", html)
    try:
        parse_one(conn, key, html)
    except Exception as e:  # keep the raw page; --reparse can retry after a parser fix
        conn.execute("UPDATE qr_reports SET parse_status = 'error' WHERE report_key = ?", (key,))
        log(f"  parse failed {key}: {e!r}")


def parse_one(conn, key: str, html: str) -> None:
    r = parse_report(html)
    summary = {k: r[k] for k in (
        "instructor", "course_codes", "responded", "invited", "response_rate", "course_overall", "course_overall_fas",
        "instructor_overall", "instructor_overall_fas", "workload_mean", "workload_median", "workload_sd",
        "workload_n", "recommend_mean", "recommend_n")}
    summary.update(parse_status=r["status"], parsed_at=db.now())
    conn.execute(
        f"UPDATE qr_reports SET {', '.join(f'{k} = ?' for k in summary)} WHERE report_key = ?",
        [*summary.values(), key],
    )
    conn.execute("DELETE FROM qr_questions WHERE report_key = ?", (key,))
    conn.execute("DELETE FROM qr_comments WHERE report_key = ?", (key,))
    for q in r["questions"]:
        db.upsert(conn, "qr_questions", {
            "report_key": key, "section": q["section"], "question": q["question"], "kind": q["kind"],
            "count": q.get("count"), "mean": q.get("mean"), "fas_mean": q.get("fas_mean"),
            "median": q.get("median"), "sd": q.get("sd"), "distribution": json.dumps(q.get("distribution")),
        })
    for i, c in enumerate(r["comments"]):
        db.upsert(conn, "qr_comments", {"report_key": key, "question": c["question"], "idx": i, "comment": c["comment"]})


def reparse(conn) -> None:
    rows = conn.execute("SELECT url FROM raw_pages WHERE kind = 'qr_report'").fetchall()
    for row in rows:
        parse_one(conn, row["url"].removeprefix("qr:"), db.load_raw(conn, row["url"]))
    conn.commit()
    log(f"re-parsed {len(rows)} reports")


async def run(args) -> None:
    conn = db.connect(args.db)
    if args.reparse:
        reparse(conn)
        return
    wanted = {t.strip() for t in args.terms.split(",")} if args.terms else None
    async with async_playwright() as p:
        keys, refresh = None, args.refresh
        for session_no in range(MAX_SESSIONS):
            browser, ctx, first_html = await open_session(p, force_sign_in=session_no > 0)
            try:
                if keys is None:
                    keys = await list_reports(ctx, conn, first_html, wanted)
                if args.list_only:
                    break
                todo = keys if refresh else [k for k in keys if not db.has_raw(conn, f"qr:{k}")]
                if args.limit:
                    todo = todo[: args.limit]
                log(f"{len(keys)} reports listed, {len(todo)} to download with {args.workers} parallel requests")
                if todo:
                    first = conn.execute("SELECT url FROM qr_reports WHERE report_key = ?", (todo[0],)).fetchone()
                    await warm_up_reports(ctx, first["url"])
                    await recheck(ctx, conn, await fetch_all(ctx, conn, todo, args.workers))
                break
            except SessionExpired:
                # Reports already stored are kept; after signing in again only the rest are fetched.
                refresh = False
                log("HarvardKey session expired; opening the sign-in window to continue...")
            finally:
                conn.commit()
                await browser.close()
    status = dict(conn.execute("SELECT coalesce(parse_status, 'not fetched'), count(*) FROM qr_reports GROUP BY 1").fetchall())
    log(f"done: {status}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--terms", help='comma-separated, e.g. "2025 Fall,2026 Spring" (default: all available)')
    ap.add_argument("--limit", type=int, help="download at most this many reports this run")
    ap.add_argument("--workers", type=int, default=8, help="parallel HTTP requests (the server slows down past ~8-10)")
    ap.add_argument("--list-only", action="store_true", help="record available reports without downloading")
    ap.add_argument("--refresh", action="store_true", help="re-download reports already stored")
    ap.add_argument("--reparse", action="store_true", help="only re-parse stored reports")
    asyncio.run(run(ap.parse_args()))


if __name__ == "__main__":
    main()

"""Scrape the public my.harvard course catalog into SQLite.

    uv run catalog                    # all FAS courses in the current catalog
    uv run catalog --max-pages 2      # quick test: first 30 courses
    uv run catalog --reparse          # re-parse stored pages, no network

No login is needed. Search results come from the JSON endpoint behind
my.harvard.edu's course search; each result links to a course detail page,
which has the fields we keep (Gen Ed, divisional, prereqs, ...).
"""

import argparse
import json
import math
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import db

BASE = "https://my.harvard.edu"
USER_AGENT = "harvard-classes/0.1 (personal course planning scraper)"
PER_PAGE = 15
COURSE_PATH = re.compile(r"^/course/[^/]+/[^/]+/[^/]+/[^/]+$")
TIME_RE = re.compile(r"\d{1,2}:\d{2}\s*[ap]m", re.I)

_local = threading.local()


def session() -> requests.Session:
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        retry = Retry(total=5, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
        s.mount("https://", HTTPAdapter(max_retries=retry))
        _local.s = s
    return _local.s


def get(url: str, delay: float, attempts: int = 4) -> requests.Response:
    # urllib3's Retry covers status codes and connect errors; this also covers
    # read timeouts while downloading the body, which my.harvard does occasionally.
    for attempt in range(attempts):
        time.sleep(delay if attempt == 0 else 2 ** attempt)
        try:
            r = session().get(url, timeout=(10, 20))  # (connect, read)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == attempts - 1:
                raise


def text(el) -> str | None:
    return " ".join(el.get_text(" ", strip=True).split()) if el else None


# --- search pages -> detail URLs -------------------------------------------

def search_url(school: str, term: str, page: int) -> str:
    return f"{BASE}/search/?q=&school={school}&term={term}&sort=subject_catalog&page={page}"


def card_links(hits_html: str, delay: float) -> list[str]:
    """Detail-page paths for every course card, expanding multi-section cards."""
    soup = BeautifulSoup(hits_html, "lxml")
    paths = []
    for card in soup.select("div[data-course-id]"):
        found = [a["href"] for a in card.select("a[href]") if COURSE_PATH.match(a["href"])]
        if not found:
            btn = card.select_one("button[data-url^='/course/sections/']")
            if btn:
                html = get(BASE + btn["data-url"], delay).json()["html"]
                found = re.findall(r'href="(/course/[^"]+)"', html)
                found = [p for p in found if COURSE_PATH.match(p)]
        paths.extend(found)
    return list(dict.fromkeys(paths))


def enumerate_courses(school: str, term: str, max_pages: int | None, workers: int, delay: float) -> list[str]:
    first = get(search_url(school, term, 1), delay).json()
    pages = math.ceil(first["total_hits"] / PER_PAGE)
    if max_pages:
        pages = min(pages, max_pages)
    print(f"{first['total_hits']} courses in {school}/{term}; reading {pages} search pages", file=sys.stderr)

    def page_links(page: int) -> list[str]:
        data = first if page == 1 else get(search_url(school, term, page), delay).json()
        return card_links(data["hits"], delay)

    paths = []
    with ThreadPoolExecutor(workers) as pool:
        for i, links in enumerate(pool.map(page_links, range(1, pages + 1)), 1):
            paths.extend(links)
            if i % 25 == 0:
                print(f"  search page {i}/{pages}", file=sys.stderr)
    return [BASE + p for p in dict.fromkeys(paths)]


# --- detail page parsing ----------------------------------------------------

def parse_course(url: str, html: str) -> dict:
    # Everything we keep sits between these two comments; parsing just that
    # slice (instead of the whole 135 KB page) is several times faster.
    start, end = html.find("<!-- ========== Page Heading"), html.find("<!-- Credit Conversion")
    soup = BeautifulSoup(html[start:end] if 0 <= start < end else html, "lxml")
    _, _, term, session_name, section = url.removeprefix(BASE).strip("/").split("/")
    subject_catalog = text(soup.select_one("#course-sub-cat span")) or ""
    # Multi-section courses show the section too ("SPANSH 40 001").
    subject_catalog = subject_catalog.removesuffix(f" {section}")
    subject, _, catalog_number = subject_catalog.rpartition(" ")

    instructors = []
    for a in soup.select("#course-instructor a[href^='/instructor/']"):
        spans = a.find_all("span", recursive=False)
        instructors.append({"name": text(spans[1]) if len(spans) > 1 else text(a), "id": a["href"].rsplit("/", 1)[-1]})

    time_box = soup.select_one("#course-time")
    spans = [text(s) for s in time_box.select("span")] if time_box else []
    days = [d.get_text(strip=True) for d in time_box.select("[aria-label$='selected']")] if time_box else []
    times = [s for s in spans if TIME_RE.search(s)]
    dates = [s for s in spans if "–" in s and re.search(r"\d{4}", s)]

    fields = {}
    for div in soup.select("#course-info > div"):
        strings = list(div.stripped_strings)
        if strings and strings[0].endswith(":"):
            fields[strings[0].rstrip(":")] = " ".join(strings[1:])
    for strong in soup.select("#course-additional-info strong"):
        parts = list(strong.parent.stripped_strings)[1:]
        fields[strong.get_text(strip=True)] = parts

    sections = {}
    for h3 in soup.select("div[id] > h3"):
        parent = h3.parent
        body = [s for s in parent.stripped_strings][1:]
        sections[h3.get_text(strip=True)] = "\n".join(body)

    joint = []
    for strong in soup.find_all("strong", string=re.compile("Jointly Offered")):
        joint += [text(a) for a in strong.parent.select("a")]

    def field(name: str, first_only: bool = False) -> str | None:
        v = fields.get(name)
        if isinstance(v, list):
            v = v[:1] if first_only else v
            return "; ".join(v) or None
        return v

    course_id = fields.get("Course ID")
    return {
        "url": url,
        "course_id": int(course_id) if course_id and course_id.isdigit() else None,
        "subject_catalog": subject_catalog,
        "subject": subject,
        "catalog_number": catalog_number,
        "title": text(soup.select_one("#course-title")),
        "term": term.replace("-", " "),
        "session": session_name.replace("-", " "),
        "section": section,
        "class_number": fields.get("Class Number"),
        "instructors": json.dumps(instructors),
        "days": ",".join(dict.fromkeys(days)) or None,
        "times": "; ".join(dict.fromkeys(times)) or None,
        "dates": "; ".join(dict.fromkeys(dates)) or None,
        "campus": text(soup.select_one("#course-location .font-medium")),
        "consent": fields.get("Consent"),
        "enrollment": fields.get("Enrollment"),
        "school": field("School"),
        "credits": field("Credits"),
        "cross_reg": field("Cross Reg"),
        "department": field("Department"),
        "component": field("Course Component", first_only=True),
        "subject_name": field("Subject"),
        "grading_basis": field("Grading Basis"),
        "course_requirements": field("Course Requirements"),
        "exam_deadline": field("Exam/Final Deadline"),
        "gen_ed": field("General Education"),
        "qrd": field("Quantitative Reasoning with Data"),
        "divisional": field("Divisional Distribution"),
        "course_level": field("Course Level"),
        "description": sections.get("Description"),
        "course_notes": sections.get("Course Notes"),
        "class_notes": sections.get("Class Notes"),
        "recommended_prep": sections.get("Recommended Prep"),
        "jointly_offered": json.dumps(joint),
        "fields": json.dumps(fields),
        "parsed_at": db.now(),
    }


def parse_all(conn) -> int:
    urls = [r["url"] for r in conn.execute("SELECT url FROM raw_pages WHERE kind = 'catalog_course'")]
    failed = 0
    for url in urls:
        try:
            db.upsert(conn, "offerings", parse_course(url, db.load_raw(conn, url)))
        except Exception as e:  # keep going; one odd page shouldn't stop the run
            failed += 1
            print(f"parse failed {url}: {e!r}", file=sys.stderr)
    conn.commit()
    print(f"parsed {len(urls) - failed} course pages ({failed} failed)", file=sys.stderr)
    return failed


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--school", default="FAS", help="FAS, HKS, GSD, ... or All")
    ap.add_argument("--term", default="All", help="term filter as used by my.harvard (default All)")
    ap.add_argument("--max-pages", type=int, help="only read this many search pages (15 courses each)")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.25, help="seconds to wait before each request, per worker")
    ap.add_argument("--refresh", action="store_true", help="re-download detail pages already stored")
    ap.add_argument("--reparse", action="store_true", help="only re-parse stored pages")
    args = ap.parse_args()

    conn = db.connect(args.db)
    if not args.reparse:
        urls = enumerate_courses(args.school, args.term, args.max_pages, args.workers, args.delay)
        todo = urls if args.refresh else [u for u in urls if not db.has_raw(conn, u)]
        print(f"{len(urls)} course pages found, {len(todo)} to download", file=sys.stderr)

        def fetch(url: str):
            try:
                return url, get(url, args.delay).text
            except requests.RequestException as e:
                print(f"fetch failed {url}: {e!r}", file=sys.stderr)
                return url, None

        # Save pages as they finish (not in submission order) so one slow page
        # doesn't hold back the rest, and commit often so the lock is short.
        started, failed = time.time(), 0
        with ThreadPoolExecutor(args.workers) as pool:
            futures = [pool.submit(fetch, u) for u in todo]
            for i, fut in enumerate(as_completed(futures), 1):
                url, html = fut.result()
                if html is None:
                    failed += 1
                else:
                    db.save_raw(conn, url, "catalog_course", html)
                if i % 25 == 0 or i == len(todo):
                    conn.commit()
                if i % 250 == 0 or i == len(todo):
                    rate = i / (time.time() - started)
                    print(f"  downloaded {i}/{len(todo)} ({rate:.1f}/s, {failed} failed)", file=sys.stderr)
        if failed:
            print(f"{failed} pages failed; run again to retry just those", file=sys.stderr)
    parse_all(conn)


if __name__ == "__main__":
    main()

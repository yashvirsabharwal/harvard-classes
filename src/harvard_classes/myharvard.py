"""Import your own record from my.harvard: courses taken, the degree audit, and
the official course list for each open requirement.

    uv run import-me            # sign in if needed, download, parse
    uv run import-me --reparse  # re-parse the stored pages (no network)

There is no public API for student records. This uses the same signed-in pages
my.harvard does: /student/grades (HTML), /student/academic-progress/display
(JSON wrapping the Academic Advising Report's HTML), and
/search/student-requirements/<school> ("Search by My Requirements": each open
requirement as a course-search query built from the registrar's course lists).
Those queries are then run against the public course search for each catalog
term. Uses the HarvardKey session in .auth/state.json, opening a sign-in
window only if needed.
"""

import argparse
import asyncio
import json
import re
from concurrent.futures import ThreadPoolExecutor

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from . import db
from .catalog import query_courses
from .session import SessionExpired, get_html, log, new_context, sign_in

MYH = "https://my.harvard.edu"
GRADES = f"{MYH}/student/grades"
AUDIT = f"{MYH}/student/academic-progress/display"
OFFICIAL = f"{MYH}/search/student-requirements/"  # + school code(s), e.g. HCOL

TERM_RE = re.compile(r"^20\d\d (Fall|Spring|Summer|Winter|January)$")
CODE_RE = re.compile(r"[A-Z][A-Z&\-]*(?: [A-Z&\-]+)*? (?:\d\w*|[A-Z]{1,3})$")  # "NEURO 80", "MATH MB"
FINAL_GRADE = re.compile(r"^(A|A-|[BCD][+-]?|E|F|SAT|UNS|PA|FL|INC|W|WD|EXT|CR|NCR)$")
UNITS_RE = re.compile(r"Units:\s*([\d.]+) required,\s*([\d.]+) taken,\s*([\d.]+) needed")
STATUS = {"Satisfied": "satisfied", "Not Satisfied": "not_satisfied", "In Progress": "in_progress"}


def squash(s: str) -> str:
    return " ".join(s.split())


def split_code(text: str) -> tuple[str, str | None]:
    """'Neurobiology of Behavior NEURO 80' -> ('Neurobiology of Behavior', 'NEURO 80')."""
    m = CODE_RE.search(text)
    return (text[: m.start()].strip(), m.group()) if m else (text, None)


# --- /student/grades ----------------------------------------------------------

def soup_without_tooltips(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "lxml")
    for el in soup.select('[role="tooltip"], script, style, svg'):
        el.decompose()
    return soup


def parse_grades(html: str) -> tuple[dict, list[dict]]:
    soup = soup_without_tooltips(html)
    main = soup.find("main") or soup
    strings = [squash(s) for s in main.stripped_strings]

    profile = {}
    for label, key in (("Current Level", "level"), ("Expected Graduation Term", "expected_graduation")):
        if label in strings:
            profile[key] = strings[strings.index(label) + 1]
    plans = [s.removeprefix("Plan:").strip() or strings[i + 1] for i, s in enumerate(strings) if s.startswith("Plan:")]
    if plans:
        profile["plan"] = plans[0]  # most recent term first

    courses = []
    for h4 in main.find_all("h4"):
        title, code = split_code(squash(h4.get_text(" ")))
        if not code:
            continue
        term_el = h4.find_previous(string=lambda s: bool(TERM_RE.match(s.strip())))
        # The row's cells follow the heading as "Label:", "value" pairs (labels are
        # visually hidden); in-progress courses have no "Grade:" pair.
        tokens = []
        for s in h4.find_all_next(string=True):
            owner = s.find_parent("h4")
            if owner is h4:
                continue
            if owner is not None:
                break  # next course
            if t := squash(s):
                tokens.append(t)
        fields = {tokens[i].rstrip(":"): tokens[i + 1] for i in range(len(tokens) - 1) if tokens[i].endswith(":")}
        grade, credits = fields.get("Grade"), fields.get("Credits")
        done = bool(grade and FINAL_GRADE.match(grade))
        courses.append({
            "code": code, "term": term_el.strip() if term_el else None, "title": title,
            "instructor": fields.get("Instructor"),
            "credits": float(credits) if credits and re.fullmatch(r"[\d.]+", credits) else None,
            "grading_basis": fields.get("Grading Basis"), "grade": grade if done else None,
            "status": "taken" if done else "in_progress",
        })
    return profile, courses


# --- /student/academic-progress/display ---------------------------------------

def parse_audit(fragment: str) -> tuple[dict, list[dict]]:
    soup = soup_without_tooltips(fragment)
    profile = {}
    text = squash(soup.get_text(" "))
    if m := re.search(r"Concentration: ([^:]+?): ([^.]+?) Track", text):
        profile["concentration"], profile["track"] = m.group(1).strip(), m.group(2).strip()

    def own(el, line) -> bool:
        return el.find_parent(class_="req-line") is line

    reqs = []
    for line in soup.select(".req-line"):
        heading = next((h for h in line.find_all(["h3", "h4"]) if own(h, line)), None)
        if heading is None:
            continue
        badge = next((b for b in line.select(".badge") if own(b, line) and squash(b.get_text()) in STATUS), None)
        units = next((UNITS_RE.search(squash(s)) for s in line.find_all(string=UNITS_RE) if own(s.parent, line)), None)
        codes = []
        for table in line.find_all("table"):
            if not own(table, line):
                continue
            for row in table.select("tbody tr"):
                cells = row.find_all("td")
                if cells and (code := split_code(squash(cells[0].get_text(" ")))[1]):
                    codes.append(code)
        path = []
        for parent in line.find_parents(class_=re.compile("hs-accordion")):
            h = parent.find(["h2", "h3", "h4"])
            if h is not None and h is not heading:
                path.append(squash(h.get_text(" ")))
        reqs.append({
            "path": " > ".join(reversed(path)),
            "title": squash(heading.get_text(" ")),
            "status": STATUS.get(squash(badge.get_text())) if badge else None,
            "units_required": float(units.group(1)) if units else None,
            "units_taken": float(units.group(2)) if units else None,
            "units_needed": float(units.group(3)) if units else None,
            "courses": json.dumps(codes),
        })
    return profile, reqs


# --- /search/student-requirements ("Search by My Requirements") --------------

def parse_official(json_text: str) -> list[dict]:
    soup = soup_without_tooltips(json.loads(json_text)["html"])
    sections = {b.get("data-hs-tab", "").lstrip("#"): squash(b.get_text(" "))
                for b in soup.select("button[id^=sbr-panel-req-item-]")}
    items = []
    for el in dict.fromkeys(soup.select("a[data-search-text], .not-available")):  # in page order, once each
        heading = el.find("h3")
        panel = el.find_parent(id=re.compile(r"^sbr-panel-req-"))
        query = el.get("data-search-text")
        available = bool(query) and "not-available" not in el.get("class", [])
        items.append({
            "section": sections.get(panel["id"]) if panel else None,
            "title": squash(heading.get_text(" ")) if heading else squash(el.get_text(" ")),
            "query": query, "available": int(available),
            "reason": None if available else (el.get("data-reason") or "Not available in Search by My Requirements"),
        })
    return items


def fetch_official_courses(conn) -> None:
    """Run each official requirement query against the public course search, per catalog term."""
    terms = [t for (t,) in conn.execute("SELECT DISTINCT term FROM offerings")]
    reqs = conn.execute("SELECT title, query FROM me_official_requirements WHERE available = 1").fetchall()
    if not terms:
        log("no catalog yet (uv run catalog); skipping official course lists")
        return
    jobs = [(r["title"], r["query"], t) for r in reqs for t in terms]
    with ThreadPoolExecutor(3) as pool:  # each query also fetches its result pages in parallel
        results = list(pool.map(lambda j: (j[0], j[2], query_courses(j[1], j[2])), jobs))
    conn.execute("DELETE FROM me_official_courses")
    for title, term, codes in results:
        conn.executemany("INSERT OR IGNORE INTO me_official_courses (title, term, code) VALUES (?, ?, ?)",
                         [(title, term, c) for c in codes])
    conn.commit()
    log(f"official course lists: {len(reqs)} requirements x {len(terms)} terms, "
        f"{sum(len(c) for *_, c in results)} matches")


# --- storing ------------------------------------------------------------------

def store(conn, grades_html: str, audit_json: str, official_json: str | None) -> None:
    profile, courses = parse_grades(grades_html)
    audit_profile, reqs = parse_audit(json.loads(audit_json)["requirements"])
    profile |= audit_profile
    official = parse_official(official_json) if official_json else []
    conn.execute("DELETE FROM me_profile")
    conn.execute("DELETE FROM me_courses")
    conn.execute("DELETE FROM me_requirements")
    conn.execute("DELETE FROM me_official_requirements")
    for i, r in enumerate(official):
        db.upsert(conn, "me_official_requirements", {"idx": i, **r})
    for k, v in profile.items():
        db.upsert(conn, "me_profile", {"key": k, "value": v})
    for c in courses:
        db.upsert(conn, "me_courses", c)
    for i, r in enumerate(reqs):
        db.upsert(conn, "me_requirements", {"idx": i, **r})
    conn.commit()
    log(f"profile: {profile}")
    log(f"{len(courses)} courses ({sum(c['status'] == 'taken' for c in courses)} taken), {len(reqs)} requirement lines "
        f"({sum(r['status'] == 'not_satisfied' for r in reqs)} not satisfied), {len(official)} official requirement queries "
        f"({sum(not r['available'] for r in official)} not searchable)")


async def download(conn) -> None:
    async with async_playwright() as p:
        for attempt in range(2):
            browser, ctx = await new_context(p)
            try:
                grades, audit, home = await asyncio.gather(  # the audit is slow (generated on request)
                    get_html(ctx, GRADES), get_html(ctx, AUDIT), get_html(ctx, f"{MYH}/"))
                json.loads(audit)
                tab = BeautifulSoup(home, "lxml").select_one('[data-search-mode="sbr"][data-schools]')
                official = await get_html(ctx, OFFICIAL + (tab["data-schools"] if tab else "HCOL"))
                json.loads(official)
                break
            except (SessionExpired, json.JSONDecodeError):
                if attempt == 1:
                    raise SystemExit("Could not read my.harvard with this session.")
                await browser.close()
                await sign_in(p, f"{MYH}/login", done_url=re.compile(r"my\.harvard\.edu/student"))
        await browser.close()
    db.save_raw(conn, GRADES, "me_grades", grades)
    db.save_raw(conn, AUDIT, "me_audit", audit)
    db.save_raw(conn, OFFICIAL, "me_official", official)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--reparse", action="store_true", help="re-parse the stored pages without downloading")
    args = ap.parse_args()
    conn = db.connect(args.db)
    if not args.reparse:
        asyncio.run(download(conn))
    store(conn, db.load_raw(conn, GRADES), db.load_raw(conn, AUDIT), db.load_raw(conn, OFFICIAL))
    if not args.reparse:
        fetch_official_courses(conn)


if __name__ == "__main__":
    main()

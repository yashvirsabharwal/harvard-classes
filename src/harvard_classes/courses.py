"""Filter next term's courses by the requirements they count toward.

    uv run courses                              # every open requirement in your audit
    uv run courses --req "Ethics"               # one requirement (substring match)
    uv run courses --multi                      # only courses counting toward 2+ kinds of requirement
    uv run courses --all                        # include requirements you've already satisfied
    uv run courses --sort rating                # code (default) | rating | hours

Needs the catalog (uv run catalog), your record (uv run import-me) and,
for the rating/hours columns, Q Reports (uv run qreports). No scores or
recommendations: Q numbers are shown for reference only.

Overlap rules applied when a course counts toward several requirements:
  * Divisional Distribution and QRD can't share a course (Advising Office).
  * Gen Ed courses don't count for divisional or QRD (encoded in the catalog).
  * Divisional and concentration can share a course (e.g. LS 1A in the audit).
  * Whether Gen Ed courses count toward a concentration is up to the
    department; the concentration's rule file lists any that do.
  * One course fills one slot within a concentration.
  * A secondary field and the concentration can share only one course;
    a double concentration can share at most 8 credits (Registrar).
"""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from importlib import resources

from . import db

# Audit lines that aren't about which courses to take.
NOT_COURSE_REQUIREMENTS = re.compile(
    r"Total Credits|C- or better|Letter Graded Credits|Courses Not used|Concentration GPA|^For questions", re.I)
SKIP_TITLES = re.compile(r"Research|Reading Course|Thesis|Tutorial|Independent Study|Directed Study|Supervised Reading", re.I)
GRAD_LEVELS = {"Graduate Course", "Primarily for Graduate Students"}
DIVISIONAL = {"Science and Engineering and Applied Science": "Science & Engineering & Applied Science"}


@dataclass
class Requirement:
    label: str
    group: str                   # gened | divisional | qrd | concentration
    matches: callable
    status: str = "not_satisfied"
    note: str = ""
    names_courses: bool = False  # lists specific courses (vs. open-ended like "any Math")
    needed: float | None = None  # units still needed, per the audit


@dataclass
class Course:
    code: str
    title: str
    course_id: int | None
    instructors: list[str]
    gen_ed: str
    divisional: str
    qrd: str
    level: str
    joint: list[str]
    stats: dict = field(default_factory=dict)
    counts: list[Requirement] = field(default_factory=list)
    needs: str | None = None  # earlier course in a sequence not yet taken, e.g. "MATH 55A"


# --- requirements -------------------------------------------------------------

def load_rules(concentration: str | None, track: str | None) -> dict | None:
    for f in resources.files("harvard_classes.rules").iterdir():
        if f.name.endswith(".json"):
            rules = json.loads(f.read_text())
            name = rules["name"].lower()
            if concentration and concentration.lower() in name and (not track or track.lower() in name):
                return rules
    return None


def code_matcher(patterns: list[str]):
    rx = re.compile(r"^(?:%s)$" % "|".join(patterns))
    return lambda c: bool(rx.match(c.code)) or any(rx.match(j) for j in c.joint)


def requirements(conn, include_satisfied: bool) -> tuple[list[Requirement], list[str]]:
    profile = dict(conn.execute("SELECT key, value FROM me_profile").fetchall())
    rules = load_rules(profile.get("concentration"), profile.get("track"))
    by_title = {r["audit_title"].lower(): r for r in (rules or {}).get("requirements", [])}
    statuses = ("not_satisfied", "in_progress", "satisfied") if include_satisfied else ("not_satisfied",)
    reqs, unmatched = [], []
    for row in conn.execute(
            f"SELECT * FROM me_requirements WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY idx", statuses):
        title, path, status = row["title"], row["path"] or "", row["status"]
        if NOT_COURSE_REQUIREMENTS.search(title):
            continue
        if path.startswith("General Education"):
            reqs.append(Requirement(f"Gen Ed: {title}", "gened", lambda c, t=title: t in c.gen_ed, status))
        elif path.startswith("Divisional"):
            div = DIVISIONAL.get(title, title)
            reqs.append(Requirement(f"Divisional: {title}", "divisional", lambda c, d=div: d in c.divisional, status))
        elif title.startswith("Quantitative Reasoning with Data"):
            reqs.append(Requirement("QRD", "qrd", lambda c: c.qrd == "Yes", status))
        elif rule := by_title.get(title.lower()):
            reqs.append(Requirement(rule["label"], "concentration", code_matcher(rule["courses"]), status,
                                    rule.get("note", ""), names_courses=not rule.get("broad")))
        elif path.startswith("Concentration") and status == "not_satisfied":
            unmatched.append(title)
            continue
        else:
            continue
        reqs[-1].needed = row["units_needed"]
    return reqs, unmatched


def double_counts(reqs: list[Requirement]) -> int:
    """How many kinds of requirement (Gen Ed, divisional, QRD, concentration) one
    course can count toward at once."""
    groups = {r.group for r in reqs}
    if {"divisional", "qrd"} <= groups:
        groups.discard("qrd")  # a course can't count for both
    return len(groups)


def overlap_notes(reqs: list[Requirement]) -> list[str]:
    """Limits on counting one course toward all of these at once."""
    groups = Counter(r.group for r in reqs)
    notes = []
    if groups["divisional"] and groups["qrd"]:
        notes.append("Divisional or QRD, not both")
    if groups["gened"] > 1:
        notes.append("listed in 2 Gen Ed categories; check if it can count for both")
    if groups["divisional"] > 1:
        notes.append("cross-divisional; counts for one division")
    if groups["concentration"] > 1:
        notes.append("fills one concentration slot")
    return notes


# --- Q data (reference only) --------------------------------------------------

def load_reports(conn):
    by_id, by_code = defaultdict(list), defaultdict(list)
    for r in conn.execute(
            "SELECT course_id, course_codes, strm, instructor, responded, course_overall, workload_median "
            "FROM qr_reports WHERE parse_status = 'ok' AND course_overall IS NOT NULL"):
        r = dict(r)
        by_id[r["course_id"]].append(r)
        for code in (r["course_codes"] or "").split(", "):
            by_code[code].append(r)
    return by_id, by_code


def course_stats(reports: list[dict], instructors: list[str]) -> dict:
    """Response-weighted averages over every past report, plus whether next term's
    instructor appears in them."""
    if not reports:
        return {}
    n = sum(r["responded"] for r in reports)
    hours = [(r["workload_median"], r["responded"]) for r in reports if r["workload_median"] is not None]
    teaching = {i.split()[-1].lower() for i in instructors if i}
    return {
        "rating": sum(r["course_overall"] * r["responded"] for r in reports) / n,
        "hours": sum(h * w for h, w in hours) / sum(w for _, w in hours) if hours else None,
        "responses": n,
        "terms": len({r["strm"] for r in reports}),
        "same_instructor": any((r["instructor"] or "").split()[-1:] and r["instructor"].split()[-1].lower() in teaching
                               for r in reports),
    }


# --- courses ------------------------------------------------------------------

def load_courses(conn, term: str) -> list[Course]:
    grouped: dict[str, Course] = {}
    for r in conn.execute("SELECT * FROM offerings WHERE term = ? ORDER BY subject_catalog, section", (term,)):
        c = grouped.get(r["subject_catalog"])
        instructors = [i["name"] for i in json.loads(r["instructors"] or "[]")]
        if c is None:
            grouped[r["subject_catalog"]] = Course(
                code=r["subject_catalog"], title=r["title"] or "", course_id=r["course_id"],
                instructors=instructors, gen_ed=r["gen_ed"] or "", divisional=r["divisional"] or "",
                qrd=r["qrd"] or "", level=r["course_level"] or "", joint=json.loads(r["jointly_offered"] or "[]"))
        else:
            c.instructors += [i for i in instructors if i not in c.instructors]
    return list(grouped.values())


def done_codes(conn) -> set[str]:
    codes = {r["code"] for r in conn.execute("SELECT code FROM me_courses")}
    # Courses can be renumbered or cross-listed: also treat anything sharing a course id as done.
    ids = {r[0] for r in conn.execute(
        f"SELECT DISTINCT course_id FROM offerings WHERE subject_catalog IN ({','.join('?' * len(codes))})", list(codes))}
    for r in conn.execute("SELECT course_id, course_codes FROM qr_reports"):
        if set((r["course_codes"] or "").split(", ")) & codes:
            ids.add(r["course_id"])
    codes |= {r[0] for r in conn.execute(
        f"SELECT DISTINCT subject_catalog FROM offerings WHERE course_id IN ({','.join('?' * len(ids))})", list(ids))}
    return codes


def sequence_prereq(code: str, known: set[str], done: set[str]) -> str | None:
    """'MATH 55B' -> 'MATH 55A' when that course exists and hasn't been taken."""
    if not code.endswith("B"):
        return None
    first = code[:-1] + "A"
    return first if first in known and first not in done else None


def reports_for(c: Course, by_id, by_code) -> list[dict]:
    """Past Q reports for a course: same course id, or any of its codes/cross-listings
    (a cross-listed course's report appears under each listing, so de-duplicate)."""
    seen, reports = set(), []
    for r in by_id.get(c.course_id, []) + [x for code in [c.code, *c.joint] for x in by_code.get(code, [])]:
        key = (r["strm"], r["instructor"], r["responded"], r["course_overall"])
        if key not in seen:
            seen.add(key)
            reports.append(r)
    return reports


def filter_courses(conn, term: str, include_satisfied: bool, include_grad: bool):
    reqs, unmatched = requirements(conn, include_satisfied)
    by_id, by_code = load_reports(conn)
    done = done_codes(conn)
    known = {r[0] for r in conn.execute("SELECT DISTINCT subject_catalog FROM offerings")} | set(by_code)
    courses = []
    for c in load_courses(conn, term):
        if c.code in done or set(c.joint) & done:
            continue
        c.counts = [r for r in reqs if r.matches(c)]
        if not c.counts:
            continue
        named = any(r.names_courses for r in c.counts)  # e.g. LS 100 or BCMP 213 for Advanced Neuro
        if not named and (SKIP_TITLES.search(c.title) or (c.level in GRAD_LEVELS and not include_grad)):
            continue
        c.stats = course_stats(reports_for(c, by_id, by_code), c.instructors)
        c.needs = sequence_prereq(c.code, known, done)
        courses.append(c)
    return reqs, unmatched, courses


# --- output -------------------------------------------------------------------

SORTS = {
    "code": lambda c: c.code,
    "rating": lambda c: -(c.stats.get("rating") or 0),
    "hours": lambda c: c.stats.get("hours") if c.stats.get("hours") is not None else 99,
}


def fmt(c: Course, exclude: Requirement | None = None) -> str:
    s = c.stats
    q = (f"Q {s['rating']:.2f}  {s['hours'] or 0:4.1f}h  n={s['responses']:<4}" + ("" if s["same_instructor"] else " new instr")
         if s else "no Q data")
    others = [r.label for r in c.counts if r is not exclude]
    also = f"  {'also' if exclude else 'counts toward'}: {'; '.join(others)}" if others else ""
    notes = overlap_notes(c.counts) if len(c.counts) > 1 else []
    warn = f"  ({'; '.join(notes)})" if notes else ""
    needs = f"  [needs {c.needs}]" if c.needs else ""
    return f"  {c.code:<13} {c.title[:42]:<42} {q:<30}{needs}{also}{warn}"


def report(term, reqs, unmatched, courses, wanted: str | None, multi: bool, sort: str) -> str:
    lines = [f"# Courses for {term} by requirement",
             "Q = response-weighted average 'Evaluate the course overall' (1-5) over past terms; h = hours/week "
             "outside class; n = Q responses. For reference only.", ""]
    shown = [r for r in reqs if not wanted or wanted.lower() in r.label.lower()]
    for r in shown:
        fits = sorted((c for c in courses if r in c.counts and (not multi or double_counts(c.counts) > 1)), key=SORTS[sort])
        status = "" if r.status == "not_satisfied" else f" [{r.status.replace('_', ' ')}]"
        lines.append(f"## {r.label}{status}: {len(fits)} courses" + (f"  ({r.note})" if r.note else ""))
        lines += [fmt(c, exclude=r) for c in fits]
        lines.append("")
    if not wanted:
        multi_courses = sorted((c for c in courses if double_counts(c.counts) > 1), key=SORTS[sort])
        lines.append(f"## Can count toward 2+ kinds of requirement at once: {len(multi_courses)} courses")
        lines += [fmt(c) for c in multi_courses]
    if unmatched:
        lines += ["", f"Concentration requirements without a course rule yet: {', '.join(unmatched)}"]
    return "\n".join(lines)


def latest_term(conn) -> str:
    return conn.execute(
        "SELECT term FROM offerings GROUP BY term ORDER BY substr(term, 1, 4) DESC, "
        "CASE WHEN term LIKE '%Fall' THEN 1 ELSE 0 END DESC LIMIT 1").fetchone()[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--term", help="catalog term, e.g. '2027 Spring' (default: the latest)")
    ap.add_argument("--req", help="only requirements whose name contains this text")
    ap.add_argument("--multi", action="store_true",
                    help="only courses that can count toward 2+ kinds of requirement (e.g. QRD + concentration)")
    ap.add_argument("--all", action="store_true", help="also list requirements already satisfied or in progress")
    ap.add_argument("--grad", action="store_true", help="include graduate-level, research and tutorial courses")
    ap.add_argument("--sort", choices=SORTS, default="code")
    ap.add_argument("--out", default="data/courses.md", help="also write the list here")
    args = ap.parse_args()

    conn = db.connect(args.db)
    if not conn.execute("SELECT 1 FROM me_requirements LIMIT 1").fetchone():
        sys.exit("No record imported yet; run `uv run import-me` first.")
    term = args.term or latest_term(conn)
    text = report(term, *filter_courses(conn, term, args.all, args.grad), args.req, args.multi, args.sort)
    print(text)
    with open(args.out, "w") as f:
        f.write(text + "\n")
    print(f"\n(saved to {args.out})", file=sys.stderr)


if __name__ == "__main__":
    main()

"""Browse courses by requirement in a local web page.

    uv run site                # build the data and serve http://127.0.0.1:8765
    uv run site --port 9000
    uv run site --build-only   # just write data/site.json

The page (src/harvard_classes/web/) has search, requirement and other
filters, sorting, and course details. It's served on 127.0.0.1 only: the
data includes your record and Q Report data, which is for Harvard
affiliates.
"""

import argparse
import functools
import gzip
import http.server
import json
import sys
from pathlib import Path

from . import db
from .courses import (GRAD_LEVELS, SKIP_TITLES, course_stats, double_counts, done_codes, latest_term, load_courses,
                      load_reports, overlap_notes, reports_for, requirements, sequence_prereq)

WEB = Path(__file__).parent / "web"
QR_SEARCH = "https://qreports.fas.harvard.edu/search/courses?school=FAS&search={}"


def split(value: str | None) -> list[str]:
    return [v.strip() for v in (value or "").split(";") if v.strip() and v.strip() not in ("N/A", "None")]


def build(conn) -> dict:
    reqs, unmatched = requirements(conn, include_satisfied=True)
    by_id, by_code = load_reports(conn)
    done = done_codes(conn)
    known = {r[0] for r in conn.execute("SELECT DISTINCT subject_catalog FROM offerings")} | set(by_code)
    latest = latest_term(conn)
    terms = [t for (t,) in conn.execute(
        "SELECT DISTINCT term FROM offerings ORDER BY substr(term, 1, 4), CASE WHEN term LIKE '%Fall' THEN 1 ELSE 0 END")]

    # Per-course details from the first section of each course in each term.
    details = {}
    for r in conn.execute("SELECT * FROM offerings ORDER BY term, subject_catalog, section"):
        details.setdefault((r["term"], r["subject_catalog"]), r)

    courses = []
    for term in terms:
        for c in load_courses(conn, term):
            c.counts = [r for r in reqs if r.matches(c)]
            still_needed = [r for r in c.counts if r.status != "satisfied"]
            d = details[(term, c.code)]
            s = course_stats(reports_for(c, by_id, by_code), c.instructors)
            courses.append({
                "term": term, "code": c.code, "title": c.title, "instructors": c.instructors,
                "reqs": [reqs.index(r) for r in c.counts],
                # Both are about requirements still to fill: overlap with a satisfied one doesn't matter.
                "double": double_counts([r for r in still_needed if r.status == "not_satisfied"]),
                "notes": overlap_notes(still_needed) if len(still_needed) > 1 else [],
                "named": any(r.names_courses for r in c.counts),
                "taken": c.code in done or bool(set(c.joint) & done),
                "research": bool(SKIP_TITLES.search(c.title)), "grad": c.level in GRAD_LEVELS,
                "needs": sequence_prereq(c.code, known, done),
                "gened": split(c.gen_ed), "divisional": split(c.divisional), "qrd": c.qrd == "Yes",
                "level": c.level, "joint": c.joint, "subject": d["subject"], "department": d["department"],
                "days": d["days"], "times": d["times"], "component": d["component"], "consent": d["consent"],
                "grading": d["grading_basis"], "prereq": d["course_requirements"], "prep": d["recommended_prep"],
                "notes_text": d["course_notes"], "desc": d["description"], "url": d["url"],
                "qr_url": QR_SEARCH.format(c.course_id) if c.course_id else None,
                "q": {"rating": round(s["rating"], 2), "hours": round(s["hours"], 1) if s["hours"] is not None else None,
                      "responses": s["responses"], "terms": s["terms"], "same_instructor": s["same_instructor"]}
                if s else None,
            })

    profile = dict(conn.execute("SELECT key, value FROM me_profile").fetchall())
    record = [dict(r) for r in conn.execute("SELECT code, term, title, status FROM me_courses ORDER BY term, code")]
    return {
        "generated": db.now(), "profile": profile, "record": record, "terms": terms, "default_term": latest,
        "requirements": [{"label": r.label, "group": r.group, "status": r.status, "note": r.note, "needed": r.needed}
                         for r in reqs],
        "unmatched": unmatched, "courses": courses,
    }


class Handler(http.server.SimpleHTTPRequestHandler):
    data: bytes = b""
    data_gz: bytes = b""

    def do_GET(self):
        if self.path.split("?")[0] == "/data.json":
            gz = "gzip" in self.headers.get("Accept-Encoding", "")
            body = self.data_gz if gz else self.data
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        else:
            super().do_GET()

    def log_message(self, *args):  # keep the terminal quiet
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--build-only", action="store_true", help="write data/site.json and exit")
    args = ap.parse_args()

    conn = db.connect(args.db)
    if not conn.execute("SELECT 1 FROM me_requirements LIMIT 1").fetchone():
        sys.exit("No record imported yet; run `uv run import-me` first.")
    data = json.dumps(build(conn), separators=(",", ":")).encode()
    out = Path(args.db).parent / "site.json"
    out.write_bytes(data)
    print(f"built {out} ({len(data) // 1024} KB)", file=sys.stderr)
    if args.build_only:
        return

    Handler.data = data
    Handler.data_gz = gzip.compress(data, compresslevel=6)
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), functools.partial(Handler, directory=str(WEB)))
    print(f"serving http://127.0.0.1:{args.port}  (Ctrl-C to stop)", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

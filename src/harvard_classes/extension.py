"""Build the browser extension into extension/dist/ (load it unpacked in Chrome).

    uv run extension

Bundles the public course catalog from data/harvard.db (no personal or Q
Report data: each student's extension reads those from their own session),
the concentration rule files, the shared finder page (web/), and the
extension scripts in extension/.
"""

import argparse
import gzip
import json
import shutil
import sys
from pathlib import Path

from . import db
from .courses import latest_term, load_courses
from .site import split

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "extension"
WEB = Path(__file__).parent / "web"
RULES = Path(__file__).parent / "rules"

STATUS_BAR = """
  <div id="ext-status" class="ext-status" aria-live="polite">Loading…</div>
  <style>
    .ext-status { margin: 12px 20px 0; padding: 10px 14px; border: 1px solid var(--line); border-radius: var(--radius);
                  background: var(--panel); font-size: 13px; display: grid; gap: 4px; }
    .ext-status .ok { color: var(--done); font-weight: 600; }
    .ext-status .warn { color: var(--warn); font-weight: 600; }
    .ext-status a { color: var(--accent); }
    .ext-actions { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 4px; }
    .ext-actions button { border: 1px solid var(--line); background: var(--bg); border-radius: 8px; padding: 4px 10px; cursor: pointer; }
    .ext-actions button:disabled { opacity: .5; cursor: default; }
    .ext-actions .msg { color: var(--muted); }
  </style>
"""


def catalog(conn) -> dict:
    terms = [t for (t,) in conn.execute(
        "SELECT DISTINCT term FROM offerings ORDER BY substr(term, 1, 4), CASE WHEN term LIKE '%Fall' THEN 1 ELSE 0 END")]
    details = {}
    for r in conn.execute("SELECT * FROM offerings ORDER BY term, subject_catalog, section"):
        details.setdefault((r["term"], r["subject_catalog"]), r)
    courses = []
    for term in terms:
        for c in load_courses(conn, term):
            d = details[(term, c.code)]
            courses.append({
                "term": term, "code": c.code, "title": c.title, "course_id": c.course_id,
                "instructors": c.instructors, "gened": split(c.gen_ed), "divisional": split(c.divisional),
                "qrd": c.qrd == "Yes", "level": c.level, "joint": c.joint, "department": d["department"], "days": d["days"], "times": d["times"], "component": d["component"],
                "consent": d["consent"], "grading": d["grading_basis"], "prereq": d["course_requirements"],
                "prep": d["recommended_prep"], "notes_text": d["course_notes"], "desc": d["description"], "url": d["url"],
            })
    return {"generated": db.now(), "terms": terms, "default_term": latest_term(conn), "courses": courses}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=db.DEFAULT_DB)
    ap.add_argument("--out", default=str(SRC / "dist"))
    args = ap.parse_args()

    conn = db.connect(args.db)
    if not conn.execute("SELECT 1 FROM offerings LIMIT 1").fetchone():
        sys.exit("No catalog yet; run `uv run catalog` first.")
    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    for name in ("manifest.json", "background.js", "harvard.js", "finder-ext.js"):
        shutil.copy(SRC / name, out / name)
    for name in ("app.js", "style.css"):
        shutil.copy(WEB / name, out / name)
    html = (WEB / "index.html").read_text()
    html = html.replace("</header>", "</header>\n" + STATUS_BAR, 1)
    html = html.replace('<script src="app.js"></script>',
                        '<script src="harvard.js"></script>\n  <script src="finder-ext.js"></script>\n'
                        '  <script src="app.js"></script>', 1)
    (out / "finder.html").write_text(html)
    (out / "rules.json").write_text(json.dumps([json.loads(f.read_text()) for f in sorted(RULES.glob("*.json"))]))
    data = json.dumps(catalog(conn), separators=(",", ":")).encode()
    packed = gzip.compress(data, compresslevel=9, mtime=0)  # unpacked in the page with DecompressionStream
    (out / "catalog.json.gz").write_bytes(packed)
    print(f"built {out} (catalog {len(packed) // 1024} KB gzipped, {len(data) // 1024} KB raw). "
          f"In Chrome: chrome://extensions > Developer mode > "
          f"Load unpacked > choose {out}", file=sys.stderr)


if __name__ == "__main__":
    main()

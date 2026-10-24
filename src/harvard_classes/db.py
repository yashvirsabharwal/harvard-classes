"""SQLite storage shared by the catalog and Q Reports scrapers.

Raw pages are kept (gzipped) in `raw_pages` so parsers can be re-run without
re-scraping. Parsed data lives in `offerings` (my.harvard catalog) and the
`qr_*` tables (Q Reports).
"""

import gzip
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path("data/harvard.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_pages (
    url        TEXT PRIMARY KEY,       -- page URL, or "qr:<report_key>" for Q reports
    kind       TEXT NOT NULL,          -- catalog_search | catalog_sections | catalog_course | qr_browse | qr_report
    fetched_at TEXT NOT NULL,
    status     INTEGER,
    html       BLOB                    -- gzip-compressed body
);
CREATE INDEX IF NOT EXISTS raw_pages_kind ON raw_pages(kind);

-- One row per my.harvard course detail page (a section of a course in a term).
CREATE TABLE IF NOT EXISTS offerings (
    url                 TEXT PRIMARY KEY,
    course_id           INTEGER,
    subject_catalog     TEXT,          -- e.g. "COMPSCI 1240"
    subject             TEXT,          -- e.g. "COMPSCI"
    catalog_number      TEXT,          -- e.g. "1240"
    title               TEXT,
    term                TEXT,          -- e.g. "2027 Spring"
    session             TEXT,
    section             TEXT,
    class_number        TEXT,
    instructors         TEXT,          -- JSON list of {name, id}
    days                TEXT,          -- e.g. "M,W"
    times               TEXT,
    dates               TEXT,
    campus              TEXT,
    consent             TEXT,
    enrollment          TEXT,
    school              TEXT,
    credits             TEXT,
    cross_reg           TEXT,
    department          TEXT,
    component           TEXT,
    subject_name        TEXT,
    grading_basis       TEXT,
    course_requirements TEXT,
    exam_deadline       TEXT,
    gen_ed              TEXT,
    qrd                 TEXT,
    divisional          TEXT,
    course_level        TEXT,
    description         TEXT,
    course_notes        TEXT,
    class_notes         TEXT,
    recommended_prep    TEXT,
    jointly_offered     TEXT,          -- JSON list of subject_catalog strings
    fields              TEXT,          -- JSON of every label/value pair on the page
    parsed_at           TEXT
);
CREATE INDEX IF NOT EXISTS offerings_course_id ON offerings(course_id);
CREATE INDEX IF NOT EXISTS offerings_subject_catalog ON offerings(subject_catalog);

-- One row per Q report (a course section x instructor in a term).
-- The report URL carries an opaque token that may change between sessions, so
-- rows are keyed on the browse-page link id plus the instructor shown there.
CREATE TABLE IF NOT EXISTS qr_reports (
    report_key             TEXT PRIMARY KEY,  -- "<link_id>|<instructor_short>"
    url                    TEXT,
    link_id                TEXT,       -- e.g. FAS-122910-2262-1-1-001
    school                 TEXT,
    course_id              INTEGER,    -- joins to offerings.course_id
    strm                   TEXT,       -- PeopleSoft term code, e.g. 2262
    term                   TEXT,       -- e.g. "2026 Spring"
    department             TEXT,
    subject                TEXT,
    catalog_number         TEXT,
    title                  TEXT,
    section                TEXT,
    instructor_short       TEXT,       -- as listed on the browse page
    instructor             TEXT,       -- full name from the report
    course_codes           TEXT,       -- all listings on the report, e.g. "APMTH 115, ENG-SCI 115"
    responded              INTEGER,
    invited                INTEGER,
    response_rate          REAL,
    course_overall         REAL,
    course_overall_fas     REAL,
    instructor_overall     REAL,
    instructor_overall_fas REAL,
    workload_mean          REAL,
    workload_median        REAL,
    workload_sd            REAL,
    workload_n             INTEGER,
    recommend_mean         REAL,
    recommend_n            INTEGER,
    parse_status           TEXT,       -- NULL until fetched; ok | empty | error
    parsed_at              TEXT
);
CREATE INDEX IF NOT EXISTS qr_reports_course_id ON qr_reports(course_id);

CREATE TABLE IF NOT EXISTS qr_questions (
    report_key   TEXT NOT NULL,
    section      TEXT NOT NULL,        -- course | instructor | general
    question     TEXT NOT NULL,
    kind         TEXT,                 -- grid | frequency | stats
    count        INTEGER,
    mean         REAL,
    fas_mean     REAL,
    median       REAL,
    sd           REAL,
    distribution TEXT,                 -- JSON
    PRIMARY KEY (report_key, section, question)
);

-- The signed-in student's own record, imported from my.harvard (uv run import-me).
CREATE TABLE IF NOT EXISTS me_profile (
    key   TEXT PRIMARY KEY,           -- level | plan | expected_graduation | concentration | track | ...
    value TEXT
);
CREATE TABLE IF NOT EXISTS me_courses (
    code          TEXT NOT NULL,      -- e.g. "NEURO 80"
    term          TEXT NOT NULL,      -- e.g. "2026 Fall"
    title         TEXT,
    instructor    TEXT,
    credits       REAL,
    grading_basis TEXT,
    grade         TEXT,
    status        TEXT,               -- taken | in_progress
    PRIMARY KEY (code, term)
);
CREATE TABLE IF NOT EXISTS me_requirements (
    idx            INTEGER PRIMARY KEY,  -- order in the advising report
    path           TEXT,                 -- enclosing sections, " > "-separated
    title          TEXT,
    status         TEXT,                 -- satisfied | not_satisfied | in_progress
    units_required REAL,
    units_taken    REAL,
    units_needed   REAL,
    courses        TEXT                  -- JSON list of course codes applied
);

CREATE TABLE IF NOT EXISTS qr_comments (
    report_key TEXT NOT NULL,
    question   TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    comment    TEXT,
    PRIMARY KEY (report_key, question, idx)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Long timeout so the two scrapers can run at once (each holds the write lock between commits).
    conn = sqlite3.connect(path, timeout=120, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    # columns added after the first release
    cols = {r[1] for r in conn.execute("PRAGMA table_info(qr_reports)")}
    if "course_codes" not in cols:
        conn.execute("ALTER TABLE qr_reports ADD COLUMN course_codes TEXT")
    return conn


def save_raw(conn: sqlite3.Connection, url: str, kind: str, html: str, status: int = 200) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO raw_pages (url, kind, fetched_at, status, html) VALUES (?, ?, ?, ?, ?)",
        (url, kind, now(), status, gzip.compress(html.encode())),
    )


def load_raw(conn: sqlite3.Connection, url: str) -> str | None:
    row = conn.execute("SELECT html FROM raw_pages WHERE url = ?", (url,)).fetchone()
    return gzip.decompress(row["html"]).decode() if row else None


def has_raw(conn: sqlite3.Connection, url: str) -> bool:
    return conn.execute("SELECT 1 FROM raw_pages WHERE url = ?", (url,)).fetchone() is not None


def upsert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT OR REPLACE INTO {table} ({cols}) VALUES ({marks})", list(row.values()))

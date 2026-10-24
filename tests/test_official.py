"""Official requirement lists ("Search by My Requirements") and their precedence over fallbacks."""

import json
from pathlib import Path

from harvard_classes import db
from harvard_classes.courses import Course, requirements
from harvard_classes.myharvard import parse_official

SAMPLE = (Path(__file__).parent / "fixtures" / "official_requirements_sample.json").read_text()


def test_parse_official():
    items = parse_official(SAMPLE)
    assert [(i["section"], i["title"], i["available"]) for i in items] == [
        ("College Requirements", "Quantitative Reasoning with Data Requirement", 1),
        ("Concentration: Demo Studies: Example Track", "Two core demo courses", 1),
        ("Concentration: Demo Studies: Example Track", "Senior thesis", 0),
    ]
    assert items[1]["query"] == "(HU_COURSE_LIST:000000001_19010101)"
    assert items[2]["reason"] == "Thesis requirements can't be searched"


def course(code, term="2027 Spring", qrd="N/A"):
    return Course(code=code, title="", course_id=None, instructors=[], gen_ed="", divisional="", qrd=qrd,
                  level="", joint=[], term=term)


def test_official_lists_take_precedence(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.execute("INSERT INTO me_profile VALUES ('concentration', 'Demo Studies')")
    conn.executemany("INSERT INTO me_requirements (idx, path, title, status) VALUES (?, ?, ?, ?)", [
        (0, "College Requirements", "Quantitative Reasoning with Data Requirement", "not_satisfied"),
        (1, "Concentration: Demo Studies: Example Track", "Two core demo courses", "not_satisfied"),
        (2, "Concentration: Demo Studies: Example Track", "Senior thesis", "not_satisfied"),
    ])
    for i, item in enumerate(parse_official(SAMPLE)):
        db.upsert(conn, "me_official_requirements", {"idx": i, **item})
    conn.executemany("INSERT INTO me_official_courses VALUES (?, ?, ?)", [
        ("Quantitative Reasoning with Data Requirement", "2027 Spring", "STAT 100"),
        ("Two core demo courses", "2027 Spring", "DEMO 101"),
    ])
    reqs, unmatched = requirements(conn, include_satisfied=False)
    by_label = {r.label: r for r in reqs}

    qrd = by_label["QRD"]
    assert qrd.source == "official"
    assert qrd.matches(course("STAT 100"))
    assert not qrd.matches(course("STAT 104", qrd="Yes"))   # catalog tag alone doesn't count when an official list exists
    assert not qrd.matches(course("STAT 100", term="2026 Fall"))  # lists are per term

    core = by_label["Two core demo courses"]
    assert (core.source, core.group, core.names_courses) == ("official", "concentration", True)
    assert core.matches(course("DEMO 101"))

    # Not searchable and no rule file: reported as unmatched, not silently dropped.
    assert unmatched == ["Senior thesis"]

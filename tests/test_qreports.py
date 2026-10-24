from pathlib import Path

import pytest

from harvard_classes.qreports import classify, matches, parse_link


@pytest.mark.parametrize("text, expected", [
    ("AFRAMER 10-Introduction to African American Studies 001 (McCarthy)",
     ("AFRAMER", "10", "Introduction to African American Studies", "001", "McCarthy")),
    ("ENG-SCI 111-Introduction to Scientific Computing 001 (Iams)",
     ("ENG-SCI", "111", "Introduction to Scientific Computing", "001", "Iams")),
    ("COMPSCI 1240-Data Structures and Algorithms 001 (Hesterberg)",
     ("COMPSCI", "1240", "Data Structures and Algorithms", "001", "Hesterberg")),
    ("AFRAMER 123Z-American Democracy 001 (Mangabeira Unger)",
     ("AFRAMER", "123Z", "American Democracy", "001", "Mangabeira Unger")),
    ("HIST-LIT 90-Science-Fiction (and Fact) 001 (de la Fuente)",
     ("HIST-LIT", "90", "Science-Fiction (and Fact)", "001", "de la Fuente")),
])
def test_parse_link(text, expected):
    row = parse_link({"id": "FAS-122910-2262-1-1-001", "href": "https://example/x", "text": text,
                      "department": "Dept"}, "2026 Spring")
    assert (row["subject"], row["catalog_number"], row["title"], row["section"], row["instructor_short"]) == expected
    assert (row["school"], row["course_id"], row["strm"]) == ("FAS", 122910, "2262")
    assert row["report_key"] == f"FAS-122910-2262-1-1-001|{expected[4]}"


def test_matches_checks_course_and_instructor():
    html = (Path(__file__).parent / "fixtures" / "report_sample.html").read_text()
    row = {"subject": "DEMO", "catalog_number": "101", "instructor_short": "Example"}
    assert matches(html, row)
    assert not matches(html, {**row, "catalog_number": "102"})
    assert not matches(html, {**row, "instructor_short": "Smith"})
    cross_listed = html.replace("Course Feedback for DEMO 101<", "Course Feedback for DEMO 101, OTHER 101<")
    assert matches(cross_listed, row)
    no_instructor = html.replace("Instructor Feedback for Jane Q. Example", "General Questions")
    assert matches(no_instructor, {**row, "instructor_short": "Staff"})


def test_classify_flags_pages_without_report_content():
    toc_only = "<html><body><h1>My Report Viewer</h1><h2>Table of Contents</h2></body></html>"
    row = {"subject": "DEMO", "catalog_number": "101", "instructor_short": "Example"}
    assert classify(toc_only, row) == "invalid"
    html = (Path(__file__).parent / "fixtures" / "report_sample.html").read_text()
    assert classify(html, {**row, "catalog_number": "102"}) == "mismatch"

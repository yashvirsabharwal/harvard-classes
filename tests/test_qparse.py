from pathlib import Path

from harvard_classes.qparse import parse_report

SAMPLE = (Path(__file__).parent / "fixtures" / "report_sample.html").read_text()


def test_summary_fields():
    r = parse_report(SAMPLE)
    assert r["status"] == "ok"
    assert r["instructor"] == "Jane Q. Example"
    assert r["course_codes"] == "DEMO 101"
    assert (r["responded"], r["invited"], r["response_rate"]) == (18, 24, 0.75)
    assert (r["course_overall"], r["course_overall_fas"]) == (4.25, 4.1)
    assert (r["instructor_overall"], r["instructor_overall_fas"]) == (4.5, 4.4)
    assert (r["workload_mean"], r["workload_median"], r["workload_sd"], r["workload_n"]) == (8.0, 7.0, 2.5, 15)
    assert (r["recommend_mean"], r["recommend_n"]) == (4.06, 16)


def test_questions_and_comments():
    r = parse_report(SAMPLE)
    by_key = {(q["section"], q["question"]): q for q in r["questions"]}
    feedback = by_key[("course", "Feedback you received on work you produced in this course")]
    assert feedback["kind"] == "grid"
    assert feedback["distribution"]["Fair"] == 20
    assert ("instructor", "Gives useful feedback on assignments") in by_key

    reasons = next(q for q in r["questions"] if q["question"].startswith("What was/were your reason"))
    assert reasons["kind"] == "frequency"
    assert {d["option"]: d["count"] for d in reasons["distribution"]}["Divisional Distribution Requirement"] == 3

    assert [c["comment"] for c in r["comments"]] == ["Problem sets are long but worth it.", "Section was the best part."]
    assert r["comments"][0]["question"].startswith("What would you like to tell future students")


def test_empty_page():
    r = parse_report("<html><body><article><p>No data</p></article></body></html>")
    assert r["status"] == "empty"
    assert r["course_overall"] is None

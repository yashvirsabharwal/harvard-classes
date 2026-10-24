from harvard_classes.courses import (Course, Requirement, code_matcher, double_counts, load_rules, overlap_notes,
                                     sequence_prereq)


def course(code, joint=()):
    return Course(code=code, title="", course_id=None, instructors=[], gen_ed="", divisional="", qrd="",
                  level="", joint=list(joint))


def req(group):
    return Requirement(label=group, group=group, matches=lambda c: True)


def test_code_matcher_checks_cross_listings():
    m = code_matcher(["NEURO 1(05|20)", "PSY 14"])
    assert m(course("NEURO 120"))
    assert not m(course("NEURO 1200"))
    assert m(course("MCB 105", joint=["NEURO 105"]))


def test_sequence_prereq():
    known = {"MATH 55A", "MATH 55B", "CHNSE BA", "CHNSE BB", "STAT 110"}
    assert sequence_prereq("MATH 55B", known, done=set()) == "MATH 55A"
    assert sequence_prereq("MATH 55B", known, done={"MATH 55A"}) is None
    assert sequence_prereq("CHNSE BB", known, done=set()) == "CHNSE BA"
    assert sequence_prereq("STAT 110", known, done=set()) is None


def test_overlap_notes():
    assert overlap_notes([req("divisional"), req("qrd")]) == ["Divisional or QRD, not both"]
    assert overlap_notes([req("divisional"), req("concentration")]) == []
    assert overlap_notes([req("qrd"), req("concentration")]) == []
    assert overlap_notes([req("concentration"), req("concentration")]) == ["fills one concentration slot"]


def test_rules_file_loads_for_concentration_and_track():
    rules = load_rules("Neuroscience", "Neurobiology")
    assert rules and {r["audit_title"] for r in rules["requirements"]} >= {"Intermediate Biology", "Two core neuroscience courses"}


def test_double_counts():
    assert double_counts([req("qrd"), req("concentration")]) == 2
    assert double_counts([req("divisional"), req("qrd")]) == 1
    assert double_counts([req("concentration"), req("concentration")]) == 1
    assert double_counts([req("divisional"), req("qrd"), req("concentration")]) == 2


def test_reports_for_counts_each_section_once():
    from collections import defaultdict

    from harvard_classes.courses import course_stats, reports_for

    def rep(link, codes, instructor, n, overall):
        return {"link_id": link, "course_id": 1, "strm": link.split("-")[2], "course_codes": codes,
                "instructor": instructor, "responded": n, "course_overall": overall, "workload_median": 4.0}

    rows = [
        # co-taught: one report per instructor, same course numbers
        rep("FAS-1-2242-1-1-001", "NEURO 57, OEB 57", "Bence Olveczky", 51, 4.05),
        rep("FAS-1-2242-1-1-001", "NEURO 57, OEB 57", "Naomi Pierce", 51, 4.05),
        # cross-listed copy of the same section (different listing number)
        rep("FAS-1-2262-1-1-001", "NEURO 57, OEB 57", "Naomi Pierce", 18, 3.77),
        rep("FAS-1-2262-1-2-001", "NEURO 57, OEB 57", "Naomi Pierce", 18, 3.77),
        # separately evaluated listings in the same term stay separate
        rep("FAS-1-2270-1-1-001", "NEURO 57", "Naomi Pierce", 30, 4.0),
        rep("FAS-1-2270-1-2-001", "OEB 57", "Naomi Pierce", 10, 3.0),
    ]
    by_id = defaultdict(list, {1: rows})
    c = course("NEURO 57")
    c.course_id, c.instructors = 1, ["Bence Olveczky"]
    reports = reports_for(c, by_id, defaultdict(list))
    stats = course_stats(reports, c.instructors)
    assert stats["responses"] == 51 + 18 + 30 + 10
    assert stats["same_instructor"]  # Olveczky only appears on a merged co-taught report

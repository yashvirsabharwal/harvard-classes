"""Runs the extension's JavaScript parser (extension/harvard.js) in headless Chrome."""

from pathlib import Path

import pytest
from playwright.sync_api import Error as PWError
from playwright.sync_api import sync_playwright

from harvard_classes.myharvard import parse_official

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = (Path(__file__).parent / "fixtures" / "report_sample.html").read_text()


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except PWError:
            pytest.skip("Google Chrome not installed")
        page = browser.new_page()
        page.set_content("<html><body></body></html>")
        page.add_script_tag(path=str(ROOT / "extension" / "harvard.js"))
        yield page
        browser.close()


def test_parse_report_matches_python(page):
    r = page.evaluate("h => Harvard.parseReport(h)", SAMPLE)
    assert r["ok"] and r["codes"] == "DEMO 101" and r["instructor"] == "Jane Q. Example"
    assert (r["responded"], r["course_overall"], r["workload_median"], r["recommend_mean"]) == (18, 4.25, 7, 4.06)


def test_parse_report_without_content(page):
    assert page.evaluate("h => Harvard.parseReport(h).ok", "<html><body><h1>My Report Viewer</h1></body></html>") is False


def test_parse_link(page):
    link = {"id": "FAS-122910-2262-1-1-001", "href": "https://example/x",
            "text": "ENG-SCI 111-Introduction to Scientific Computing 001 (Iams)"}
    r = page.evaluate("l => Harvard.parseLink(l, '2026 Spring')", link)
    assert (r["course_id"], r["strm"], r["code"], r["instructor_short"]) == (122910, "2262", "ENG-SCI 111", "Iams")


def test_parse_official_matches_python(page):
    sample = (Path(__file__).parent / "fixtures" / "official_requirements_sample.json").read_text()
    js = page.evaluate("s => Harvard.parseOfficial(JSON.parse(s))", sample)
    py = parse_official(sample)
    assert [(j["section"], j["title"], j["query"], j["available"], j["reason"]) for j in js] == \
           [(p["section"], p["title"], p["query"], bool(p["available"]), p["reason"]) for p in py]

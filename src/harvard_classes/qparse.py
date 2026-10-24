"""Parse an Explorance Blue Q report page (my-harvard-bc.bluera.com/rpv-eng.aspx).

A report is a sequence of h3 headings and tables. Table shapes:
  * response rate   - header [Raters, Students]; rows Responded/Invited/Response Ratio
  * rating grid     - header ['', Count, Excellent, ..., Course Mean, FAS Mean]; one row per question
  * frequency       - header [Options, (Score), Count, (Percentage)]; question is the preceding h3
  * statistics      - header [Statistics, Value]; question is the preceding h3
  * comments        - header [Comments]; question is the preceding h4 (or h3)
"""

import re

from bs4 import BeautifulSoup

STRUCTURAL = ("Feedback for", "Course Response Rate", "Course General Questions",
              "General Instructor Questions", "Comments from students", "Table of Contents")


def num(s: str | None) -> float | None:
    if s is None:
        return None
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    return float(m.group()) if m else None


def cells(row) -> list[str]:
    return [" ".join(c.get_text(" ", strip=True).split()) for c in row.find_all(["th", "td"])]


def table_rows(table) -> tuple[list[str], list[list[str]]]:
    head = table.find("thead")
    if head:
        header = cells(head.find("tr"))
        body = [cells(r) for r in table.select("tbody tr")] or [cells(r) for r in table.find_all("tr")[1:]]
    else:
        rows = table.find_all("tr")
        header, body = (cells(rows[0]), [cells(r) for r in rows[1:]]) if rows else ([], [])
    return header, body


def parse_report(html: str) -> dict:
    soup = BeautifulSoup(html, "lxml")
    root = soup.find("article") or soup
    out = {"instructor": None, "course_codes": None, "responded": None, "invited": None, "response_rate": None,
           "questions": {}, "comments": []}
    section, question, sub_question = "general", None, None

    def q(sec: str, text: str, kind: str) -> dict:
        return out["questions"].setdefault((sec, text), {"section": sec, "question": text, "kind": kind})

    for el in root.find_all(["h3", "h4", "table"]):
        if el.name == "h3":
            t = " ".join(el.get_text(" ", strip=True).split())
            if t.startswith("Course Feedback for"):
                section = "course"
                out["course_codes"] = t.removeprefix("Course Feedback for").strip()
            elif t.startswith("Instructor Feedback for"):
                section = "instructor"
                out["instructor"] = t.removeprefix("Instructor Feedback for").strip()
            elif not t.startswith(STRUCTURAL):
                section, question = "general", t
            sub_question = None
            continue
        if el.name == "h4":
            sub_question = " ".join(el.get_text(" ", strip=True).split())
            continue

        header, body = table_rows(el)
        if not header or not body:
            continue
        caption = el.caption.get_text(" ", strip=True) if el.caption else ""

        if header[:2] == ["Raters", "Students"] or "Response Rate" in caption:
            vals = {r[0]: r[-1] for r in body if len(r) >= 2}
            out["responded"] = num(vals.get("Responded"))
            out["invited"] = num(vals.get("Invited"))
            ratio = num(vals.get("Response Ratio"))
            out["response_rate"] = ratio / 100 if ratio is not None else None

        elif "Count" in header and any(h.endswith("Mean") for h in header):
            mean_cols = [i for i, h in enumerate(header) if h.endswith("Mean")]
            for r in body:
                if len(r) != len(header):
                    continue
                row = q(section, r[0], "grid")
                row["count"] = num(r[header.index("Count")])
                row["mean"] = num(r[mean_cols[0]])
                row["fas_mean"] = num(r[mean_cols[1]]) if len(mean_cols) > 1 else None
                row["distribution"] = {header[i]: num(r[i]) for i in range(1, len(header))
                                       if i not in mean_cols and header[i] != "Count"}

        elif header[0] == "Options" and question:
            row = q("general", question, "frequency")
            idx = {h: i for i, h in enumerate(header)}
            dist = []
            for r in body:
                item = {"option": r[0]}
                for col in ("Score", "Count", "Percentage"):
                    if col in idx and idx[col] < len(r):
                        item[col.lower()] = num(r[idx[col]])
                dist.append(item)
            row["distribution"] = dist
            row["count"] = sum(d.get("count") or 0 for d in dist)
            scored = [d for d in dist if d.get("score") is not None and d.get("count")]
            if scored and row.get("mean") is None:
                row["mean"] = sum(d["score"] * d["count"] for d in scored) / sum(d["count"] for d in scored)

        elif header[0] == "Statistics" and question:
            row = q("general", question, "stats" if (("general", question) not in out["questions"]) else
                    out["questions"][("general", question)]["kind"])
            stats = {r[0]: num(r[1]) for r in body if len(r) >= 2}
            row["mean"] = stats.get("Mean", row.get("mean"))
            row["median"] = stats.get("Median")
            row["sd"] = stats.get("Standard Deviation")
            if stats.get("Response Count") is not None:
                row["count"] = stats["Response Count"]
            row.setdefault("distribution", {k: v for k, v in stats.items()})

        elif header == ["Comments"]:
            label = sub_question or question or "Comments"
            out["comments"] += [{"question": label, "comment": r[0]} for r in body if r and r[0]]

    out["questions"] = list(out["questions"].values())
    out.update(summarize(out["questions"]))
    out["status"] = "ok" if out["questions"] or out["responded"] is not None else "empty"
    return out


def summarize(questions: list[dict]) -> dict:
    def find(section: str, pattern: str) -> dict:
        for row in questions:
            if row["section"] == section and re.search(pattern, row["question"], re.I):
                return row
        return {}

    course = find("course", r"course overall") or next((r for r in questions if r["section"] == "course"), {})
    instr = find("instructor", r"instructor overall") or next((r for r in questions if r["section"] == "instructor"), {})
    work = find("general", r"hours per week")
    rec = find("general", r"recommend")
    return {
        "course_overall": course.get("mean"), "course_overall_fas": course.get("fas_mean"),
        "instructor_overall": instr.get("mean"), "instructor_overall_fas": instr.get("fas_mean"),
        "workload_mean": work.get("mean"), "workload_median": work.get("median"),
        "workload_sd": work.get("sd"), "workload_n": work.get("count"),
        "recommend_mean": rec.get("mean"), "recommend_n": rec.get("count"),
    }

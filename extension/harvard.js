// Parsers and fetchers for my.harvard and Q Reports, run inside the student's
// own browser session. JavaScript ports of src/harvard_classes/{myharvard,qreports,qparse}.py.

const Harvard = (() => {
  const MYH = "https://my.harvard.edu";
  const QR = "https://qreports.fas.harvard.edu";
  const TERM_RE = /^20\d\d (Fall|Spring|Summer|Winter|January)$/;
  const CODE_RE = /[A-Z][A-Z&\-]*(?: [A-Z&\-]+)*? (?:\d\w*|[A-Z]{1,3})$/;
  const FINAL_GRADE = /^(A|A-|[BCD][+-]?|E|F|SAT|UNS|PA|FL|INC|W|WD|EXT|CR|NCR)$/;
  const UNITS_RE = /Units:\s*([\d.]+) required,\s*([\d.]+) taken,\s*([\d.]+) needed/;
  const STATUS = { "Satisfied": "satisfied", "Not Satisfied": "not_satisfied", "In Progress": "in_progress" };
  // "AFRAMER 10-Introduction to African American Studies 001 (McCarthy)"
  const LINK_RE = /^([A-Z][A-Z&\-]*(?: [A-Z&\-]+)*?) (\S+?)-(.*) (\S+)\s*\((.*)\)$/;

  const squash = (s) => (s || "").replace(/\s+/g, " ").trim();

  function parse(html) {
    const doc = new DOMParser().parseFromString(html, "text/html");
    doc.querySelectorAll('[role="tooltip"], script, style, svg').forEach((e) => e.remove());
    return doc;
  }

  function splitCode(text) {  // "Neurobiology of Behavior NEURO 80" -> ["Neurobiology of Behavior", "NEURO 80"]
    const m = CODE_RE.exec(text);
    return m ? [text.slice(0, m.index).trim(), m[0]] : [text, null];
  }

  function textNodes(root) {
    const out = [];
    const walker = root.ownerDocument.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) out.push(walker.currentNode);
    return out;
  }

  // --- my.harvard: /student/grades -------------------------------------------

  function parseGrades(html) {
    const doc = parse(html);
    const main = doc.querySelector("main") || doc.body;
    const strings = textNodes(main).map((n) => squash(n.textContent)).filter(Boolean);
    const profile = {};
    const after = (label) => { const i = strings.indexOf(label); return i >= 0 ? strings[i + 1] : undefined; };
    if (after("Current Level")) profile.level = after("Current Level");
    if (after("Expected Graduation Term")) profile.expected_graduation = after("Expected Graduation Term");
    const plan = strings.findIndex((s) => s.startsWith("Plan:"));
    if (plan >= 0) profile.plan = strings[plan].slice(5).trim() || strings[plan + 1];

    // Each course is an <h4> ("Title CODE") followed by hidden "Label:" / value pairs.
    const courses = [];
    let term = null, current = null, h4 = null;
    const finish = () => {
      if (!current) return;
      const f = {};
      for (let i = 0; i < current.tokens.length - 1; i++) {
        if (current.tokens[i].endsWith(":")) f[current.tokens[i].slice(0, -1)] = current.tokens[i + 1];
      }
      const done = FINAL_GRADE.test(f.Grade || "");
      courses.push({
        code: current.code, term: current.term, title: current.title, instructor: f.Instructor || null,
        credits: f.Credits ? +f.Credits : null, grading_basis: f["Grading Basis"] || null,
        grade: done ? f.Grade : null, status: done ? "taken" : "in_progress",
      });
      current = null;
    };
    for (const node of textNodes(main)) {
      const t = squash(node.textContent);
      if (!t) continue;
      const owner = node.parentElement.closest("h4");
      if (owner) {
        if (owner !== h4) {
          finish();
          h4 = owner;
          const [title, code] = splitCode(squash(owner.textContent));
          if (code) current = { code, title, term, tokens: [] };
        }
        continue;
      }
      if (TERM_RE.test(t)) { finish(); term = t; continue; }
      if (current) current.tokens.push(t);
    }
    finish();
    return { profile, courses };
  }

  // --- my.harvard: /student/academic-progress/display (JSON wrapping HTML) ----

  function parseAudit(json) {
    const doc = parse(json.requirements);
    const profile = {};
    const m = /Concentration: ([^:]+?): ([^.]+?) Track/.exec(squash(doc.body.textContent));
    if (m) { profile.concentration = m[1].trim(); profile.track = m[2].trim(); }
    const own = (el, line) => el.closest(".req-line") === line;
    const reqs = [];
    for (const line of doc.querySelectorAll(".req-line")) {
      const heading = [...line.querySelectorAll("h3, h4")].find((h) => own(h, line));
      if (!heading) continue;
      const badge = [...line.querySelectorAll(".badge")].find((b) => own(b, line) && squash(b.textContent) in STATUS);
      const unitsNode = textNodes(line).find((n) => own(n.parentElement, line) && UNITS_RE.test(n.textContent));
      const units = unitsNode && UNITS_RE.exec(unitsNode.textContent);
      const codes = [];
      for (const table of line.querySelectorAll("table")) {
        if (!own(table, line)) continue;
        for (const row of table.querySelectorAll("tbody tr")) {
          const cell = row.querySelector("td");
          const code = cell && splitCode(squash(cell.textContent))[1];
          if (code) codes.push(code);
        }
      }
      const path = [];
      for (let p = line.parentElement; p; p = p.parentElement) {
        if (!/hs-accordion/.test(p.className)) continue;
        const h = p.querySelector("h2, h3, h4");
        if (h && h !== heading) path.unshift(squash(h.textContent));
      }
      reqs.push({
        path: path.join(" > "), title: squash(heading.textContent),
        status: badge ? STATUS[squash(badge.textContent)] : null,
        units_needed: units ? +units[3] : null, courses: codes,
      });
    }
    return { profile, requirements: reqs };
  }

  // --- my.harvard: "Search by My Requirements" --------------------------------
  // Each open requirement as a course-search query built from the registrar's
  // official course lists (port of parse_official in myharvard.py).

  function parseOfficial(json) {
    const doc = parse(json.html);
    const sections = {};
    for (const b of doc.querySelectorAll("button[id^=sbr-panel-req-item-]")) {
      sections[(b.getAttribute("data-hs-tab") || "").replace(/^#/, "")] = squash(b.textContent);
    }
    const items = [];
    for (const el of new Set(doc.querySelectorAll("a[data-search-text], .not-available"))) {
      const heading = el.querySelector("h3");
      const panel = el.closest("[id^=sbr-panel-req-]");
      const query = el.getAttribute("data-search-text");
      const available = !!query && !el.classList.contains("not-available");
      items.push({
        section: panel ? sections[panel.id] || null : null,
        title: squash((heading || el).textContent), query, available,
        reason: available ? null : el.getAttribute("data-reason") || "Not available in Search by My Requirements",
      });
    }
    return items;
  }

  const CARD_CODE = /data-course-id="[^"]*"[\s\S]*?badge-light[^>]*>([^<]+)</g;  // each card's "Class ID" badge

  // Course codes matching a search query in one term. Public: no login needed.
  async function queryCourses(hq, term) {
    const page = async (n) => JSON.parse((await get(`${MYH}/search/?` + new URLSearchParams(
      { q: "", hq, school: "All", term, sort: "subject_catalog", page: n }))).text);
    const first = await page(1);
    const rest = Array.from({ length: Math.ceil(first.total_hits / 15) - 1 }, (_, i) => i + 2);
    const pages = [first, ...await Promise.all(rest.map(page))];
    return [...new Set(pages.flatMap((p) => [...p.hits.matchAll(CARD_CODE)].map((m) => squash(m[1]))))];
  }

  // --- Q Reports --------------------------------------------------------------

  function parseLink(a, term) {
    const [school, courseId, strm] = a.id.split("-");
    const m = LINK_RE.exec(a.text);
    return {
      key: `${a.id}|${m ? m[5] : ""}`, url: a.href, term, strm, school,
      course_id: /^\d+$/.test(courseId) ? +courseId : null,
      code: m ? `${m[1]} ${m[2]}` : null, title: m ? m[3] : a.text, instructor_short: m ? m[5] : null,
    };
  }

  function parseBrowse(html, baseUrl) {
    const doc = parse(html);
    const terms = {};
    for (const a of doc.querySelectorAll("a[href]")) {
      const label = squash(a.textContent);
      if (TERM_RE.test(label) && !(label in terms)) terms[label] = new URL(a.getAttribute("href"), baseUrl).href;
    }
    const links = [...doc.querySelectorAll("#bluecourses a[id][href]")].map((a) => ({
      id: a.id, href: new URL(a.getAttribute("href"), baseUrl).href, text: squash(a.textContent),
    }));
    return { signedIn: !!doc.querySelector("#bluecourses"), terms, links };
  }

  const num = (s) => { const m = /-?\d+(?:\.\d+)?/.exec((s || "").replace(/,/g, "")); return m ? +m[0] : null; };

  // Summary numbers from one report page (the parts the finder shows).
  function parseReport(html) {
    const doc = parse(html);
    const root = doc.querySelector("article") || doc.body;
    const out = { codes: null, instructor: null, responded: null, course_overall: null, workload_median: null, recommend_mean: null };
    let section = "general", question = null;
    for (const el of root.querySelectorAll("h3, table")) {
      if (el.tagName === "H3") {
        const t = squash(el.textContent);
        if (t.startsWith("Course Feedback for")) { section = "course"; out.codes = t.slice(19).trim(); }
        else if (t.startsWith("Instructor Feedback for")) { section = "instructor"; out.instructor = t.slice(23).trim(); }
        else if (!/^(Feedback for|Course Response Rate|Course General Questions|General Instructor Questions|Comments from students|Table of Contents)/.test(t)) {
          section = "general"; question = t;
        }
        continue;
      }
      const header = [...(el.querySelector("thead tr") || el.rows[0] || { cells: [] }).cells].map((c) => squash(c.textContent));
      const body = [...el.querySelectorAll("tbody tr")].map((r) => [...r.cells].map((c) => squash(c.textContent)));
      if (header[0] === "Raters") {
        const row = body.find((r) => r[0] === "Responded");
        if (row) out.responded = num(row[1]);
      } else if (section === "course" && header.includes("Count") && out.course_overall === null) {
        const mean = header.findIndex((h) => h.endsWith("Mean"));
        const row = body.find((r) => /course overall/i.test(r[0])) || body[0];
        if (row && mean > 0) out.course_overall = num(row[mean]);
      } else if (header[0] === "Statistics" && question) {
        const stats = Object.fromEntries(body.map((r) => [r[0], num(r[1])]));
        if (/hours per week/i.test(question) && out.workload_median === null) out.workload_median = stats.Median ?? null;
        if (/recommend/i.test(question) && out.recommend_mean === null) out.recommend_mean = stats.Mean ?? null;
      }
    }
    out.ok = out.codes !== null;
    return out;
  }

  // --- fetching with the student's own cookies --------------------------------

  async function get(url) {
    const r = await fetch(url, { credentials: "include" });
    if (!r.ok) throw new Error(`${r.status} for ${url}`);
    return { url: r.url, text: await r.text() };
  }

  async function fetchRecord() {
    const grades = await get(`${MYH}/student/grades`);
    if (!/Course &amp; Grade History|Course & Grade History/.test(grades.text)) return { signedIn: false };
    const [audit, home] = await Promise.all([
      get(`${MYH}/student/academic-progress/display`),  // slow: generated on request
      get(`${MYH}/`),
    ]);
    const tab = parse(home.text).querySelector('[data-search-mode="sbr"][data-schools]');
    let official = [];
    try {
      const r = await get(`${MYH}/search/student-requirements/${tab ? tab.getAttribute("data-schools") : "HCOL"}`);
      official = parseOfficial(JSON.parse(r.text));
    } catch (e) {
      console.warn("official requirements unavailable; falling back to catalog fields and rules", e);
    }
    const g = parseGrades(grades.text);
    const a = parseAudit(JSON.parse(audit.text));
    return { signedIn: true, profile: { ...g.profile, ...a.profile }, courses: g.courses, requirements: a.requirements, official };
  }

  async function fetchReportIndex(onProgress) {
    const first = await get(`${QR}/browse/index`);
    const b = parseBrowse(first.text, first.url);
    if (!b.signedIn) return { signedIn: false };
    let count = 0;
    const perTerm = await Promise.all(Object.entries(b.terms).map(async ([term, href]) => {  // all terms at once
      const page = await get(href);
      const links = parseBrowse(page.text, page.url).links.map((l) => parseLink(l, term));
      onProgress?.(count += links.length);
      return links;
    }));
    const seen = new Set();
    const reports = perTerm.flat().filter((r) => !seen.has(r.key) && seen.add(r.key));
    return { signedIn: true, terms: Object.keys(b.terms), reports };
  }

  async function fetchReport(url) {
    return parseReport((await get(url)).text);
  }

  return {
    MYH, QR, parseGrades, parseAudit, parseOfficial, parseBrowse, parseLink, parseReport, splitCode,
    fetchRecord, queryCourses, fetchReportIndex, fetchReport,
  };
})();

if (typeof module !== "undefined") module.exports = Harvard;

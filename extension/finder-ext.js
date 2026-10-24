// Extension side of the Course Finder: reads the student's my.harvard record and
// Q Reports from their own session, matches the bundled public catalog against
// their requirements (a port of src/harvard_classes/courses.py), and feeds the
// shared finder UI (app.js) through window.courseFinderSource.
// Nothing leaves the browser: everything is kept in chrome.storage.local.

(() => {
  const DIVISIONAL = { "Science and Engineering and Applied Science": "Science & Engineering & Applied Science" };
  const NOT_COURSE_REQUIREMENTS = /Total Credits|C- or better|Letter Graded Credits|Courses Not used|Concentration GPA|^For questions/i;
  const SKIP_TITLES = /Research|Reading Course|Thesis|Tutorial|Independent Study|Directed Study|Supervised Reading/i;
  const GRAD_LEVELS = new Set(["Graduate Course", "Primarily for Graduate Students"]);
  const QR_SEARCH = "https://qreports.fas.harvard.edu/search/courses?school=FAS&search=";
  const Q_WORKERS = 6;
  const STALE_AFTER = 8;  // consecutive content-less report pages before assuming the Q session expired

  const store = {
    get: async (key) => (await chrome.storage.local.get(key))[key],
    set: (key, value) => chrome.storage.local.set({ [key]: value }),
  };
  let catalog, rules, record, qindex, qcache = {}, data;

  // --- requirements -----------------------------------------------------------

  function codeMatcher(patterns) {
    const rx = new RegExp(`^(?:${patterns.join("|")})$`);
    return (c) => rx.test(c.code) || c.joint.some((j) => rx.test(j));
  }

  function buildRequirements() {
    if (!record) return { reqs: [], unmatched: [] };
    const p = record.profile;
    const rule = rules.find((r) => p.concentration && r.name.toLowerCase().includes(p.concentration.toLowerCase())
      && (!p.track || r.name.toLowerCase().includes(p.track.toLowerCase())));
    const byTitle = Object.fromEntries((rule?.requirements || []).map((r) => [r.audit_title.toLowerCase(), r]));
    const reqs = [], unmatched = [];
    for (const row of record.requirements) {
      const { title, path, status } = row;
      if (!status || NOT_COURSE_REQUIREMENTS.test(title)) continue;
      let req = null;
      if (path.startsWith("General Education")) {
        req = { label: `Gen Ed: ${title}`, group: "gened", match: (c) => c.gened.includes(title) };
      } else if (path.startsWith("Divisional")) {
        const div = DIVISIONAL[title] || title;
        req = { label: `Divisional: ${title}`, group: "divisional", match: (c) => c.divisional.includes(div) };
      } else if (title.startsWith("Quantitative Reasoning with Data")) {
        req = { label: "QRD", group: "qrd", match: (c) => c.qrd };
      } else if (byTitle[title.toLowerCase()]) {
        const r = byTitle[title.toLowerCase()];
        req = { label: r.label, group: "concentration", match: codeMatcher(r.courses), note: r.note || "", names_courses: !r.broad };
      } else if (path.startsWith("Concentration") && status === "not_satisfied") {
        unmatched.push(title);
      }
      if (req) reqs.push({ note: "", names_courses: false, ...req, status, needed: row.units_needed });
    }
    return { reqs, unmatched };
  }

  function doubleCounts(reqs) {  // kinds of requirement one course can fill at once
    const groups = new Set(reqs.map((r) => r.group));
    if (groups.has("divisional") && groups.has("qrd")) groups.delete("qrd");  // can't count for both
    return groups.size;
  }

  function overlapNotes(reqs) {
    const n = (g) => reqs.filter((r) => r.group === g).length;
    const notes = [];
    if (n("divisional") && n("qrd")) notes.push("Divisional or QRD, not both");
    if (n("gened") > 1) notes.push("listed in 2 Gen Ed categories; check if it can count for both");
    if (n("divisional") > 1) notes.push("cross-divisional; counts for one division");
    if (n("concentration") > 1) notes.push("fills one concentration slot");
    return notes;
  }

  function doneCodes() {
    const codes = new Set((record?.courses || []).map((c) => c.code));
    // Renumbered or cross-listed courses share a course id with something taken.
    const ids = new Set(catalog.courses.filter((c) => codes.has(c.code)).map((c) => c.course_id));
    for (const c of catalog.courses) if (ids.has(c.course_id)) codes.add(c.code);
    return codes;
  }

  // --- Q data -----------------------------------------------------------------

  let reportsById = new Map(), reportsByCode = new Map();

  function indexReports() {
    reportsById = new Map();
    reportsByCode = new Map();
    for (const r of qindex?.reports || []) {
      if (!reportsById.has(r.course_id)) reportsById.set(r.course_id, []);
      reportsById.get(r.course_id).push(r);
      if (r.code) {
        if (!reportsByCode.has(r.code)) reportsByCode.set(r.code, []);
        reportsByCode.get(r.code).push(r);
      }
    }
  }

  function reportsFor(c) {
    const seen = new Set();
    return [...(reportsById.get(c.course_id) || []), ...[c.code, ...c.joint].flatMap((k) => reportsByCode.get(k) || [])]
      .filter((r) => !seen.has(r.key) && seen.add(r.key));
  }

  function qStats(c) {
    const loaded = [], seen = new Set();
    let pending = 0;
    for (const r of reportsFor(c)) {
      const s = qcache[r.key];
      if (s === undefined) { pending++; continue; }
      if (!s || s.course_overall === null || !s.responded) continue;
      // Cross-listed courses have one report under several listings.
      const id = `${r.strm}|${s.instructor}|${s.responded}|${s.course_overall}`;
      if (!seen.has(id)) { seen.add(id); loaded.push({ ...s, strm: r.strm }); }
    }
    if (!loaded.length) return { q: null, pending };
    const n = loaded.reduce((a, r) => a + r.responded, 0);
    const hours = loaded.filter((r) => r.workload_median !== null);
    const hw = hours.reduce((a, r) => a + r.responded, 0);
    const teaching = new Set(c.instructors.map((i) => i.split(" ").pop().toLowerCase()));
    return {
      pending,
      q: {
        rating: Math.round(loaded.reduce((a, r) => a + r.course_overall * r.responded, 0) / n * 100) / 100,
        hours: hw ? Math.round(hours.reduce((a, r) => a + r.workload_median * r.responded, 0) / hw * 10) / 10 : null,
        responses: n, terms: new Set(loaded.map((r) => r.strm)).size,
        same_instructor: loaded.some((r) => teaching.has((r.instructor || "").split(" ").pop().toLowerCase())),
      },
    };
  }

  // --- data for the finder ----------------------------------------------------

  function buildData() {
    const { reqs, unmatched } = buildRequirements();
    const done = doneCodes();
    const known = new Set(catalog.courses.map((c) => c.code));
    indexReports();
    const courses = catalog.courses.map((c) => {
      const counts = reqs.filter((r) => r.match(c));
      const stillNeeded = counts.filter((r) => r.status !== "satisfied");
      const first = c.code.endsWith("B") ? c.code.slice(0, -1) + "A" : null;
      const { q, pending } = qStats(c);
      return {
        ...c,
        reqs: counts.map((r) => reqs.indexOf(r)),
        double: doubleCounts(stillNeeded.filter((r) => r.status === "not_satisfied")),
        notes: stillNeeded.length > 1 ? overlapNotes(stillNeeded) : [],
        named: counts.some((r) => r.names_courses),
        taken: done.has(c.code) || c.joint.some((j) => done.has(j)),
        research: SKIP_TITLES.test(c.title), grad: GRAD_LEVELS.has(c.level),
        needs: first && known.has(first) && !done.has(first) ? first : null,
        qr_url: c.course_id ? QR_SEARCH + c.course_id : null,
        q, q_pending: pending > 0,
      };
    });
    data = {
      generated: catalog.generated, profile: record?.profile || {}, record: record?.courses || [],
      terms: catalog.terms, default_term: catalog.default_term,
      requirements: reqs.map(({ match, ...r }) => r), unmatched, courses,
    };
    return data;
  }

  function refreshQ(courses) {  // recompute Q columns in place after reports load
    for (const c of courses) {
      const { q, pending } = qStats(c);
      c.q = q;
      c.q_pending = pending > 0;
    }
  }

  // --- lazy Q loading ---------------------------------------------------------

  const queue = [], queued = new Set();
  let active = 0, invalidStreak = 0, warmedUp = false, rerenderTimer = null, saveTimer = null;

  // Reports for these courses go to the front of the queue, so whatever is on
  // screen (or just requested) loads first; anything queued earlier waits behind.
  function enqueue(courses) {
    if (!qindex) return;
    const front = [];
    for (const c of courses) {
      for (const r of reportsFor(c)) {
        if (qcache[r.key] !== undefined) continue;
        if (queued.has(r.key)) {
          const i = queue.findIndex((item) => item.r.key === r.key);
          if (i >= 0) front.push(...queue.splice(i, 1));
        } else {
          queued.add(r.key);
          front.push({ r, c });
        }
      }
    }
    queue.unshift(...front);
    pump();
  }

  function pump() {
    while (active < Q_WORKERS && queue.length && invalidStreak < STALE_AFTER) {
      const { r } = queue.shift();
      active++;
      loadReport(r).finally(() => { active--; queued.delete(r.key); pump(); scheduleRerender(); status(); });
    }
  }

  async function loadReport(r) {
    try {
      let s = await Harvard.fetchReport(r.url);
      if (!s.ok && !warmedUp) {
        // First visit to the report site may need a sign-on hop that only a real tab completes.
        warmedUp = true;
        await chrome.runtime.sendMessage({ type: "warmup", url: r.url });
        s = await Harvard.fetchReport(r.url);
      }
      if (!s.ok) {
        if (++invalidStreak >= STALE_AFTER) status();
        return;
      }
      invalidStreak = 0;
      qcache[r.key] = { codes: s.codes, instructor: s.instructor, responded: s.responded,
                        course_overall: s.course_overall, workload_median: s.workload_median };
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => store.set("qcache", qcache), 1000);
    } catch (e) {
      console.warn("Q report failed", r.key, e);
    }
  }

  function scheduleRerender() {
    clearTimeout(rerenderTimer);
    rerenderTimer = setTimeout(() => {
      refreshQ(data.courses);
      window.courseFinder.render();
    }, 400);
  }

  // --- status bar -------------------------------------------------------------

  let message = "";

  function status() {
    const el = document.getElementById("ext-status");
    const loaded = Object.keys(qcache).length;
    const qStale = invalidStreak >= STALE_AFTER;
    const recordPart = record
      ? `<span class="ok">my.harvard ✓</span> ${record.courses.length} courses, ${(data?.requirements || []).filter((r) => r.status === "not_satisfied").length} open requirements · synced ${new Date(record.synced).toLocaleString()}`
      : `<span class="warn">Not signed in to my.harvard.</span> <a href="https://my.harvard.edu/login" target="_blank">Sign in ↗</a>, then refresh.`;
    const qPart = qindex && !qStale
      ? `<span class="ok">Q Reports ✓</span> ${qindex.reports.length.toLocaleString()} reports listed, ${loaded.toLocaleString()} loaded${queue.length + active ? `, ${queue.length + active} loading` : ""}`
      : `<span class="warn">${qStale ? "Q Reports session expired." : "Not signed in to Q Reports."}</span> <a href="https://qreports.fas.harvard.edu/home/index" target="_blank">Sign in ↗</a>, then refresh.`;
    el.innerHTML = `
      <div>${recordPart}</div>
      <div>${qPart}</div>
      <div class="ext-actions">
        <button id="extRefresh">Refresh my record &amp; Q lists</button>
        <button id="extLoadQ" ${qindex ? "" : "disabled"}>Load Q for my open requirements</button>
        <button id="extClear">Clear stored data</button>
        <span class="msg">${message}</span>
      </div>`;
    document.getElementById("extRefresh").onclick = () => sync(true);
    document.getElementById("extLoadQ").onclick = () => {
      const open = new Set(data.requirements.map((r, i) => r.status === "not_satisfied" ? i : -1));
      enqueue(data.courses.filter((c) => c.reqs.some((i) => open.has(i)) && !c.taken));
    };
    document.getElementById("extClear").onclick = async () => {
      await chrome.storage.local.clear();
      location.reload();
    };
  }

  function say(text) { message = text; status(); }

  // --- sync -------------------------------------------------------------------

  async function sync(force) {
    if (force || !record) {
      say("Reading your my.harvard record…");
      try {
        const r = await Harvard.fetchRecord();
        if (r.signedIn) {
          record = { ...r, synced: Date.now() };
          await store.set("record", record);
        } else if (force) {
          record = null;
        }
      } catch (e) {
        console.warn(e);
      }
    }
    if (force || !qindex) {
      say("Listing Q Reports…");
      try {
        const q = await Harvard.fetchReportIndex((term, n) => say(`Listing Q Reports… ${term} (${n.toLocaleString()} so far)`));
        if (q.signedIn) {
          qindex = { ...q, synced: Date.now() };
          await store.set("qindex", qindex);
          invalidStreak = 0;
        }
      } catch (e) {
        console.warn(e);
      }
    }
    window.courseFinder.setData(buildData());
    say("");
  }

  window.courseFinderSource = {
    async load() {
      [catalog, rules] = await Promise.all([
        fetch("catalog.json").then((r) => r.json()),
        fetch("rules.json").then((r) => r.json()),
      ]);
      record = await store.get("record");
      qindex = await store.get("qindex");
      qcache = (await store.get("qcache")) || {};
      const d = buildData();
      status();
      if (!record || !qindex) setTimeout(() => sync(false), 0);  // fill in what's missing, then re-render
      return d;
    },
    onRender(visible) {
      enqueue(visible);
    },
  };
})();

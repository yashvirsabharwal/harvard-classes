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
  const Q_WORKERS = 8;       // the report server is slow per request but handles ~8 in parallel well
  const Q_PER_COURSE = 4;    // newest sections fetched per course in list views; opening a course loads the rest
  const STALE_AFTER = 8;     // consecutive content-less report pages before assuming the Q session expired
  const Q_PREFIX = "q:";     // one storage entry per section, so saving a few never rewrites them all

  let catalog, rules, record, qindex, data;
  // Course-level Q numbers per section listing ("link id", e.g. FAS-207533-2262-1-1-001). A section has
  // one report per instructor, all repeating the course numbers, so fetching one of them is enough.
  const qcache = new Map();  // link id -> { codes, responded, course_overall, workload_median }

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

  const unitCourses = new Map();  // link id -> courses it belongs to (to update only those)
  const unitUrl = new Map();      // link id -> one report url for it
  const linkId = (key) => key.split("|")[0];
  const surname = (name) => (name || "").split(" ").pop().toLowerCase();

  function linkReports() {  // attach each course's sections ("units"), newest first
    const byId = new Map(), byCode = new Map();
    const push = (m, k, r) => (m.get(k) || m.set(k, []).get(k)).push(r);
    unitCourses.clear();
    unitUrl.clear();
    for (const r of qindex?.reports || []) {
      push(byId, r.course_id, r);
      if (r.code) push(byCode, r.code, r);
      if (!unitUrl.has(linkId(r.key))) unitUrl.set(linkId(r.key), r.url);
    }
    for (const c of catalog.courses) {
      const units = new Map();
      for (const r of [...(byId.get(c.course_id) || []), ...[c.code, ...c.joint].flatMap((k) => byCode.get(k) || [])]) {
        const id = linkId(r.key);
        const u = units.get(id) || units.set(id, { id, strm: r.strm, instructors: new Set() }).get(id);
        u.instructors.add(surname(r.instructor_short));
      }
      c._units = [...units.values()].sort((a, b) => b.strm.localeCompare(a.strm));
      for (const u of c._units) push(unitCourses, u.id, c);
    }
  }

  function setQ(c) {
    const loaded = [], seen = new Set();
    for (const u of c._units) {
      const s = qcache.get(u.id);
      if (!s || s.course_overall === null || !s.responded) continue;
      // Cross-listed copies of one section repeat the same numbers: count them once.
      const same = `${u.strm}|${u.id.split("-").pop()}|${s.codes}|${s.responded}|${s.course_overall}`;
      if (!seen.has(same)) { seen.add(same); loaded.push({ ...s, strm: u.strm }); }
    }
    c.q_pending = c._units.some((u) => queued.has(u.id));
    if (!loaded.length) { c.q = null; return; }
    const n = loaded.reduce((a, r) => a + r.responded, 0);
    const hours = loaded.filter((r) => r.workload_median !== null);
    const hw = hours.reduce((a, r) => a + r.responded, 0);
    const teaching = new Set(c.instructors.map(surname));
    c.q = {
      rating: Math.round(loaded.reduce((a, r) => a + r.course_overall * r.responded, 0) / n * 100) / 100,
      hours: hw ? Math.round(hours.reduce((a, r) => a + r.workload_median * r.responded, 0) / hw * 10) / 10 : null,
      responses: n, terms: new Set(loaded.map((r) => r.strm)).size,
      same_instructor: c._units.some((u) => [...u.instructors].some((i) => teaching.has(i))),  // from the listing: no fetch
      reports: c._units.filter((u) => qcache.has(u.id)).length, total: c._units.length,
    };
  }

  // --- data for the finder ----------------------------------------------------

  function buildData() {
    const { reqs, unmatched } = buildRequirements();
    const done = doneCodes();
    const known = new Set(catalog.courses.map((c) => c.code));
    linkReports();
    for (const c of catalog.courses) {  // computed fields go on the catalog objects themselves: no copies
      const counts = reqs.filter((r) => r.match(c));
      const stillNeeded = counts.filter((r) => r.status !== "satisfied");
      const first = c.code.endsWith("B") ? c.code.slice(0, -1) + "A" : null;
      c.reqs = counts.map((r) => reqs.indexOf(r));
      c.double = doubleCounts(stillNeeded.filter((r) => r.status === "not_satisfied"));
      c.notes = stillNeeded.length > 1 ? overlapNotes(stillNeeded) : [];
      c.named = counts.some((r) => r.names_courses);
      c.taken = done.has(c.code) || c.joint.some((j) => done.has(j));
      c.research = SKIP_TITLES.test(c.title);
      c.grad = GRAD_LEVELS.has(c.level);
      c.needs = first && known.has(first) && !done.has(first) ? first : null;
      c.qr_url = c.course_id ? QR_SEARCH + c.course_id : null;
      setQ(c);
    }
    data = {
      generated: catalog.generated, profile: record?.profile || {}, record: record?.courses || [],
      terms: catalog.terms, default_term: catalog.default_term,
      requirements: reqs.map(({ match, ...r }) => r), unmatched, courses: catalog.courses,
    };
    return data;
  }

  // --- lazy Q loading ---------------------------------------------------------

  const queue = [];               // link ids, front = most wanted
  const queued = new Set();       // queued or in flight
  const failed = new Set();       // no usable content this session; not retried until reload
  const dirty = new Set();        // courses whose Q numbers changed since the last render
  const unsaved = {};
  let active = 0, invalidStreak = 0, warmedUp = false, flushTimer = null, saveTimer = null;

  const markDirty = (id) => { for (const c of unitCourses.get(id) || []) dirty.add(c); };

  // Sections of these courses go to the front of the queue (newest first), so
  // whatever is on screen loads before anything requested earlier.
  function enqueue(courses, { all = false } = {}) {
    if (!qindex) return;
    const front = [];
    for (const c of courses) {
      for (const u of all ? c._units : c._units.slice(0, Q_PER_COURSE)) {
        if (qcache.has(u.id) || failed.has(u.id)) continue;
        if (queued.has(u.id)) {
          const i = queue.indexOf(u.id);
          if (i >= 0) front.push(...queue.splice(i, 1));
        } else {
          queued.add(u.id);
          front.push(u.id);
          markDirty(u.id);
        }
      }
    }
    if (!front.length) return;  // nothing new: no re-render (this runs on every render)
    queue.unshift(...front);
    pump();
    flushSoon();
  }

  function pump() {
    while (active < Q_WORKERS && queue.length && invalidStreak < STALE_AFTER) {
      const id = queue.shift();
      active++;
      loadSection(id).finally(() => {
        active--;
        queued.delete(id);
        markDirty(id);
        pump();
        flushSoon();
      });
    }
  }

  async function loadSection(id) {
    const url = unitUrl.get(id);
    try {
      let s = await Harvard.fetchReport(url);
      if (!s.ok && !warmedUp) {
        // First visit to the report site may need a sign-on hop that only a real tab completes.
        warmedUp = true;
        await chrome.runtime.sendMessage({ type: "warmup", url });
        s = await Harvard.fetchReport(url);
      }
      if (!s.ok) {
        invalidStreak++;
        failed.add(id);
        return;
      }
      invalidStreak = 0;
      const summary = { codes: s.codes, responded: s.responded, course_overall: s.course_overall,
                        workload_median: s.workload_median };
      qcache.set(id, summary);
      unsaved[Q_PREFIX + id] = summary;
    } catch (e) {
      failed.add(id);
      console.warn("Q report failed", id, e);
    }
  }

  function flushSoon() {  // recompute only the courses that changed, then one re-render and a status update
    if (!flushTimer) flushTimer = setTimeout(() => {
      flushTimer = null;
      for (const c of dirty) setQ(c);
      dirty.clear();
      window.courseFinder.render();
      status();
      if (!saveTimer && Object.keys(unsaved).length) saveTimer = setTimeout(save, 1000);
    }, 250);
  }

  function save() {
    saveTimer = null;
    const batch = { ...unsaved };
    for (const k in batch) delete unsaved[k];
    chrome.storage.local.set(batch);
  }

  // --- status bar -------------------------------------------------------------

  const messages = { record: "", q: "" };

  function status() {
    const el = document.getElementById("ext-status");
    const qStale = invalidStreak >= STALE_AFTER;
    const loading = queue.length + active;
    const recordPart = record
      ? `<span class="ok">my.harvard ✓</span> ${record.courses.length} courses, ${(data?.requirements || []).filter((r) => r.status === "not_satisfied").length} open requirements · synced ${new Date(record.synced).toLocaleString()}`
      : messages.record || `<span class="warn">Not signed in to my.harvard.</span> <a href="https://my.harvard.edu/login" target="_blank">Sign in ↗</a>, then refresh.`;
    const qPart = qindex && !qStale
      ? `<span class="ok">Q Reports ✓</span> ${unitUrl.size.toLocaleString()} course sections listed, ${qcache.size.toLocaleString()} loaded${loading ? `, ${loading} loading` : ""}`
      : messages.q || `<span class="warn">${qStale ? "Q Reports session expired." : "Not signed in to Q Reports."}</span> <a href="https://qreports.fas.harvard.edu/home/index" target="_blank">Sign in ↗</a>, then refresh.`;
    el.innerHTML = `
      <div>${recordPart}</div>
      <div>${qPart}</div>
      <div class="ext-actions">
        <button id="extRefresh">Refresh my record &amp; Q lists</button>
        <button id="extLoadQ" ${qindex ? "" : "disabled"}>Load Q for my open requirements</button>
        <button id="extClear">Clear stored data</button>
      </div>`;
    document.getElementById("extRefresh").onclick = () => sync(true);
    document.getElementById("extLoadQ").onclick = () => {
      const open = new Set(data.requirements.flatMap((r, i) => r.status === "not_satisfied" ? [i] : []));
      enqueue(data.courses.filter((c) => !c.taken && c.reqs.some((i) => open.has(i))));
    };
    document.getElementById("extClear").onclick = async () => {
      await chrome.storage.local.clear();
      location.reload();
    };
  }

  // --- sync -------------------------------------------------------------------

  async function syncRecord(force) {
    if (record && !force) return;
    messages.record = "Reading your my.harvard record…";
    status();
    try {
      const r = await Harvard.fetchRecord();
      if (r.signedIn) {
        record = { ...r, synced: Date.now() };
        await chrome.storage.local.set({ record });
      } else if (force) {
        record = null;
      }
    } catch (e) {
      console.warn(e);
    }
    messages.record = "";
  }

  async function syncIndex(force) {
    if (qindex && !force) return;
    messages.q = "Listing Q Reports…";
    status();
    try {
      const q = await Harvard.fetchReportIndex((n) => { messages.q = `Listing Q Reports… ${n.toLocaleString()} so far`; status(); });
      if (q.signedIn) {
        qindex = { ...q, synced: Date.now() };
        await chrome.storage.local.set({ qindex });
        invalidStreak = 0;
      }
    } catch (e) {
      console.warn(e);
    }
    messages.q = "";
  }

  async function sync(force) {
    await Promise.all([syncRecord(force), syncIndex(force)]);  // independent: run side by side
    window.courseFinder.setData(buildData());
    status();
  }

  async function loadCatalog() {
    // Bundled gzipped (≈1 MB instead of ≈5 MB); the browser's own decompressor unpacks it.
    const r = await fetch("catalog.json.gz");
    return new Response(r.body.pipeThrough(new DecompressionStream("gzip"))).json();
  }

  window.courseFinderSource = {
    async load() {
      let stored;
      [catalog, rules, stored] = await Promise.all([
        loadCatalog(),
        fetch("rules.json").then((r) => r.json()),
        chrome.storage.local.get(null),
      ]);
      record = stored.record;
      qindex = stored.qindex;
      // Keys are link ids; the first prototype keyed per instructor report ("<link id>|<name>").
      for (const [k, v] of Object.entries(stored)) if (k.startsWith(Q_PREFIX)) qcache.set(linkId(k.slice(Q_PREFIX.length)), v);
      if (stored.qcache) chrome.storage.local.remove("qcache");  // format used by the first prototype
      const d = buildData();
      status();
      if (!record || !qindex) setTimeout(() => sync(false), 0);  // fill in what's missing, then re-render
      return d;
    },
    onRender(visible) {
      enqueue(visible);
    },
    onExpand(course) {
      if (course) enqueue([course], { all: true });
    },
  };
})();

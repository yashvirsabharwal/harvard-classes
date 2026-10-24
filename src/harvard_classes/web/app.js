// Course Finder: filters the data exported by `uv run site` (data.json).
// State lives in the URL hash so a filtered view survives reloads.

const PAGE = 60;
const DAYS = ["M", "T", "W", "Th", "F"];
const DEFAULTS = {
  term: null, q: "", reqs: [], mode: "any", double: false, showDone: false,
  days: [], after: 7, before: 23, minRating: 1, maxHours: 25, hasQ: false,
  gened: [], divisional: [], qrd: false, hideTaken: true, hideAdvanced: true, sort: "code",
};

let data, state, shown = PAGE;
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// --- state <-> URL -----------------------------------------------------------

function loadState() {
  try {
    const saved = JSON.parse(decodeURIComponent(location.hash.slice(1)) || "{}");
    state = { ...DEFAULTS, ...saved };
  } catch {
    state = { ...DEFAULTS };
  }
  if (!data.terms.includes(state.term)) state.term = data.default_term;
}

function saveState() {
  const diff = Object.fromEntries(Object.entries(state).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(DEFAULTS[k])));
  history.replaceState(null, "", Object.keys(diff).length ? "#" + encodeURIComponent(JSON.stringify(diff)) : location.pathname);
}

function update(changes) {
  Object.assign(state, changes);
  shown = PAGE;
  saveState();
  render();
}

// --- helpers -----------------------------------------------------------------

function toHours(t) {  // "1:30pm" -> 13.5
  const m = /(\d{1,2}):(\d{2})\s*([ap]m)/i.exec(t);
  if (!m) return null;
  let h = (+m[1] % 12) + +m[2] / 60;
  if (m[3].toLowerCase() === "pm") h += 12;
  return h;
}

function meetingSpan(c) {
  const times = (c.times || "").split(";").flatMap((p) => p.split("-").map(toHours)).filter((h) => h !== null);
  return times.length ? [Math.min(...times), Math.max(...times)] : null;
}

const fmtHour = (h) => {
  const hr = Math.floor(h), min = Math.round((h - hr) * 60);
  return `${((hr + 11) % 12) + 1}${min ? ":" + String(min).padStart(2, "0") : ""}${hr < 12 ? "am" : "pm"}`;
};

const reqVisible = (i) => state.showDone || data.requirements[i].status !== "satisfied";

// --- filtering ---------------------------------------------------------------

function filtered() {
  const q = state.q.trim().toLowerCase();
  const terms = q ? q.split(/\s+/) : [];
  const days = new Set(state.days);
  return data.courses.filter((c) => {
    if (c.term !== state.term) return false;
    if (state.hideTaken && c.taken) return false;
    if (state.hideAdvanced && !c.named && (c.grad || c.research)) return false;
    if (state.reqs.length) {
      const has = (i) => c.reqs.includes(i);
      if (state.mode === "all" ? !state.reqs.every(has) : !state.reqs.some(has)) return false;
    }
    if (state.double && c.double < 2) return false;
    if (state.qrd && !c.qrd) return false;
    if (state.gened.length && !state.gened.some((g) => c.gened.includes(g))) return false;
    if (state.divisional.length && !state.divisional.some((d) => c.divisional.includes(d))) return false;
    if (days.size) {
      const cd = (c.days || "").split(",").filter(Boolean);
      if (!cd.length || cd.some((d) => !days.has(d))) return false;  // meets only on the chosen days
    }
    if (state.after > 7 || state.before < 23) {
      const span = meetingSpan(c);
      if (!span || span[0] < state.after || span[1] > state.before) return false;
    }
    if (state.hasQ && !c.q) return false;
    if (c.q && state.minRating > 1 && c.q.rating < state.minRating) return false;
    if (state.minRating > 1 && !c.q) return false;
    if (state.maxHours < 25 && (!c.q || c.q.hours === null || c.q.hours > state.maxHours)) return false;
    if (terms.length) {
      const hay = `${c.code} ${c.joint.join(" ")} ${c.title} ${c.instructors.join(" ")} ${c.department || ""} ${c.desc || ""}`.toLowerCase();
      if (!terms.every((t) => hay.includes(t))) return false;
    }
    return true;
  });
}

const SORTS = {
  code: (a, b) => a.code.localeCompare(b.code, undefined, { numeric: true }),
  rating: (a, b) => (b.q?.rating ?? 0) - (a.q?.rating ?? 0),
  hours: (a, b) => (a.q?.hours ?? 99) - (b.q?.hours ?? 99),
  responses: (a, b) => (b.q?.responses ?? 0) - (a.q?.responses ?? 0),
};

// --- rendering ---------------------------------------------------------------

function renderSidebar() {
  const termCourses = data.courses.filter((c) => c.term === state.term && !(state.hideTaken && c.taken)
    && !(state.hideAdvanced && !c.named && (c.grad || c.research)));
  $("reqs").innerHTML = data.requirements.map((r, i) => {
    if (!reqVisible(i)) return "";
    const n = termCourses.filter((c) => c.reqs.includes(i)).length;
    const status = r.status.replace("_", " ");
    return `<li><button data-req="${i}" class="${state.reqs.includes(i) ? "on" : ""}" title="${esc(status + (r.note ? " · " + r.note : ""))}">
      <span class="dot ${r.status}"></span><span class="label">${esc(r.label)}</span><span class="n">${n}</span></button></li>`;
  }).join("");
  document.querySelectorAll(".seg [data-mode]").forEach((b) => b.classList.toggle("on", b.dataset.mode === state.mode));
  $("terms").innerHTML = data.terms.map((t) =>
    `<button role="radio" aria-checked="${t === state.term}" class="${t === state.term ? "on" : ""}" data-term="${t}">${t}</button>`).join("");
  $("days").innerHTML = DAYS.map((d) => `<button data-day="${d}" class="${state.days.includes(d) ? "on" : ""}">${d}</button>`).join("");
  $("gened").innerHTML = genedCats.map((g) => `<button data-gened="${esc(g)}" class="${state.gened.includes(g) ? "on" : ""}">${esc(g)}</button>`).join("");
  $("divisional").innerHTML = divisions.map((d) => `<button data-div="${esc(d)}" class="${state.divisional.includes(d) ? "on" : ""}">${esc(d.replace(" & Engineering & Applied Science", "/SEAS"))}</button>`).join("");
  for (const id of ["double", "showDone", "qrd", "hideTaken", "hideAdvanced", "hasQ"]) $(id).checked = state[id];
  for (const id of ["after", "before", "minRating", "maxHours"]) $(id).value = state[id];
  $("afterOut").textContent = state.after > 7 ? fmtHour(state.after) : "any";
  $("beforeOut").textContent = state.before < 23 ? fmtHour(state.before) : "any";
  $("minRatingOut").textContent = state.minRating > 1 ? state.minRating.toFixed(1) : "any";
  $("maxHoursOut").textContent = state.maxHours < 25 ? state.maxHours + "h" : "any";
  $("q").value = state.q;
  $("sort").value = state.sort;
}

function courseHTML(c) {
  const reqTags = c.reqs.filter(reqVisible).map((i) => {
    const r = data.requirements[i];
    return `<span class="tag ${r.status}">${esc(r.label)}</span>`;
  });
  const flags = [
    ...c.notes.map((n) => `<span class="tag warn">${esc(n)}</span>`),
    c.needs ? `<span class="tag warn">needs ${esc(c.needs)}</span>` : "",
    c.taken ? `<span class="tag taken">taken</span>` : "",
    c.consent && c.consent !== "No Consent" ? `<span class="tag">consent: ${esc(c.consent)}</span>` : "",
  ];
  const q = c.q
    ? `<div><b>${c.q.rating.toFixed(2)}</b><span>Q rating</span></div>
       <div><b>${c.q.hours ?? "–"}</b><span>hrs/wk</span></div>
       <div><b>${c.q.responses}</b><span>${c.q.same_instructor ? "responses" : "resp · new instr"}</span></div>`
    : `<span class="none">no Q data</span>`;
  const when = [c.days?.replace(/,/g, " "), c.times].filter(Boolean).join(" · ");
  const rows = [
    ["Instructors", c.instructors.join(", ")],
    ["Also listed as", c.joint.join(", ")],
    ["Requirements", c.prereq],
    ["Recommended prep", c.prep],
    ["Notes", c.notes_text],
    ["Gen Ed", c.gened.join(", ")],
    ["Divisional", c.divisional.join(", ")],
    ["Level", c.level],
    ["Grading", c.grading],
    ["Component", c.component],
  ].filter(([, v]) => v);
  return `<li><details class="course">
    <summary>
      <div class="title-line"><span class="code">${esc(c.code)}</span><span class="ctitle">${esc(c.title)}</span></div>
      <div class="q">${q}</div>
      <div class="meta">${esc([c.instructors.slice(0, 2).join(", ") + (c.instructors.length > 2 ? " +" + (c.instructors.length - 2) : ""), when].filter(Boolean).join(" · "))}</div>
      <div class="tags">${[...reqTags, ...flags].join("")}</div>
    </summary>
    <div class="body">
      ${c.desc ? `<p>${esc(c.desc)}</p>` : ""}
      <dl>${rows.map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
      <div class="links">
        <a href="${esc(c.url)}" target="_blank" rel="noopener">my.harvard ↗</a>
        ${c.qr_url ? `<a href="${esc(c.qr_url)}" target="_blank" rel="noopener">Q Reports ↗</a>` : ""}
      </div>
    </div>
  </details></li>`;
}

function render() {
  renderSidebar();
  const list = filtered().sort(SORTS[state.sort] || SORTS.code);
  $("count").textContent = `${list.length.toLocaleString()} course${list.length === 1 ? "" : "s"} in ${state.term}`;
  $("results").innerHTML = list.length
    ? list.slice(0, shown).map(courseHTML).join("")
    : `<li class="empty">No courses match these filters.</li>`;
  $("more").hidden = list.length <= shown;
  $("more").textContent = `Show more (${(list.length - shown).toLocaleString()} left)`;
}

// --- events ------------------------------------------------------------------

function toggle(list, value) {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

function wire() {
  let typing;
  $("q").addEventListener("input", (e) => {
    clearTimeout(typing);
    typing = setTimeout(() => update({ q: e.target.value }), 150);
  });
  document.addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    if (b.dataset.term) update({ term: b.dataset.term });
    else if (b.dataset.req) update({ reqs: toggle(state.reqs, +b.dataset.req) });
    else if (b.dataset.mode) update({ mode: b.dataset.mode });
    else if (b.dataset.day) update({ days: toggle(state.days, b.dataset.day) });
    else if (b.dataset.gened) update({ gened: toggle(state.gened, b.dataset.gened) });
    else if (b.dataset.div) update({ divisional: toggle(state.divisional, b.dataset.div) });
  });
  for (const id of ["double", "showDone", "qrd", "hideTaken", "hideAdvanced", "hasQ"]) {
    $(id).addEventListener("change", (e) => update({ [id]: e.target.checked }));
  }
  for (const id of ["after", "before", "minRating", "maxHours"]) {
    $(id).addEventListener("input", (e) => update({ [id]: +e.target.value }));
  }
  $("sort").addEventListener("change", (e) => update({ sort: e.target.value }));
  $("reset").addEventListener("click", () => update({ ...DEFAULTS, term: state.term }));
  $("more").addEventListener("click", () => { shown += PAGE; render(); });
  // Our own updates use replaceState (no event); this catches pasted or edited URLs.
  window.addEventListener("hashchange", () => { loadState(); shown = PAGE; render(); });
  $("filtersToggle").addEventListener("click", (e) => {
    const open = $("sidebar").classList.toggle("open");
    e.target.setAttribute("aria-expanded", open);
  });
}

// --- start -------------------------------------------------------------------

let genedCats = [], divisions = [];

fetch("data.json").then((r) => r.json()).then((d) => {
  data = d;
  genedCats = [...new Set(d.courses.flatMap((c) => c.gened))].sort();
  divisions = [...new Set(d.courses.flatMap((c) => c.divisional))].sort();
  const p = d.profile;
  $("profile").textContent = [p.concentration, p.track && `${p.track} track`, p.level].filter(Boolean).join(" · ");
  $("generated").textContent = `Data built ${new Date(d.generated).toLocaleString()}. Q = response-weighted average of past "Evaluate the course overall" scores (1–5).`;
  loadState();
  wire();
  render();
}).catch(() => {
  $("results").innerHTML = `<li class="empty">Couldn't load data.json. Start the site with <code>uv run site</code>.</li>`;
});

# harvard-classes

Scrapes the Harvard FAS course catalog and Q Reports (course evaluations) into
one SQLite database (`data/harvard.db`) for finding good classes.

## Setup

```bash
uv sync
```

The Q Reports scraper uses your installed Google Chrome through Playwright
(falling back to Playwright's Chromium: `uv run playwright install chromium`).

## 1. Course catalog (public, no login)

```bash
uv run catalog                  # all FAS courses in the current catalog (~8,900 section pages, ~20 min)
uv run catalog --max-pages 2    # quick test
uv run catalog --reparse        # re-parse stored pages without downloading
```

Source: the JSON search endpoint behind my.harvard.edu course search, then
each course's detail page. Fills `offerings`: one row per course section per
term, with instructors, schedule, Gen Ed, divisional distribution, QRD,
prerequisites (`course_requirements`, `recommended_prep`), renumbering notes
(`course_notes`, e.g. "previously numbered CS 124"), and cross-listings.

## 2. Q Reports (needs HarvardKey)

```bash
uv run qreports --terms "2026 Spring" --limit 20   # try it first
uv run qreports                                     # every term you can see (~1,500 reports/term)
```

Sign-in: if there's no valid HarvardKey session saved in `.auth/state.json`, a
Chrome window opens for you to sign in, then closes. HarvardKey cookies only
last for the browser session, so expect to sign in again once the saved
session expires. Everything else is headless: term listings and
reports are fetched as plain HTTP requests with the session's cookies (8 in
parallel by default; the server slows down past ~8–10) and parsed with
BeautifulSoup, so no pages are rendered. A full run of ~9,400 reports takes
about an hour.

Each report is checked against the course and instructor it was listed under;
anything that doesn't match is re-fetched one at a time. The session only lasts
a few hours: when it expires mid-run (pages come back without report content),
the sign-in window reopens and the run continues once you sign in, or exits
after 15 minutes and resumes on the next run. Runs resume: reports
already downloaded are skipped. `--list-only` records which reports exist
without downloading, `--refresh` re-downloads, and `--reparse` re-parses
stored pages.

Students can only see Fall 2023 onward (6 terms as of Oct 2026). Only FAS is
scraped (includes FAS graduate courses; not HMS, HKS, etc.).

Fills:

- `qr_reports`: one row per course section × instructor per term, with
  response counts, overall course/instructor ratings and the FAS means,
  workload hours (mean/median/sd), and the "would you recommend" score.
  Cross-listed courses share one report, so it appears once per listing;
  `course_codes` holds every listing (e.g. "APMTH 115, ENG-SCI 115") for
  de-duplicating.
- `qr_questions`: every question's count, mean, FAS mean, and answer distribution.
- `qr_comments`: student comments.

## 3. Your own record (needs HarvardKey)

```bash
uv run import-me            # courses taken + degree audit from my.harvard
```

There's no public API for student records; this reads the same signed-in
pages my.harvard uses: `/student/grades` (HTML) and
`/student/academic-progress/display` (JSON wrapping the Academic Advising
Report). Fills `me_profile` (level, concentration, track), `me_courses`, and
`me_requirements` (every requirement line with status, units needed, and the
courses applied). Same `.auth/state.json` session as the Q Reports scraper.

## 4. Courses by requirement

```bash
uv run courses                     # next term's courses for each open requirement in your audit
uv run courses --req "Ethics"      # one requirement
uv run courses --multi             # courses that can count toward 2+ kinds of requirement at once
uv run courses --all --sort rating # include satisfied requirements; sort by Q rating
```

A filter, not a recommender: for each requirement it lists the courses that
count toward it, each with every other requirement it also counts toward, and
Q numbers for reference (response-weighted average rating, hours/week,
responses). Sorted by course code unless `--sort rating|hours`. Saved to
`data/courses.md`.

Overlap rules: Divisional Distribution and QRD can't share a course (Advising
Office); Gen Ed courses don't count for divisional or QRD (the catalog never
tags them); divisional and concentration can share (e.g. LS 1A in the audit);
Gen Ed counts toward a concentration only if the department says so (listed in
its rule file); one course fills one concentration slot; a secondary field and
the concentration can share only one course, and a double concentration at
most 8 credits (Registrar). Courses you've taken or are taking (including
cross-listings and renumberings) are hidden, as are graduate-level, research
and tutorial courses unless the concentration names them (or `--grad`).
"B" courses whose "A" you haven't taken are flagged.

Concentration requirements come from rule files in
`src/harvard_classes/rules/` (currently Neuroscience: Neurobiology, Class of
2029+, from the department worksheet). Gen Ed, divisional and QRD come from
catalog fields. Not modeled yet: secondary fields, prerequisites beyond A/B
sequences, schedule conflicts, and the language requirement.

## 5. Website

```bash
uv run site      # builds data/site.json and serves http://127.0.0.1:8765
```

The same data as `uv run courses` in a browsable page: search (code, title,
instructor, description), requirement filters (any/all of the selected
requirements, or courses that count toward 2+ open requirements at once),
schedule (days, start/end time), Q rating and workload limits, Gen Ed,
divisional and QRD filters, sorting, and expandable course details with
prerequisites and links to my.harvard and Q Reports. Filters are kept in the
URL. It's served on 127.0.0.1 only, since the data includes your record and
Q Report data. The page lives in `src/harvard_classes/web/`; rebuild the data
by restarting the command after re-running any of the scrapers.

## 6. Browser extension (prototype)

```bash
uv run extension   # builds extension/dist/
```

Then in Chrome: `chrome://extensions` → turn on Developer mode → **Load
unpacked** → choose `extension/dist`. Click the extension's toolbar button to
open the Course Finder.

The extension is the same finder page, but it gets each student's data from
their own browser session instead of from these scrapers:

- **Record:** reads `my.harvard.edu/student/grades` and the advising report
  with the student's cookies (sign in to my.harvard first).
- **Q Reports:** lists all reports once (the 6 term pages, fetched in
  parallel), then fetches course numbers for courses on screen, newest
  sections first and at most 4 per course; opening a course loads the rest,
  and "Load Q for my open requirements" queues every course that fills an open
  requirement. A section has one report per instructor and a cross-listed
  course one per listing, all repeating the same course numbers, so it fetches
  one per section listing and merges identical copies (the Python side counts
  them the same way). Results are cached per section, so each is fetched once.
- **Catalog:** bundled, gzipped (≈1.2 MB; public data exported from
  `data/harvard.db`), since scraping ~9,000 pages per student would be slow.

Nothing leaves the browser: data is kept in `chrome.storage.local` ("Clear
stored data" wipes it), and the extension never sees a password; it only uses
the HarvardKey session already in the browser (it may follow HarvardKey's
redirects to renew an expired Q Reports session). Source is in `extension/`
(`harvard.js`: parsers ported from the Python ones; `finder-ext.js`:
requirement matching ported from `courses.py`, syncing, lazy Q loading).

## Joining

`qr_reports.course_id` matches `offerings.course_id` (the my.harvard Course ID):

```sql
SELECT o.subject_catalog, o.title, o.term, o.gen_ed, o.divisional,
       avg(q.course_overall) AS rating, avg(q.workload_median) AS hours, sum(q.responded) AS n
FROM offerings o JOIN qr_reports q ON q.course_id = o.course_id
GROUP BY o.url ORDER BY rating DESC;
```

Raw pages are kept gzipped in `raw_pages` so parsers can change without
re-scraping. Q Report data is for Harvard affiliates; keep `data/` private.

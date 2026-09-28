# Bibliotheca — a portable, offline research catalogue for a book collection

A local, offline research database + Harvard referencing tool built over a book
collection (the reference machine's is at `G:\Library\Books`). One SQLite file is
the single source of truth; a Flask app (`app.py`, phase 6 — built) serves it on
127.0.0.1. The project is **portable**: point it at any collection on any PC.

Nothing in this project ever modifies, moves or deletes a file in the library.
Every step is read-only against the collection and writes only to this project
folder (the catalogue, reports, exports).

## Getting started on a new PC / a new collection

1. Clone the repo.
2. Create a virtualenv and install dependencies:
   ```
   python -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   ```
3. Configure: `copy config.example.json config.json`, then edit `library_root`
   to point at your book collection (leave `db_path` empty to use
   `<project>/data/library.db`). Set `flask_secret` to a random string.
4. Build the catalogue against your collection (each step is resumable):
   ```
   python init_db.py                 # create data/library.db from schema.sql
   python lib/scan.py                # inventory the tree, dedupe, CSV reports
   python lib/extract.py --workers 12  # embedded metadata + snippets
   python lib/backfill.py --timeout 180  # stubborn/damaged files
   python lib/normalise.py           # works / authors / subjects
   python lib/maintain.py            # dedupe, shelf counts, totals
   ```
5. Optional enrichments (each is a separate, idempotent post-pass):
   ```
   python lib/describe.py            # generated scope-note for every subject
   python lib/encyclopedia.py        # real definitions from SEP + Van Nostrand's PDFs
   python lib/notes.py summaries     # publisher-blurb summary notes
   python lib/notes.py quotes --desc --workers 16   # full-text quotes (pymupdf)
   ```
6. Run the app: `python app.py`, then open <http://127.0.0.1:5000>.

Notes for other machines: `config.json` and the whole `data/` folder are
git-ignored (they are per-machine). `db.py` defaults `db_path` to
`<project>/data/library.db` when it is blank, so the project works from any
working directory. The two PDF sources (`encyclopedia.py --source ...`) are found
by pattern inside your `library_root` and are skipped if absent.

## Layout

```
LibraryApp/
  config.json         library_root, db_path, host/port, scan exclusions
  schema.sql          full DDL (27 tables incl. 2 FTS5, 34 indexes, 5 triggers)
  init_db.py          create/refresh the schema (--reset drops the db,
                      --fix-fts rebuilds a broken search index)
  fix_fts.py          one-off repair for the doc_fts index (safe to re-run)
  status.py           quick row counts + last ingest runs
  app.py              phase 6: Flask UI, localhost only, port from config.json
  lib/
    db.py             config, connection PRAGMAs, schema init, helpers
    scan.py           phase 1: inventory, shelf tree, dedupe, CSV reports
    extract.py        phase 2: embedded metadata + text snippets
    backfill.py       phase 2b: timeout-guarded straggler processing
    parse_names.py    phase 3: filename grammar engine (title/author/isbn/…)
    normalise.py      phase 3: metadata merge -> works/authors/subjects
    maintain.py       post-scan: dedupe, shelf counts, reports, close runs
    cite.py           phase 5: Harvard (Cite Them Right) formatter
    export.py         phase 5b: BibTeX / RIS / CSV / DOCX / PDF export
    crossrefs.py      encyclopedia see-also graph (add/remove/queries)
    describe.py       encyclopedia scope-note backfill: fills empty
                      subject entries from catalogue data (works, sub-topics,
                      year span, top authors, sample titles). Offline,
                      idempotent; never overwrites source='manual'.
    encyclopedia.py    real encyclopedia definitions pulled from the
                      collection's own encyclopedias, written with
                      source='encyclopedia'. Multi-source runner:
                      --source sep (Stanford Encyclopedia of Philosophy HTML)
                      | van-nostrand (Van Nostrand's Scientific Encyclopedia
                      A-Z PDF) | all. Precedence: manual > encyclopedia > auto.
    pdftoc.py          extracts term -> definition entries from an A-Z PDF
                      reference (used for Van Nostrand's); ligature/artifact
                      cleaning, heading detection, see-also termination.
    notes.py           fills the Notes & quotes panel: summaries from
                      works.description blurbs, quotes from full-text
                      sentence selection. Stamps source='extracted'.
    text.py            full-text extraction (PDF/EPUB/TXT/HTML) + the
                      quotable-sentence picker used by notes.py.
    data/van_nostrand_cache.json  cached Van Nostrand entries so re-runs
                      don't re-scan ~6k PDF pages (--rebuild-cache to refresh)
    lookup.py         phase 4: offline ISBN/DOI metadata enrichment (OpenLibrary
                      + Crossref, cache-first, gap-filling only)
  templates/          Jinja pages: base, dashboard, shelves, shelf, work,
                      authors, author, subjects, subject, search, review, cite,
                      lists, list, settings (partials/ reserved for fragments)
  static/             app.css, app.js
  data/library.db     the catalogue (WAL mode)
  reports/            inventory_*.csv, duplicates_*.csv
  exports/            bibliography export target (phase 5b)
  logs/               on-disk pipeline logs (the app logs to stdout)
  tests/              166 unittest cases + 1 opt-in live integration script
  requirements.txt    flask, python-docx, reportlab
  probe_*.py          ad-hoc read-only queries against the catalogue
  smoke_final.py      route-by-route check against a running app
  diagnose-502.ps1    ops helper: trace a 502/504 back through the proxy to the origin
```

## Pipeline

| Phase | Command | What it does |
|---|---|---|
| 1 | `python lib/scan.py` | Walk the tree, record every file (path, size, mtime, kind, quick-hash), mirror the folder tree into `collections`, confirm duplicates with full SHA-1, emit `reports/inventory_*.csv` + `reports/duplicates_*.csv` |
| 2 | `python lib/extract.py --workers 12` | Embedded metadata per leader file (`file_meta`): PDF `/Title` `/Author` `/CreationDate` + page count + front-matter snippet; EPUB DC title/creator/ISBN/date; TXT/HTML head; RAR/ZIP member listing via `UnRAR.exe`; images/media recorded as text-less |
| 2b | `python lib/backfill.py --timeout 180` | Runs each remaining file in a child process with a hard timeout so damaged 20–60 MB PDFs (pypdf cache loops) cannot stall the pass. Records `empty_file` / `extract_timeout` |
| 3 | `python lib/normalise.py` | Merge filename grammar with embedded metadata, resolve authors to `Family, Given`, seed publishers/series, inherit subjects from the folder taxonomy, build `works` + `work_files` + `work_subjects`, queue low-confidence records in `review_queue` |
| post | `python lib/maintain.py` | Re-run dedupe / shelf counts / reports, close interrupted `ingest_log` runs, print catalogue totals |
| 4 | `python lib/lookup.py --workers 2` | Fill empty `works` fields from OpenLibrary (ISBN) / Crossref (DOI); see Metadata enrichment |
| post | `python lib/describe.py` | Fill empty "Encyclopedia entry" descriptions on every subject with a deterministic scope note built from the catalogue (work count, sub-topics, year span, top authors, representative titles). `--dry-run` to preview; never overwrites `source='manual'` (your own edits). |
| post | `python lib/encyclopedia.py` | Upgrade a subject to a *real* definition when the collection holds a matching encyclopedia article. Sources: `--source sep` (Stanford Encyclopedia of Philosophy, 883 local articles) and/or `--source van-nostrand` (Van Nostrand's Scientific Encyclopedia, ~9,300 A-Z entries parsed from the PDFs via `pdftoc.py`, cached in `data/`). Matches by exact title, incl. "Last, First" folder names ("Kant, Immanuel" ↔ "Immanuel Kant"). A junk filter (`is_clean_definition`) blocks contributor headers / citations mis-detected as entries. Idempotent; your `manual` edits always win. |
| post | `python lib/notes.py summaries` | Seed `kind='summary'` notes from the ~430 publisher blurbs already in `works.description` (HTML cleaned). Idempotent; manual notes win. |
| post | `python lib/notes.py quotes [--exts ...]` | Extract each book's full text (PDF/EPUB/TXT/HTML), pick 1-3 quotable sentences, and store them as `kind='quote'` notes. Resumable (skips works that already have a quote); the text is discarded. **PDFs are slow under pypdf (~29s each) — use `--exts .epub,.txt,.html,.htm` for a fast run, or add pymupdf for full PDF coverage.** |

Resume behaviour: `extract.py` and `normalise.py` only touch files with no
`file_meta` row / no `work_files` row, so they can be stopped and re-run at any
time. `--reprocess` forces a re-read.

## Web app (phase 6)

```
python app.py              # then open http://127.0.0.1:5000
python smoke_final.py      # with the app running: checks every route answers 200
```

| Route | Page |
|---|---|
| `/` | dashboard: totals, last ingest runs, shelf highlights |
| `/shelves`, `/shelves/<id>` | folder-tree shelves, works + loose files per shelf |
| `/work/<id>` | full work record + citation preview (`/edit`, `/enrich`, `/note`, `/note/<id>/delete` POST) |
| `/authors`, `/author/<id>` | author index and per-author works |
| `/subjects`, `/subject/<slug>` | encyclopedia spine (`/describe`, `/crossref`, `/crossref/delete` POST) |
| `/search` | FTS5 search over documents and notes, with term highlighting |
| `/review`, `/review/<id>/resolve` | triage of low-confidence fields |
| `/cite` | Harvard citation builder for a set of work ids |
| `/lists`, `/lists/<id>` | reading lists (`/new`, `/add`, `/remove` POST) |
| `/settings` | config, row counts, pipeline commands |

22 routes plus the static handler. The server binds to `host`/`port` from
`config.json` (default `127.0.0.1:5000`) and is not built to face the internet:
put a reverse proxy in front of it for remote access, and reach for
`diagnose-502.ps1` when that proxy starts answering 502.

### Export

`GET /export/<fmt>` downloads a bibliography as `bibtex`, `ris`, `csv`,
`docx` or `pdf`, sourced from any of:

* `ids=1,2,3` — explicit work ids (the Cite builder),
* `list_id=N` — a reading list,
* `q=words` — the same FTS query as `/search` (top 2,000 hits).

Example: `/export/bibtex?list_id=3`. DOCX/PDF are capped at 2,000 works per
download; text formats take up to 50,000. BibTeX/RIS/CSV are dependency-free;
DOCX needs `python-docx` and PDF needs `reportlab` (both in
`requirements.txt`). Export links appear on the Cite page, every reading
list, and search results.

The app is read-mostly. The only writes are manual corrections (work fields,
subjects, notes, reading lists, review triage); they are stored as
`source='manual'` and are never overwritten by a pipeline re-run.

## Metadata enrichment (phase 4)

`lib/lookup.py` fills gaps in `works` from two free, key-less public APIs:

| Source | Endpoint | Used for |
|---|---|---|
| OpenLibrary | `openlibrary.org/api/volumes/brief/isbn/<isbn>.json` | ISBN-10 / ISBN-13 → title, subtitle, authors, publisher, place, year, pages, language |
| Crossref | `api.crossref.org/works/<doi>` | DOI → title, authors, publisher, year, volume, language, abstract |

ISBN wins over DOI when a work has both. Identifiers are normalised first
(`978-0-14-044913-6` → `9780140449136`, `doi:10.1/x` / `https://doi.org/10.1/x`
→ `10.1/x`), and malformed values are rejected rather than sent upstream.

### Cache-first, offline-safe

Every response is normalised and stored in `lookup_cache` under
`isbn:<isbn>` / `doi:<doi>` before it is applied. A second run — or
`--offline` — reads only that table and never touches the network, so the
pipeline is repeatable and works with no connectivity at all.

### Provenance rules

* Only **empty** `works` fields are written; existing data is never replaced.
* Rows with `source='manual'` are skipped entirely — and not even fetched.
* `source` becomes `'lookup'` only when it was `'filename'` or empty; a richer
  source such as `'embedded'` is kept.
* Every filled field is recorded in `edit_audit` with a `lookup:<source>` note,
  and `--dry-run` reports the plan without writing anything (including the
  `publishers` row, which is only created on a real run).

### Rate limiting and identification

OpenLibrary asks every client to send a `User-Agent` naming the application
and a contact address, and throttles anonymous traffic hard. Put your address
in `config.json` (`"contact_email"`) or the `BIBLIOTHECA_CONTACT` env var; it is
appended to the `User-Agent`, which buys a 3 req/s budget instead of 1.

`--min-interval` (default `0.35`, ≈3 req/s) is a **process-wide** limit shared
by every worker thread, so raising `--workers` does not raise the request
rate. A full pass is thousands of calls, so leave the throttle on.

### Upstream data hygiene

`clean_publisher()` repairs the damage that shows up in real OpenLibrary
records before anything reaches the database:

* a lost leading character (`"andom House Publishing Group"` → `"Random House
  Publishing Group"`), via a small alias table;
* a forename stranded after the corporate tail by a MARC import (`"Wiley &
  Sons, Limited, John"` → `"John Wiley & Sons, Limited"`);
* collapsed whitespace, and outright rejection of values that are not really
  publisher names (blank, digits, punctuation, absurdly long).

### CLI

```
python lib/lookup.py                     # every work with an ISBN or DOI
python lib/lookup.py --dry-run           # report what would be filled
python lib/lookup.py --offline           # cache only, never fetch
python lib/lookup.py --limit 200         # cap the run
python lib/lookup.py --workers 2         # parallel workers (rate still capped)
python lib/lookup.py --min-interval 0.35 # seconds between requests, 0 = off
```

A work is a candidate when it has an ISBN or DOI **and** at least one field
`enrich_work` can fill is still empty, so completed records are not re-fetched
on later passes. The run reports filled / no-change / error counts and the
cache total.

In the app, the **Enrich** button on `/work/<id>` runs the same
`enrich_work()` for a single record and flashes what was filled.

## Search index (FTS5)

`doc_fts` indexes title, subtitle, author names, publisher and description,
kept in sync by three triggers on `works`.

It is deliberately a **standalone** fts5 table, not an external-content table
over `works`. That matters: the index carries derived values (author display
names, publisher name) that are not columns on `works`, and FTS5 external
content mode resolves its `'delete'` command by re-reading the content table.
With `content='works'` the update trigger asked for `works.authors`, which
does not exist, and **every** `UPDATE works` failed with *"database disk image
is malformed"* — while `PRAGMA integrity_check` and the FTS integrity-check
both still reported the database as healthy, because the fault is in the write
path, not in the stored pages.

`fix_fts.py` repairs an existing database (or `init_db.py --fix-fts`); it is a
no-op when the index is already healthy:

```
python fix_fts.py --dry-run
python fix_fts.py
```

## Data model highlights

* `files` — one row per physical file; `is_duplicate_of` points at the group
  leader (lowest id). Duplicates are **never** deleted.
* `works` ↔ `work_files` — a work groups its file + all duplicate members, so
  one book is catalogued once no matter how many copies exist.
* `authors` / `work_authors` — `sort_name` is the authority key
  (`Waal, Frans de`), `display_name` is for reading; roles: author/editor/
  translator/illustrator.
* `subjects` + `work_subjects` — the encyclopedia spine, seeded from the folder
  taxonomy (`Humanities/Esoteric Occult/Alchemy Collection` → Humanities →
  Esoteric Occult → Alchemy) and intended for manual rich descriptions.
* `crossrefs` — the see-also / broader / narrower / disputed_with graph.
* `notes`, `reading_lists`, `citations` — quotes with page refs, reading lists,
  and stored rendered Harvard references (fields kept as JSON so they can be
  regenerated if a field is corrected).
* `doc_fts` / `quote_fts` — FTS5 full-text indexes maintained by triggers.
* `lookup_cache` — normalised OpenLibrary/Crossref responses keyed by
  `isbn:<n>` / `doi:<doi>`, so enrichment is cache-first and repeatable
  offline.
* `review_queue` — every low-confidence field, for human triage in the app.
* `move_plan` / `undo_log` — the reorganisation phase writes proposals only;
  an approved plan carries an undo manifest.

## Confidence and provenance

`works.source` records where the data came from (`filename`, `embedded`,
`lookup`, `manual`) and `works.confidence` is a 0–1 blend. Anything below
`0.8` is recorded in `review_queue` with the reason. Embedded metadata wins over
filenames, but junk is filtered first: PDF titles that are really the filename
or a Word/PScript placeholder, and creators like `Usuario`/`administrator`/
`lesterha` are rejected so the filename parse is used instead.

`source='manual'` rows are never overwritten by a re-run — your corrections
always win.

## Tests

```
cd C:\Users\Durolipons\Documents\OrganizedWorkspace\LibraryApp
python -m unittest discover -s tests -t .
```

166 tests, all offline (they never touch `G:\Library\Books`, and never make a
network call):

* `tests/test_parse_names.py` (11) — filename grammar: ISBN prefixes, `Author -
  Title`, magazine issues, publisher tokens, plain titles with series numbers
  and editions, kind-by-extension.
* `tests/test_normalise.py` (22) — author sort names (initials, name
  particles), sort titles (leading articles moved), shelf-taxonomy subjects
  (publisher / release-pack / bare-year folders dropped), slugify, and merge
  precedence (embedded metadata beats the filename, OS/software creator
  strings rejected).
* `tests/test_cite.py` (10) — Harvard formatter: book, edited book, chapter,
  journal, web with accessed date, in-text et-al rules, bibliography sort +
  dedupe, missing-field validation.
* `tests/test_export.py` (12) — export: BibTeX keys/types/escaping, RIS tag
  layout per work type, CSV columns, DOCX/PDF build (headers + content).
* `tests/test_crossrefs.py` (13) — crossref graph: relation validation,
  idempotent add, self-link rejection, inbound/outbound queries, remove.
* `tests/test_describe.py` (13) — encyclopedia scope-note backfill: note
  composition (singular/plural, year span, title truncation, no-works case),
  determinism of the top-author/title aggregation, `source='manual'`
  protection, idempotent regeneration, and dry-run writing nothing.
* `tests/test_encyclopedia.py` (16) — real encyclopedia definitions: HTML strip /
  entity decode, SEP opening-definition extraction (stops at first `<h2>`,
  strips pubinfo), slug + "Last, First" variant matching, exact-over-reversal
  preference, no partial matching, `source='manual'` protection, and idempotency
  (an existing `source='encyclopedia'` entry is left alone).
* `tests/test_pdftoc.py` (11) — A-Z PDF reference extraction against a
  synthetic reportlab PDF through the real pypdf path: ligature/whitespace
  cleaning, heading detection (accepts caps terms, rejects sentences and
  `TABLE` captions), definition capture, and `See also` termination.
* `tests/test_encyclopedia.py` (22) — also covers `is_clean_definition`:
  rejects short terms, author-fragment terms ("ROBERT M"), affiliation and
  citation "definitions", and non-prose.
* `tests/test_notes.py` (11) — notes seeding: `source` column migration
  (idempotent), HTML/entity cleaning, summary seeding (extracted source, skips
  existing, truncation, dry-run).
* `tests/test_text.py` (9) — full-text + quote selection: junk filtering
  (captions, citations, leading digits, no markup/entities), determinism, and
  TXT/HTML extraction.
* `tests/test_lookup.py` (32) — enrichment: ISBN/DOI normalisation, OpenLibrary
  (both response shapes) and Crossref parsing, cache hit/miss, gap-filling only,
  publisher re-use and cleanup, `source='manual'` immutability, `edit_audit`
  rows, candidate selection, rate limiter, unknown-ISBN payloads, and `--dry-run`
  executing with no network access.

One script is deliberately **not** part of the suite because it needs the
network and a throwaway database:

```
python tests/integration_enrich.py
```

It enriches one ISBN work and one DOI work through the real Flask route, then
checks the flash message, persisted fields, `source='manual'` protection,
`edit_audit`/`lookup_cache` rows, and that `doc_fts` is still searchable after
those writes. Run it when you want to confirm the upstream APIs still behave.

## Not yet built

* Phase 4 (partial) — the thesaurus/authority merge UI; offline ISBN/DOI
  enrichment is built (`lib/lookup.py`), but there is no batch-review screen
  for the fields it fills.
* Phase 6 (partial) — reorg approval UI: `move_plan` is written by the
  catalogue, but nothing in the app reviews or approves a plan yet.
* Phase 7 — the reorganiser (`move_plan` → PowerShell mover + undo script).

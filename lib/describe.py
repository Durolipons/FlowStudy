"""Encyclopedia scope-note backfill (the 'Encyclopedia entry' panel).

lib/normalise.py seeds `subjects` from the folder taxonomy and never sets
`description`, so every subject page in the UI renders the empty placeholder
('No entry written yet...'). This pass fills each entry with a deterministic,
offline scope note derived entirely from data already in the catalogue: how many
works the subject holds, how it divides into sub-topics, the year span it
covers, its leading authors, and a few representative titles.

Rules
-----
* Offline and read-only against the collection (writes only to library.db).
* Idempotent: running it twice produces byte-identical notes.
* Never overwrites a human correction. Subjects with source='manual' are
  skipped, and any subject that already carries non-generated text is left
  alone. Only 'taxonomy' (never written) and 'auto' rows are (re)generated.
* Generated notes are stamped source='auto' so the UI's source pill makes it
  clear the entry is generated, not curated.

Usage:
    python lib/describe.py [--dry-run] [--limit N] [--verbose]
"""
from __future__ import annotations

import os
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lib.db as db  # noqa: E402

MANUAL = 'manual'          # a human wrote the entry via the UI — never touch
AUTO = 'auto'              # stamp applied to generated notes
REGENERABLE = ('taxonomy', AUTO)
MAX_AUTHORS = 3
MAX_TITLES = 3
TITLE_CHARS = 60


# ---------------------------------------------------------------------------
# gathering helpers (one pass each, so the whole run is a handful of queries)
# ---------------------------------------------------------------------------
def works_by_subject(conn) -> Dict[int, Tuple[int, Optional[int], Optional[int]]]:
    """sid -> (distinct work count, min year, max year)."""
    out: Dict[int, Tuple[int, Optional[int], Optional[int]]] = {}
    for r in conn.execute(
            "SELECT ws.subject_id AS sid, COUNT(DISTINCT ws.work_id) AS n,"
            " MIN(w.year) AS ymin, MAX(w.year) AS ymax"
            " FROM work_subjects ws JOIN works w ON w.id = ws.work_id"
            " GROUP BY ws.subject_id"):
        out[r['sid']] = (r['n'], r['ymin'], r['ymax'])
    return out


def children_by_subject(conn) -> Dict[int, int]:
    """sid -> number of direct sub-topics."""
    return {r['sid']: r['n'] for r in conn.execute(
        "SELECT parent_id AS sid, COUNT(*) AS n FROM subjects"
        " WHERE parent_id IS NOT NULL GROUP BY parent_id")}


def top_authors_by_subject(conn) -> Dict[int, List[Tuple[str, int]]]:
    """sid -> [(display_name, work count), ...] (max MAX_AUTHORS, deterministic)."""
    rows = conn.execute(
        "SELECT subject_id, display_name, n FROM ("
        "  SELECT ws.subject_id AS subject_id, a.display_name AS display_name,"
        "         COUNT(DISTINCT ws.work_id) AS n,"
        "         ROW_NUMBER() OVER (PARTITION BY ws.subject_id"
        "           ORDER BY COUNT(DISTINCT ws.work_id) DESC, a.display_name) AS rn"
        "  FROM work_subjects ws"
        "  JOIN work_authors wa ON wa.work_id = ws.work_id"
        "  JOIN authors a ON a.id = wa.author_id"
        "  GROUP BY ws.subject_id, a.id"
        ") WHERE rn <= ?", (MAX_AUTHORS,))
    out: Dict[int, List[Tuple[str, int]]] = {}
    for r in rows:
        out.setdefault(r['subject_id'], []).append((r['display_name'], r['n']))
    return out


def top_titles_by_subject(conn) -> Dict[int, List[str]]:
    """sid -> up to MAX_TITLES representative titles (highest weight first)."""
    rows = conn.execute(
        "SELECT subject_id, title FROM ("
        "  SELECT ws.subject_id AS subject_id, w.title AS title,"
        "         ROW_NUMBER() OVER (PARTITION BY ws.subject_id"
        "           ORDER BY ws.weight DESC, w.title) AS rn"
        "  FROM work_subjects ws JOIN works w ON w.id = ws.work_id"
        ") WHERE rn <= ?", (MAX_TITLES,))
    out: Dict[int, List[str]] = {}
    for r in rows:
        out.setdefault(r['subject_id'], []).append(r['title'])
    return out




# ---------------------------------------------------------------------------
# note composition (pure function — unit-testable without a database)
# ---------------------------------------------------------------------------
def _truncate(title: str, n: int = TITLE_CHARS) -> str:
    t = ' '.join((title or '').split())
    return t if len(t) <= n else t[:n - 1].rstrip() + '…'


def _year_phrase(ymin: Optional[int], ymax: Optional[int]) -> Optional[str]:
    if ymin is None or ymax is None:
        return None
    if ymin == ymax:
        return 'dated %d' % ymin
    return 'spanning %d–%d' % (ymin, ymax)


def build_note(n_works: int, ymin: Optional[int], ymax: Optional[int],
               n_child: int, authors: List[Tuple[str, int]],
               titles: List[str]) -> str:
    """Compose a plain-text scope note from one subject's catalogue facts."""
    if not n_works:
        return ('Auto-generated scope note. This subject has no catalogued '
                'works yet, so no scope could be derived from the catalogue.')

    lead = 'Auto-generated scope note. This subject holds %d catalogued %s' % (
        n_works, 'work' if n_works == 1 else 'works')
    if n_child:
        lead += ', divided into %d sub-topic%s' % (
            n_child, '' if n_child == 1 else 's')
    years = _year_phrase(ymin, ymax)
    if years:
        lead += ', ' + years
    sentences = [lead + '.']

    if authors:
        sentences.append('Leading authors: %s.' % ', '.join(
            '%s (%d)' % (name, n) for name, n in authors))
    if titles:
        sentences.append('Representative titles: %s.' % '; '.join(
            _truncate(t) for t in titles))
    return ' '.join(sentences)


# ---------------------------------------------------------------------------
# the pass
# ---------------------------------------------------------------------------
def run_describe(conn, dry_run: bool = False, limit: Optional[int] = None,
                 verbose: bool = False) -> Dict[str, int]:
    """Fill empty/auto encyclopedia entries from catalogue data.

    Returns a stats dict. Writes only to `subjects` (description, source,
    updated); never to the library tree. Safe to re-run.
    """
    works = works_by_subject(conn)
    children = children_by_subject(conn)
    authors = top_authors_by_subject(conn)
    titles = top_titles_by_subject(conn)

    rows = conn.execute(
        "SELECT id, term, source, description FROM subjects ORDER BY id"
    ).fetchall()
    if limit is not None:
        rows = rows[:limit]

    stats = {'written': 0, 'skipped_manual': 0, 'skipped_existing': 0,
             'no_works': 0}
    for r in rows:
        src = r['source'] or ''
        current = (r['description'] or '').strip()
        if src == MANUAL:
            stats['skipped_manual'] += 1
            continue
        if current and src not in REGENERABLE:
            stats['skipped_existing'] += 1
            continue
        n, ymin, ymax = works.get(r['id'], (0, None, None))
        note = build_note(n, ymin, ymax, children.get(r['id'], 0),
                          authors.get(r['id'], []), titles.get(r['id'], []))
        if not n:
            stats['no_works'] += 1
        if not dry_run:
            conn.execute(
                "UPDATE subjects SET description=?, source=?,"
                " updated=datetime('now') WHERE id=?",
                (note, AUTO, r['id']))
        stats['written'] += 1
        if verbose and stats['written'] <= 10:
            print('  %-38s %s' % (r['term'][:38], note[:88]), flush=True)
    if not dry_run:
        conn.commit()
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Fill empty encyclopedia entries with catalogue-derived "
                    "scope notes (offline, idempotent, never overwrites manual).")
    ap.add_argument('--dry-run', action='store_true',
                    help="compute and show sample notes without writing")
    ap.add_argument('--limit', type=int, default=None,
                    help="only process the first N subjects (debugging)")
    ap.add_argument('--verbose', action='store_true', help="print 10 sample notes")
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))

    cfg = db.load_config()
    with db.connect(cfg) as conn:
        db.init_db(conn)
        total = conn.execute('SELECT COUNT(*) FROM subjects').fetchone()[0]
        print('describe: %d subjects in the catalogue' % total, flush=True)
        stats = run_describe(conn, dry_run=args.dry_run, limit=args.limit,
                             verbose=args.verbose)
        if args.dry_run:
            print('--- dry run, nothing written ---')
        print('--- describe results ---')
        for k, v in stats.items():
            print('  %-18s %d' % (k, v))
        filled = conn.execute(
            "SELECT COUNT(*) FROM subjects"
            " WHERE description IS NOT NULL AND trim(description) <> ''"
        ).fetchone()[0]
        print('  subjects with an entry: %d/%d' % (filled, total))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

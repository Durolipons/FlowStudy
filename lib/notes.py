r"""Fill the (empty) Notes & quotes panel from data already in the catalogue.

The `notes` table starts empty; the existing UI only adds notes by hand. This
pass seeds it from data the pipeline already extracted:

  * summaries  - publisher blurbs in `works.description` (cleaned of HTML)
  * quotes     - quotable sentences chosen from the book's full text (phase B3/
                 B4, see lib/text.py for the extraction side)

Every seeded row is stamped source='extracted'; your own notes (source='manual')
are never touched. Idempotent: a re-run never duplicates. Reads only from
library.db; nothing in G:\Library\Books is modified.

Usage:
    python lib/notes.py summaries [--dry-run] [--limit N] [--verbose]
"""
from __future__ import annotations

import html as _html
import os
import re
import sys
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lib.db as db  # noqa: E402

MANUAL = 'manual'
EXTRACTED = 'extracted'
MAX_SUMMARY = 500
MAX_QUOTE = 320

_TAG_RE = re.compile(r'<[^>]+>')


def ensure_source_column(conn) -> bool:
    """Add notes.source if an older DB predates it. Idempotent."""
    cols = {r['name'] for r in conn.execute('PRAGMA table_info(notes)')}
    if 'source' in cols:
        return False
    conn.execute("ALTER TABLE notes ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    conn.commit()
    return True


def clean_html(t: str) -> str:
    """Strip tags/entities from a blurb and collapse whitespace."""
    if not t:
        return ''
    t = _TAG_RE.sub(' ', t)
    t = _html.unescape(t)
    t = t.replace('\xa0', ' ')
    return re.sub(r'\s+', ' ', t).strip()

    t = _html.unescape(t)


def seed_summaries(conn, dry_run: bool = False, limit: Optional[int] = None,
                   verbose: bool = False) -> Dict[str, int]:
    """Seed kind='summary' notes from works.description (publisher blurbs).

    Idempotent: a work that already has a summary (manual or extracted) is
    skipped, so re-running never duplicates and manual notes always win.
    """
    ensure_source_column(conn)
    rows = conn.execute(
        "SELECT w.id, w.title, w.description FROM works w"
        " WHERE w.description IS NOT NULL AND trim(w.description) <> ''"
        " AND NOT EXISTS (SELECT 1 FROM notes n WHERE n.work_id=w.id"
        "                 AND n.kind='summary')"
        " ORDER BY w.id").fetchall()
    if limit is not None:
        rows = rows[:limit]
    stats = {'candidates': len(rows), 'written': 0, 'skipped_existing': 0}
    for r in rows:
        body = clean_html(r['description'])
        if len(body) < 20:
            stats['skipped_existing'] += 1
            continue
        if len(body) > MAX_SUMMARY:
            body = body[:MAX_SUMMARY].rsplit(' ', 1)[0] + '…'
        if not dry_run:
            conn.execute(
                "INSERT INTO notes(work_id, kind, body, source)"
                " VALUES(?,'summary',?,?)", (r['id'], body, EXTRACTED))
        stats['written'] += 1
        if verbose and stats['written'] <= 10:
            print('  %-40s %s' % (r['title'][:40], body[:60]), flush=True)
    if not dry_run:
        conn.commit()
    return stats


def _guarded_quote(item, timeout: int):
    """Extract + pick quotes for one file in a hard-timeout child process.

    A corrupt/scan PDF can hang pymupdf for minutes (e.g. 'cycle in page
    tree'); a per-file subprocess with a timeout guarantees one bad file never
    stalls the batch. Returns (work_id, quotes, err).
    """
    import subprocess
    import json
    work_id, path, ext = item
    code = ('import sys, json; sys.path.insert(0, %r);'
            'from lib import text;'
            'w, p, e = json.loads(sys.argv[1]);'
            't, err = text.extract_text(p, e);'
            'print(json.dumps([w, text.pick_quotes(t) if not err else [], err]))'
            ) % os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        proc = subprocess.run(
            [sys.executable, '-c', code, json.dumps([work_id, path, ext])],
            capture_output=True, text=True, errors='replace', timeout=timeout)
        if proc.returncode != 0:
            return (work_id, [], 'rc=%d' % proc.returncode)
        return tuple(json.loads(proc.stdout.strip().splitlines()[-1]))
    except subprocess.TimeoutExpired:
        return (work_id, [], 'timeout>%ds' % timeout)
    except Exception as e:
        return (work_id, [], str(e)[:80])


def pending_quote_works(conn, limit: Optional[int] = None,
                        exts: Optional[List[str]] = None,
                        desc: bool = False):
    """Works that have a text-bearing leader file and no quote note yet.

    desc=True processes newest ids first, which reaches the mainstream
    text-rich books before the large scanned/dead-end folders.
    """
    from lib.text import TEXT_EXTS
    use = tuple(e.lower() for e in (exts or TEXT_EXTS))
    ph = ','.join('?' * len(use))
    rows = conn.execute(
        "SELECT DISTINCT w.id, f.path, f.ext FROM works w"
        " JOIN work_files wf ON wf.work_id = w.id"
        " JOIN files f ON f.id = wf.file_id"
        " WHERE f.state='active' AND f.is_duplicate_of IS NULL"
        " AND LOWER(f.ext) IN (" + ph + ")"
        " AND NOT EXISTS (SELECT 1 FROM notes n WHERE n.work_id = w.id"
        "                 AND n.kind = 'quote')"
        " ORDER BY w.id " + ("DESC" if desc else "ASC"), use).fetchall()
    items = [(r['id'], r['path'], r['ext']) for r in rows]
    return items[:limit] if limit else items


def seed_quotes(conn, dry_run: bool = False, limit: Optional[int] = None,
                workers: int = 8, verbose: bool = False,
                exts: Optional[List[str]] = None,
                timeout: int = 20, desc: bool = False) -> Dict[str, int]:
    """Extract full text, pick 1-3 quotable sentences, write kind='quote' notes.

    Resumable: a work that already has a quote is skipped. The text itself is
    discarded; only the chosen quotes are stored. exts filters the source types.
    Each file is extracted in a hard-timeout child process (timeout seconds)
    so a corrupt PDF can never stall the batch.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ensure_source_column(conn)
    items = pending_quote_works(conn, limit, exts, desc=desc)
    stats = {'candidates': len(items), 'written': 0, 'no_quotes': 0, 'errors': 0}
    done = 0
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = [ex.submit(_guarded_quote, it, timeout) for it in items]
        for fut in as_completed(futs):
            try:
                work_id, quotes, err = fut.result()
            except Exception:
                stats['errors'] += 1
                done += 1
                continue
            if err:
                stats['errors'] += 1
            if not quotes:
                stats['no_quotes'] += 1
            if quotes:
                if not dry_run:
                    for q in quotes:
                        conn.execute(
                            "INSERT INTO notes(work_id, kind, body, source)"
                            " VALUES(?,'quote',?,?)", (work_id, q, EXTRACTED))
                    conn.commit()
                stats['written'] += 1
            done += 1
            if verbose and done % 25 == 0:
                print('  %d/%d works, %d with quotes' % (
                    done, len(items), stats['written']), flush=True)
    return stats



def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Fill the Notes & quotes panel from catalogue data "
                    "(summaries now; quotes via full-text pass). Offline, "
                    "idempotent, never overwrites your own notes.")
    ap.add_argument('command', choices=('summaries', 'quotes'))
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--workers', type=int, default=8)
    ap.add_argument('--exts', default=None,
                    help='comma-separated source types, e.g. .epub,.txt,.html')
    ap.add_argument('--timeout', type=int, default=20,
                    help='per-file seconds before a stuck extract is killed')
    ap.add_argument('--desc', action='store_true',
                    help='process newest works first (mainstream before scans)')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))

    cfg = db.load_config()
    with db.connect(cfg) as conn:
        db.init_db(conn)
        if args.command == 'summaries':
            print('notes: seeding summaries from works.description', flush=True)
            stats = seed_summaries(conn, dry_run=args.dry_run,
                                   limit=args.limit, verbose=args.verbose)
        elif args.command == 'quotes':
            import time
            print('notes: extracting full text to pick quotes (workers=%d)'
                  % args.workers, flush=True)
            t0 = time.time()
            exts = ([e.strip().lower() for e in args.exts.split(',')]
                    if args.exts else None)
            stats = seed_quotes(conn, dry_run=args.dry_run, limit=args.limit,
                                workers=args.workers, verbose=args.verbose,
                                exts=exts, timeout=args.timeout,
                                desc=args.desc)
            print('  elapsed %.0fs' % (time.time() - t0), flush=True)
        if args.dry_run:
            print('--- dry run, nothing written ---')
        for k, v in stats.items():
            print('  %-18s %d' % (k, v))
        total = conn.execute('SELECT COUNT(*) FROM notes').fetchone()[0]
        by_kind = {r['kind']: r['n'] for r in conn.execute(
            'SELECT kind, COUNT(*) AS n FROM notes GROUP BY kind')}
        print('  notes total: %d %s' % (total, by_kind))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

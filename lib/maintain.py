"""Post-scan maintenance: dedupe, shelf counts, CSV reports, close open runs.

The full inventory scan was interrupted after its walk (the machine was busy
with the 12-worker extraction), so the tail stages never ran.  This command
re-runs them safely against whatever is currently in the DB:

    * dedupe_pass            - group by quick_hash, confirm with full SHA-1
    * update_collection_counts - refresh collections.book_count
    * write_reports          - regenerate inventory_*.csv / duplicates_*.csv
    * close_open_runs        - stamp finished/summary on ingest_log rows that
                               the interruption left open

Every stage is idempotent, so running this twice changes nothing the second
time.

Usage:
    python lib/maintain.py [--skip-dedupe] [--skip-reports] [--verbose]
"""
from __future__ import annotations
import csv
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lib.db as db  # noqa: E402
import lib.scan as scan  # noqa: E402


def reports_dir() -> str:
    base = db.BASE_DIR
    return os.path.join(base, 'reports')


def close_open_runs(conn, root: str) -> List[str]:
    """Stamp a finish time on ingest_log rows interrupted mid-run."""
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    ids = [r['run_id'] for r in conn.execute(
        "SELECT run_id FROM ingest_log WHERE finished IS NULL")]
    for rid in ids:
        seen = conn.execute("SELECT COUNT(*) FROM files WHERE ingest_run=?",
                            (rid,)).fetchone()[0]
        dup_rows = conn.execute(
            "SELECT COUNT(*) FROM files WHERE ingest_run=? AND"
            " is_duplicate_of IS NOT NULL", (rid,)).fetchone()[0]
        conn.execute(
            "UPDATE ingest_log SET finished=?, files_seen=COALESCE(files_seen,?),"
            " files_added=COALESCE(files_added,?),"
            " summary=COALESCE(summary, ?) WHERE run_id=?",
            (now, seen, seen,
             f"closed-by-maintain rows_tagged={seen} dup_rows={dup_rows}", rid))
    conn.commit()
    return ids


def counts_summary(conn, root: str) -> Dict[str, int]:
    q = lambda sql, *p: conn.execute(sql, p).fetchone()[0]
    return {
        'files_total': q("SELECT COUNT(*) FROM files WHERE library_root=?", root),
        'files_active': q("SELECT COUNT(*) FROM files WHERE library_root=?"
                          " AND state='active'", root),
        'files_missing': q("SELECT COUNT(*) FROM files WHERE library_root=?"
                           " AND state='missing'", root),
        'duplicate_rows': q("SELECT COUNT(*) FROM files WHERE library_root=?"
                            " AND is_duplicate_of IS NOT NULL", root),
        'file_meta': q("SELECT COUNT(*) FROM file_meta"),
        'works': q("SELECT COUNT(*) FROM works"),
        'authors': q("SELECT COUNT(*) FROM authors"),
        'subjects': q("SELECT COUNT(*) FROM subjects"),
        'open_reviews': q("SELECT COUNT(*) FROM review_queue"
                          " WHERE status='open'"),
        'collections': q("SELECT COUNT(*) FROM collections WHERE library_root=?",
                         root),
    }


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Post-scan maintenance.")
    ap.add_argument('--skip-dedupe', action='store_true')
    ap.add_argument('--skip-reports', action='store_true')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))

    cfg = db.load_config()
    root = cfg['library_root']
    run_id = 'maint-' + uuid.uuid4().hex[:6]
    with db.connect(cfg) as conn:
        db.init_db(conn)
        print(f"maintain: root={root}", flush=True)

        if not args.skip_dedupe:
            # small pages so progress is visible during the first (I/O heavy) pass
            groups, dupes = scan.dedupe_pass(conn, root, cur_batch=500)
            print(f"dedupe: {groups} content groups, {dupes} duplicate rows",
                  flush=True)

        ncol = scan.update_collection_counts(conn, root)
        print(f"collection counts refreshed: {ncol} shelves", flush=True)

        if not args.skip_reports:
            rep = scan.write_reports(conn, root, reports_dir(), run_id)
            print(f"reports: {os.path.basename(rep['inventory'])}"
                  f" + {os.path.basename(rep['duplicates'])}", flush=True)

        closed = close_open_runs(conn, root)
        if closed:
            print(f"closed interrupted ingest runs: {', '.join(closed)}",
                  flush=True)

        stats = counts_summary(conn, root)
        print("--- catalogue totals ---")
        for k, v in stats.items():
            print(f"  {k:16s} {v:,}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

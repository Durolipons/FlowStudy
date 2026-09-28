"""Backfill file_meta for the stragglers the bulk pass could not finish.

Most files extract in well under a second, but a handful of huge or damaged
PDFs make pypdf spin (it can loop rewriting its object cache).  Rather than
letting one bad file stall a 59k-file pass, this driver runs each remaining
file in its own child process under a hard timeout:

    * empty file (0 bytes)        -> error='empty_file'   (no child spawn)
    * child finishes in time       -> normal file_meta row
    * child exceeds --timeout      -> child killed, error='extract_timeout'
    * child crashes non-zero       -> error='extract_failed'

Checkpoint-safe: only files still missing a file_meta row are considered, and
each result is committed before the next file starts, so Ctrl-C loses nothing.

Usage:
    python lib/backfill.py [--timeout 240] [--limit N] [--verbose]
"""
from __future__ import annotations
import os
import subprocess
import sys
import time
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lib.db as db  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
EXTRACT = os.path.join(HERE, 'extract.py')


def pending(conn) -> List[Tuple[int, str, str, int]]:
    return [(r['id'], r['path'], r['ext'], r['size_bytes'] or 0) for r in
            conn.execute(
                "SELECT f.id, f.path, f.ext, f.size_bytes FROM files f"
                " LEFT JOIN file_meta m ON m.file_id = f.id"
                " WHERE f.state = 'active' AND f.is_duplicate_of IS NULL"
                " AND m.file_id IS NULL"
                " ORDER BY f.size_bytes DESC")]


def record(conn, fid: int, res: Dict) -> None:
    cols = ("file_id, title, creator, publisher, pub_date, language, isbn,"
            " pages, has_text, snippet, identifiers, description,"
            " archive_count, archive_members, raw_json, error")
    vals = (fid, res.get('title'), res.get('creator'), res.get('publisher'),
            res.get('pub_date'), res.get('language'), res.get('isbn'),
            res.get('pages'), res.get('has_text'), res.get('snippet'),
            res.get('identifiers'), res.get('description'),
            res.get('archive_count'), res.get('archive_members'),
            res.get('raw_json'), res.get('error'))
    nq = ",".join(["?"] * len(vals)) + ",datetime('now')"
    conn.execute(f"INSERT OR REPLACE INTO file_meta({cols},extracted_at)"
                 f" VALUES({nq})", vals)
    conn.commit()


def run_one(fid: int, timeout: int) -> Dict:
    """Run extract.py --only-file in a child process, hard-killed on timeout."""
    cmd = [sys.executable, EXTRACT, '--only-file', str(fid)]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              errors='replace', timeout=timeout,
                              cwd=os.path.dirname(HERE))
    except subprocess.TimeoutExpired:
        return {'error': f'extract_timeout>{timeout}s',
                'raw_json': '{"backfill": 1, "timeout": true}',
                'elapsed': round(time.time() - t0, 1)}
    if proc.returncode != 0:
        tail = (proc.stderr or '').strip().splitlines()
        msg = tail[-1] if tail else f'rc={proc.returncode}'
        return {'error': f'extract_failed: {msg[:180]}',
                'raw_json': '{"backfill": 1, "failed": true}',
                'elapsed': round(time.time() - t0, 1)}
    # child already wrote the row; signal that to the caller
    return {'_child_wrote': True,
            'elapsed': round(time.time() - t0, 1),
            'stdout': (proc.stdout or '').strip()[:400]}


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Backfill stubborn files.")
    ap.add_argument('--timeout', type=int, default=240,
                    help='per-file seconds before the child is killed')
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))

    cfg = db.load_config()
    with db.connect(cfg) as conn:
        db.init_db(conn)
        jobs = pending(conn)
        if args.limit is not None:
            jobs = jobs[:args.limit]
        total = len(jobs)
        print(f"backfill: {total} pending leaders (timeout={args.timeout}s)",
              flush=True)
        done = ok = failed = 0
        t0 = time.time()
        for fid, path, ext, size in jobs:
            done += 1
            # Empty files never need opening: pypdf would only error out.
            if size == 0:
                record(conn, fid, {
                    'has_text': 0, 'error': 'empty_file',
                    'raw_json': '{"backfill": 1, "size": 0}'})
                failed += 1
                print(f"  [{done}/{total}] id={fid} EMPTY (0 bytes)", flush=True)
                continue
            res = run_one(fid, args.timeout)
            if res.get('_child_wrote'):
                ok += 1
                print(f"  [{done}/{total}] id={fid} {ext} "
                      f"{size:,}b ok in {res['elapsed']}s", flush=True)
            else:
                record(conn, fid, res)
                failed += 1
                print(f"  [{done}/{total}] id={fid} {ext} "
                      f"{size:,}b FAILED after {res['elapsed']}s:"
                      f" {res.get('error')}", flush=True)
        print(f"backfill done: total={total} ok={ok} recorded_errors={failed}"
              f" elapsed={time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

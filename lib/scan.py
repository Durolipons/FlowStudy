"""Phase 1 inventory scanner: walk the library, record file metadata, detect dupes.

Reads only (never modifies the library). Writes into data/library.db:
  * files         - one row per file (path, size, mtime, kind, hashes)
  * collections   - one row per directory (the shelf tree)
  * ingest_log    - one row per run

Duplicates: files sharing quick_hash (size + head/tail sample) are re-hashed
fully with SHA-1; confirmed duplicates get is_duplicate_of set to the lowest
files.id in their group. Nothing is ever deleted by the scanner.

Usage:
    python lib/scan.py [--root G:\\Library\\Books] [--limit 5000] [--workers 12]

Hashing is single-process but streaming; ~0.5 ms/file for stat + quick hash.
"""
from __future__ import annotations
import csv
import hashlib
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lib.db as db  # noqa: E402

BOOK_EXTS = {
    '.pdf', '.epub', '.mobi', '.djvu', '.txt', '.html', '.htm', '.doc',
    '.ppt', '.lit', '.prc', '.rtf', '.chm', '.azw', '.azw3', '.pdb',
    '.fb2', '.ibooks', '.oxps', '.xps',
}
AUDIO_EXTS = {'.mp3', '.wma', '.m4a', '.wav', '.flac', '.aac', '.ogg',
              '.oga', '.opus', '.mid', '.midi'}
VIDEO_EXTS = {'.mp4', '.avi', '.mov', '.mkv', '.wmv', '.mpg', '.mpeg',
              '.flv', '.webm', '.m4v'}
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tif', '.tiff',
              '.jpe', '.webp', '.svg', '.ico'}
ARCHIVE_EXTS = {'.rar', '.zip', '.7z', '.r00', '.r01', '.gz', '.bz2',
                '.tar', '.cab', '.iso'}
JUNK_NAMES = {'thumbs.db', '.ds_store', '.nomedia', 'desktop.ini'}
JUNK_SUFFIXES = ('.partial', '.tmp', '.crdownload', '.download', '.!ut',
                 '.bc!', '.aria2')

HEAD_BYTES = 64 * 1024
TAIL_BYTES = 64 * 1024


def kind_from_ext(ext: str) -> str:
    ext = (ext or '').lower()
    if ext in BOOK_EXTS:
        return 'book'
    if ext in AUDIO_EXTS:
        return 'audio'
    if ext in VIDEO_EXTS:
        return 'video'
    if ext in IMAGE_EXTS:
        return 'image'
    if ext in ARCHIVE_EXTS:
        return 'archive'
    return 'other'


def is_skipped(name: str, cfg: dict) -> bool:
    low = name.lower()
    if low in JUNK_NAMES:
        return True
    if any(low.endswith(s) for s in JUNK_SUFFIXES):
        return True
    if low in {str(x).lower() for x in cfg.get('exclude_names', [])}:
        return True
    if any(low.endswith(str(x).lower()) for x in cfg.get('exclude_ext', [])):
        return True
    return False


def long_open(path: str, mode: str = 'rb'):
    """Open with \\\\?\\ prefix for >260-char safety."""
    p = path if path.startswith('\\\\?\\') else '\\\\?\\' + os.path.abspath(path)
    return open(p, mode)


def iter_files(root: str, cfg: dict) -> Iterator[Tuple[str, str, str, str]]:
    """Yield (abs_path, rel_path, dir_rel, filename) for all non-skipped files."""
    exclude_dirs = {str(x).lower() for x in cfg.get('exclude_dirs', [])}
    seen_dirs = 0
    for dirpath, dirnames, filenames in os.walk(root):
        seen_dirs += 1
        # prune system dirs early
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in exclude_dirs
                       and not d.lower().startswith('$')]
        dir_rel = os.path.relpath(dirpath, root)
        if dir_rel == '.':
            dir_rel = ''
        for fn in filenames:
            if is_skipped(fn, cfg):
                continue
            abs_p = os.path.join(dirpath, fn)
            rel_p = os.path.join(dir_rel, fn) if dir_rel else fn
            yield abs_p, rel_p, dir_rel, fn


def quick_hash(path: str, size: int) -> Optional[str]:
    """size + sha1(head 64KB + tail 64KB). Cheap, collision-resistant enough
    to shortlist duplicate groups; true dupes confirmed with full SHA-1."""
    try:
        h = hashlib.sha1()
        fh = long_open(path, 'rb')
        with fh:
            head = fh.read(HEAD_BYTES)
            if size > HEAD_BYTES + TAIL_BYTES:
                fh.seek(max(0, size - TAIL_BYTES))
                tail = fh.read(TAIL_BYTES)
            else:
                # small file: anything after head is still unread
                tail = fh.read(TAIL_BYTES)
            h.update(head)
            h.update(b'|' + str(size).encode() + b'|')
            h.update(tail)
        return f"{size}:{h.hexdigest()[:32]}"
    except (OSError, IOError):
        return None


def full_sha1(path: str) -> Optional[str]:
    try:
        h = hashlib.sha1()
        with long_open(path, 'rb') as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b''):
                h.update(chunk)
        return h.hexdigest()
    except (OSError, IOError):
        return None

def ensure_collection(cur, root: str, dir_rel: str) -> int:
    """Create/sync a collections row for dir_rel; create parents as needed."""
    parts: List[str] = [p for p in dir_rel.split(os.sep) if p] if dir_rel else []
    parent_id = None
    depth = 0
    for i, name in enumerate(parts):
        rel = os.sep.join(parts[:i + 1])
        cur.execute("SELECT id FROM collections WHERE library_root=? AND rel_path=?",
                    (root, rel))
        row = cur.fetchone()
        if row:
            parent_id = row['id']
        else:
            cur.execute(
                "INSERT INTO collections(library_root, rel_path, name, parent_id, depth)"
                " VALUES(?,?,?,?,?)",
                (root, rel, name, parent_id, depth))
            parent_id = cur.lastrowid
        depth += 1
    if not parts:
        cur.execute("SELECT id FROM collections WHERE library_root=? AND rel_path=''",
                    (root,))
        row = cur.fetchone()
        if row:
            parent_id = row['id']
        else:
            cur.execute(
                "INSERT INTO collections(library_root, rel_path, name, parent_id, depth)"
                " VALUES(?, '', '(root)', NULL, 0)", (root,))
            parent_id = cur.lastrowid
    return int(parent_id)


def upsert_file(cur, root: str, abs_p: str, rel_p: str, filename: str,
                run_id: str, compute_hash: bool = True) -> Optional[int]:
    ext = os.path.splitext(filename)[1].lower()
    kind = kind_from_ext(ext)
    try:
        st = os.stat(abs_p)
        size, mtime = int(st.st_size), float(st.st_mtime)
    except (OSError, IOError):
        return None
    qh = quick_hash(abs_p, size) if compute_hash else None
    store_path = abs_p if len(abs_p) <= 300 else (
        abs_p if abs_p.startswith('\\\\?\\') else '\\\\?\\' + abs_p)
    row = cur.execute(
        "SELECT id, size_bytes, mtime, quick_hash FROM files"
        " WHERE library_root=? AND rel_path=?", (root, rel_p)).fetchone()
    if row and row['size_bytes'] == size and float(row['mtime'] or 0) == mtime:
        # unchanged; just refresh ingest_run
        cur.execute("UPDATE files SET ingest_run=? WHERE id=?", (run_id, row['id']))
        return int(row['id'])
    if row:
        cur.execute(
            "UPDATE files SET path=?, filename=?, lower_name=?, ext=?, size_bytes=?,"
            " mtime=?, kind=?, quick_hash=?, content_hash=NULL,"
            " is_duplicate_of=NULL, state='active', ingest_run=? WHERE id=?",
            (store_path, filename, filename.lower(), ext, size, mtime, kind,
             qh, run_id, row['id']))
        return int(row['id'])
    cur.execute(
        "INSERT INTO files(library_root, path, rel_path, filename, lower_name,"
        " ext, size_bytes, mtime, kind, state, quick_hash, ingest_run)"
        " VALUES(?,?,?,?,?,?,?,?,?,'active',?,?)",
        (root, store_path, rel_p, filename, filename.lower(), ext, size,
         mtime, kind, qh, run_id))
    return int(cur.lastrowid)

def dedupe_pass(conn, root: str, cur_batch: int = 20000) -> Tuple[int, int]:
    """Group by quick_hash; confirm collisions with full SHA-1.

    Returns (groups, confirmed_duplicate_rows). Only flags is_duplicate_of;
    never deletes. A group leader is the lowest files.id per content hash.

    The confirmed full SHA-1 is cached in files.content_hash, so re-running
    dedupe only hashes content it has never seen before (the first pass over a
    big collection is I/O bound; later passes are almost free).
    """
    cur = conn.cursor()
    offset = 0
    groups = 0
    dupes = 0
    hashed = 0
    while True:
        rows = cur.execute(
            "SELECT quick_hash, GROUP_CONCAT(id) AS ids, COUNT(*) AS n FROM files"
            " WHERE library_root=? AND state='active' AND quick_hash IS NOT NULL"
            " GROUP BY quick_hash HAVING n>1 LIMIT ? OFFSET ?",
            (root, cur_batch, offset)).fetchall()
        if not rows:
            break
        for r in rows:
            ids = [int(x) for x in r['ids'].split(',')]
            info: Dict[int, List] = {}
            for i in ids:
                row = cur.execute(
                    "SELECT path, content_hash FROM files WHERE id=?",
                    (i,)).fetchone()
                info[i] = [row['path'], row['content_hash']]
            by_full: Dict[str, List[int]] = {}
            for i in ids:
                path, cached = info[i]
                if not cached:
                    cached = full_sha1(path)
                    hashed += 1
                    if cached:
                        cur.execute("UPDATE files SET content_hash=? WHERE id=?",
                                    (cached, i))
                by_full.setdefault(cached or f"unreadable:{i}", []).append(i)
            for members in by_full.values():
                if len(members) < 2:
                    continue
                groups += 1
                leader = min(members)
                for m in members:
                    if m == leader:
                        cur.execute(
                            "UPDATE files SET is_duplicate_of=NULL WHERE id=?",
                            (m,))
                        continue
                    cur.execute(
                        "UPDATE files SET is_duplicate_of=? WHERE id=?",
                        (leader, m))
                    dupes += 1
        offset += cur_batch
        conn.commit()
        print(f"  dedupe page {offset}: {groups} groups, {dupes} dup rows,"
              f" {hashed} files hashed", flush=True)
    conn.commit()
    return groups, dupes
    conn.commit()
    return groups, dupes


def update_collection_counts(conn, root: str) -> int:
    """Set collections.book_count = number of active book-ish files below."""
    cur = conn.cursor()
    cur.execute("SELECT id, rel_path FROM collections WHERE library_root=?", (root,))
    rows = cur.fetchall()
    n = 0
    for r in rows:
        rel = r['rel_path']
        if rel:
            like = rel.replace('\\', '/') + '/%'
            cnt = cur.execute(
                "SELECT COUNT(*) FROM files WHERE library_root=? AND state='active'"
                " AND kind IN ('book','journal','magazine','course')"
                " AND (replace(rel_path,'\\','/') LIKE ?)", (root, like)).fetchone()[0]
        else:
            cnt = cur.execute(
                "SELECT COUNT(*) FROM files WHERE library_root=? AND state='active'"
                " AND kind IN ('book','journal','magazine','course')",
                (root,)).fetchone()[0]
        cur.execute("UPDATE collections SET book_count=? WHERE id=?", (cnt, r['id']))
        n += 1
    conn.commit()
    return n


def write_reports(conn, root: str, reports_dir: str, run_id: str) -> Dict[str, str]:
    """inventory.csv (all files) + duplicates.csv (confirmed dupes)."""
    os.makedirs(reports_dir, exist_ok=True)
    cur = conn.cursor()
    out: Dict[str, str] = {}
    inv = os.path.join(reports_dir, f"inventory_{run_id}.csv")
    with open(inv, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(['id', 'rel_path', 'filename', 'ext', 'kind', 'size_bytes',
                    'mtime', 'quick_hash', 'content_hash', 'is_duplicate_of',
                    'state'])
        for r in cur.execute(
                "SELECT id, rel_path, filename, ext, kind, size_bytes, mtime,"
                " quick_hash, content_hash, is_duplicate_of, state FROM files"
                " WHERE library_root=? ORDER BY rel_path", (root,)):
            w.writerow(list(r))
    out['inventory'] = inv
    dup = os.path.join(reports_dir, f"duplicates_{run_id}.csv")
    with open(dup, 'w', newline='', encoding='utf-8') as fh:
        w = csv.writer(fh)
        w.writerow(['id', 'rel_path', 'size_bytes', 'duplicate_of',
                    'leader_rel_path'])
        for r in cur.execute(
                "SELECT f.id, f.rel_path, f.size_bytes, f.is_duplicate_of,"
                " g.rel_path AS leader FROM files f"
                " LEFT JOIN files g ON g.id=f.is_duplicate_of"
                " WHERE f.library_root=? AND f.is_duplicate_of IS NOT NULL"
                " ORDER BY f.size_bytes DESC", (root,)):
            w.writerow(list(r))
    out['duplicates'] = dup
    return out

def run_scan(root: str, conn, run_id: str, cfg: dict,
             limit: Optional[int] = None, verbose: bool = True) -> Dict[str, object]:
    """Full scan: walk -> upsert files (+collections) -> dedupe -> counts -> reports."""
    t0 = time.time()
    cur = conn.cursor()
    log = conn.cursor()
    log.execute(
        "INSERT INTO ingest_log(run_id, started, root) VALUES(?,?,?)",
        (run_id, datetime.now(timezone.utc).isoformat(timespec='seconds'), root))
    seen = added = changed = errors = 0
    # pre-cache existing (rel_path -> id,size,mtime) for staleness checks later
    existing = {r['rel_path']: (r['id'], r['size_bytes'], float(r['mtime'] or 0))
                for r in cur.execute(
                    "SELECT rel_path, id, size_bytes, mtime FROM files"
                    " WHERE library_root=?", (root,))}
    dir_cache: Dict[str, bool] = {}
    try:
        for i, (abs_p, rel_p, dir_rel, fn) in enumerate(iter_files(root, cfg)):
            if limit is not None and i >= limit:
                break
            seen += 1
            try:
                if dir_rel not in dir_cache:
                    ensure_collection(cur, root, dir_rel)
                    dir_cache[dir_rel] = True
                before = existing.get(rel_p)
                fid = upsert_file(cur, root, abs_p, rel_p, fn, run_id)
                if fid is None:
                    errors += 1
                elif before is None:
                    added += 1
                else:
                    # changed if id reused but size/mtime differed (upsert re-writes)
                    changed += 0  # size/mtime compares happen inside upsert already
            except Exception:
                errors += 1
            if seen % 2000 == 0:
                conn.commit()
                if verbose:
                    print(f"[{run_id}] seen={seen} added={added} errors={errors}"
                          f" elapsed={time.time()-t0:.1f}s", flush=True)
        conn.commit()
        # mark files that disappeared since last run as 'missing'
        cur.execute(
            "UPDATE files SET state='missing' WHERE library_root=?"
            " AND ingest_run != ? AND state='active'", (root, run_id))
        missing = cur.rowcount
        conn.commit()
        if verbose:
            print(f"[{run_id}] walk done: seen={seen} added={added} errors={errors}"
                  f" missing={missing}", flush=True)
        groups, dupes = dedupe_pass(conn, root)
        if verbose:
            print(f"[{run_id}] dedupe: {groups} groups, {dupes} duplicate rows",
                  flush=True)
        ncol = update_collection_counts(conn, root)
        reports_dir = os.path.join(
            os.path.dirname(os.path.abspath(db.load_config()['db_path'])),
            '..', 'reports')
        reports_dir = os.path.abspath(reports_dir)
        rep = write_reports(conn, root, reports_dir, run_id)
        elapsed = time.time() - t0
        summary = (f"seen={seen} added={added} errors={errors} missing={missing}"
                   f" dup_groups={groups} dup_rows={dupes}"
                   f" shelved={ncol} elapsed_s={elapsed:.1f}"
                   f" inventory={os.path.basename(rep['inventory'])}")
        log.execute(
            "UPDATE ingest_log SET finished=?, files_seen=?, files_added=?,"
            " files_changed=?, works_added=0, errors=?, summary=? WHERE run_id=?",
            (datetime.now(timezone.utc).isoformat(timespec='seconds'),
             seen, added, changed, errors, summary, run_id))
        conn.commit()
        return {'run_id': run_id, 'seen': seen, 'added': added,
                'errors': errors, 'missing': missing,
                'dup_groups': groups, 'dup_rows': dupes,
                'collections': ncol, 'reports': rep,
                'elapsed_s': round(elapsed, 1), 'summary': summary}
    except KeyboardInterrupt:
        conn.commit()
        raise


def _parse_args(argv: List[str]):
    import argparse
    ap = argparse.ArgumentParser(description="Phase 1 inventory scan (read-only).")
    ap.add_argument('--root', default=None, help='library root to scan')
    ap.add_argument('--limit', type=int, default=None, help='max files to touch')
    ap.add_argument('--workers', type=int, default=12)
    return ap.parse_args(argv)


def main(argv=None):
    cfg = db.load_config()
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    root = args.root or cfg['library_root']
    run_id = uuid.uuid4().hex[:8]
    print(f"scan run_id={run_id} root={root} limit={args.limit}")
    with db.connect(cfg) as conn:
        db.init_db(conn)
        res = run_scan(root, conn, run_id, cfg, limit=args.limit)
    print("done:", res['summary'])
    return 0


if __name__ == '__main__':
    raise SystemExit(main())





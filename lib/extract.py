"""Phase 2 metadata extractor: embedded metadata + front-matter text snippets.

Reads only (never modifies the library). Writes file_meta rows (one per
*leader* file; duplicate-group members reuse the leader's extraction).

  * PDF  (pypdf)   : /Title /Author /Creator /Producer /CreationDate,
                     page count, first-2-pages text snippet, has_text flag
  * EPUB (EbookLib): DC title/creator/date/identifier/language/publisher/
                     description, item count
  * TXT/HTML       : first-4KB snippet, has_text=1
  * RAR/ZIP        : member listing (count + first 500 paths), no extraction
  * other          : minimal row with error='unsupported_format' so the
                     resume logic (skip rows already present) covers them

Checkpoint/resume: rerunning only processes files lacking a file_meta row
(unless --reprocess). Multiprocess: workers extract, the main process
writes to SQLite (single writer, no locking issues).

Usage:
    python lib/extract.py [--limit 2000] [--workers 12] [--reprocess]
"""
from __future__ import annotations
import json
import logging
import os
import sys
import time
import uuid
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lib.db as db  # noqa: E402

# Damaged PDFs make pypdf emit thousands of "Object N 0 not defined." lines on
# stderr.  Silence the library so console logs show only our own progress.
for _lg in ('pypdf', 'PyPDF2', 'pdfminer'):
    logging.getLogger(_lg).setLevel(logging.CRITICAL)

EXTRACTABLE = {'.pdf', '.epub', '.txt', '.html', '.htm', '.mobi', '.doc',
               '.djvu'}
ARCHIVE_EXTS = {'.rar', '.zip'}
MEDIA_EXTS = {'.mp3', '.mp4', '.wma', '.m4a', '.avi', '.mov', '.wmv',
              '.flac', '.aac', '.ogg', '.mid', '.midi', '.mpg', '.mpeg',
              '.flv', '.webm', '.m4v'}
IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tif', '.tiff',
              '.jpe', '.webp', '.svg', '.ico'}
SNIPPET_CHARS = 800
ARCHIVE_MEMBER_CAP = 500
UNRAR_EXE = r"C:\Program Files\WinRAR\UnRAR.exe"


def _lp(path: str) -> str:
    """Long-path-safe path for open() calls."""
    if path.startswith('\\\\?\\'):
        return path
    return '\\\\?\\' + os.path.abspath(path)


def extract_pdf(path: str) -> Dict:
    out: Dict = {'has_text': 0, 'pages': None}
    try:
        from pypdf import PdfReader
    except ImportError as e:
        out['error'] = f"pypdf missing: {e}"
        return out
    try:
        r = PdfReader(_lp(path), strict=False)
    except Exception as e:
        out['error'] = f"pdf open failed: {str(e)[:200]}"
        return out
    try:
        out['pages'] = len(r.pages)
    except Exception:
        pass
    meta: Dict = {}
    try:
        m = r.metadata or {}
        for k, v in dict(m).items():
            try:
                meta[str(k)] = str(v)[:500]
            except Exception:
                pass
    except Exception:
        pass
    out['raw_json'] = json.dumps({'pdf_meta': meta,
                                  'pages': out['pages']})[:8000]
    # title/author mapping from the most useful keys
    get = lambda *ks: next((meta[k] for k in ks if meta.get(k)), None)
    title = get('/Title', 'Title')
    author = get('/Author', 'Author')
    if title and not title.lower().startswith(('c:\\', 'microsoft ', 'pscript')):
        out['title'] = title[:300]
    if author:
        out['creator'] = author[:300]
    cd = get('/CreationDate', 'CreationDate', '/ModDate')
    if cd:
        out['pub_date'] = str(cd)[:30]
    # front-matter text (first two pages, cheap)
    try:
        chunks = []
        for pg in (r.pages[:2] if out['pages'] else []):
            try:
                chunks.append(pg.extract_text() or '')
            except Exception:
                pass
        txt = ('\n'.join(chunks)).strip()
        if len(txt) >= 40:
            out['has_text'] = 1
            out['snippet'] = txt[:SNIPPET_CHARS]
    except Exception as e:
        out.setdefault('error', f"text extract failed: {str(e)[:120]}")
    return out

def extract_epub(path: str) -> Dict:
    out: Dict = {'has_text': 1}
    try:
        from ebooklib import epub
    except ImportError as e:
        out['error'] = f"EbookLib missing: {e}"
        return out
    try:
        b = epub.read_epub(_lp(path))
    except Exception as e:
        out['error'] = f"epub open failed: {str(e)[:200]}"
        return out
    def dc(tag: str) -> List[str]:
        try:
            return [str(x[0])[:500] for x in b.get_metadata('DC', tag) if x and x[0]]
        except Exception:
            return []
    title = dc('title')
    creator = dc('creator')
    date = dc('date')
    lang = dc('language')
    pub = dc('publisher')
    desc = dc('description')
    idents = dc('identifier')
    if title:
        out['title'] = title[0][:300]
    if creator:
        out['creator'] = '; '.join(creator)[:500]
    if date:
        out['pub_date'] = date[0][:30]
    if lang:
        out['language'] = lang[0][:20]
    if pub:
        out['publisher'] = pub[0][:200]
    if desc:
        out['description'] = desc[0][:1000]
    ids = []
    for i in idents:
        scheme = None
        if i.startswith(('978', '979')) and i.replace('-', '').replace(' ', '').isdigit():
            scheme = 'ISBN'
        elif i.lower().startswith('urn:isbn:'):
            scheme = 'ISBN'
            i = i.split(':', 2)[-1]
        if scheme:
            ids.append({'scheme': scheme, 'value': i[:32]})
    if ids:
        out['identifiers'] = json.dumps(ids)[:1000]
        out['isbn'] = ids[0]['value'][:32]
    try:
        out['pages'] = len(list(b.get_items()))
    except Exception:
        pass
    raw = {'title': title[:2], 'creator': creator[:3], 'date': date[:2],
           'identifier': idents[:4], 'language': lang[:2], 'publisher': pub[:2]}
    out['raw_json'] = json.dumps(raw)[:4000]
    return out


def extract_textlike(path: str, ext: str) -> Dict:
    out: Dict = {'has_text': 1}
    try:
        with open(_lp(path), 'r', encoding='utf-8', errors='replace') as fh:
            head = fh.read(4096)
        if ext in ('.html', '.htm'):
            import re as _re
            head = _re.sub(r'<script.*?</script>|<style.*?</style>', ' ',
                           head, flags=_re.S | _re.I)
            head = _re.sub(r'<[^>]+>', ' ', head)
            head = _re.sub(r'\s+', ' ', head)
        head = head.strip()
        if len(head) >= 40:
            out['snippet'] = head[:SNIPPET_CHARS]
        else:
            out['has_text'] = 0
        return out
    except Exception as e:
        out['error'] = f"text read failed: {str(e)[:200]}"
        return out


def extract_sidecar(path: str, ext: str) -> Dict:
    """Cheap sidecar pass for formats we don't open: EXIF-ish for images via
    PIL-free parsing is overkill; instead record size/pages=unknown and, for
    .doc/.djvu/.mobi, stash the first bytes hint. Everything lands behind
    sidecar=1 in raw_json so normalise.py can parse filenames instead."""
    out: Dict = {'has_text': 0}
    try:
        size = os.path.getsize(_lp(path))
    except Exception:
        size = None
    ext = (ext or '').lower()
    if ext in IMAGE_EXTS:
        out['error'] = 'image_no_text'
    elif ext in MEDIA_EXTS:
        out['error'] = 'media_no_text'
    else:
        out['error'] = 'sidecar_only'
    out['raw_json'] = (f'{{"sidecar": 1, "ext": "{ext}", "size": {size}}}')
    return out


def extract_zip(path: str) -> Dict:
    out: Dict = {}
    try:
        with zipfile.ZipFile(_lp(path), 'r') as z:
            names = z.namelist()
        out['archive_count'] = len(names)
        out['archive_members'] = json.dumps(names[:ARCHIVE_MEMBER_CAP])[:20000]
        return out
    except Exception as e:
        out['error'] = f"zip list failed: {str(e)[:200]}"
        return out


def extract_rar(path: str) -> Dict:
    out: Dict = {}
    try:
        import subprocess
        res = subprocess.run(
            [UNRAR_EXE, 'lb', '--', path], capture_output=True, text=True,
            timeout=120, errors='replace')
        names = [ln.strip() for ln in (res.stdout or '').splitlines() if ln.strip()]
        if res.returncode != 0 and not names:
            out['error'] = f"unrar failed rc={res.returncode}: {(res.stderr or '')[:200]}"
            return out
        out['archive_count'] = len(names)
        out['archive_members'] = json.dumps(names[:ARCHIVE_MEMBER_CAP])[:20000]
        return out
    except FileNotFoundError:
        out['error'] = "UnRAR.exe not found"
        return out
    except Exception as e:
        out['error'] = f"rar list failed: {str(e)[:200]}"
        return out


def extract_mobi(path: str) -> Dict:
    out: Dict = {'has_text': 1}
    try:
        with open(_lp(path), 'rb') as fh:
            head = fh.read(4096)
        texts = []
        for enc in ('utf-8', 'cp1252', 'latin-1'):
            try:
                t = head.decode(enc)
            except Exception:
                continue
            t = ''.join(ch if (ch.isprintable() or ch in '\n\t') else ' ' for ch in t)
            t = ' '.join(t.split())
            if len(t) > len(' '.join(texts)) and len(t) >= 40:
                texts = [t]
        if texts:
            out['snippet'] = texts[0][:SNIPPET_CHARS]
        else:
            out['has_text'] = 0
        return out
    except Exception as e:
        out['error'] = f"mobi read failed: {str(e)[:200]}"
        return out


def extract_doc(path: str) -> Dict:
    out: Dict = {'has_text': 0}
    try:
        import subprocess
        # antiword/catdoc are not guaranteed; try `strings`-style fallback
        with open(_lp(path), 'rb') as fh:
            blob = fh.read(200000)
        import re as _re
        runs = _re.findall(rb'[ -~]{6,}', blob)
        text = ' '.join(r.decode('ascii', 'ignore') for r in runs[:200])
        text = ' '.join(text.split())
        # Word .doc binaries contain tons of field junk; keep only if it
        # looks like prose (spaces + mixed case + some length).
        if len(text) >= 200 and sum(c.isalpha() for c in text[:2000]) > 800:
            out['has_text'] = 1
            out['snippet'] = text[:SNIPPET_CHARS]
        else:
            out['error'] = 'doc_binary_no_prose'
        return out
    except Exception as e:
        out['error'] = f"doc read failed: {str(e)[:200]}"
        return out


def extract_djvu(path: str) -> Dict:
    out: Dict = {'has_text': 0, 'error': 'djvu_embed_text_unsupported'}
    try:
        with open(_lp(path), 'rb') as fh:
            head = fh.read(8192)
        if b'TXT' in head or b'text' in head[:2000].lower():
            out['raw_json'] = '{"djvu": "has-hidden-text-chunk"}'
        else:
            out['raw_json'] = '{"djvu": "scanned-probably"}'
        return out
    except Exception as e:
        out['error'] = f"djvu read failed: {str(e)[:200]}"
        return out


EXTRACTORS = {
    '.pdf': extract_pdf,
    '.epub': extract_epub,
    '.txt': lambda p: extract_textlike(p, '.txt'),
    '.html': lambda p: extract_textlike(p, '.html'),
    '.htm': lambda p: extract_textlike(p, '.htm'),
    '.zip': extract_zip,
    '.rar': extract_rar,
    '.mobi': extract_mobi,
    '.doc': extract_doc,
    '.djvu': extract_djvu,
}


def extract_one(job: Tuple[int, str, str]) -> Tuple[int, Dict]:
    """Worker entry: (file_id, abs_path, ext) -> (file_id, result dict)."""
    fid, path, ext = job
    ext = (ext or '').lower()
    fn = EXTRACTORS.get(ext)
    if fn is None:
        if ext in IMAGE_EXTS:
            return fid, {'error': 'image_no_text', 'has_text': 0}
        if ext in MEDIA_EXTS:
            return fid, {'error': 'media_no_text', 'has_text': 0}
        return fid, extract_sidecar(path, ext)
    try:
        res = fn(path)
    except Exception as e:  # last-resort guard: workers must never die
        res = {'error': f"extractor crashed: {str(e)[:200]}"}
    res.setdefault('has_text', None)
    return fid, res

def pending_jobs(conn, root: str, limit: Optional[int],
                 reprocess: bool) -> List[Tuple[int, str, str]]:
    """Leaders = active files that are not duplicates, lacking file_meta rows."""
    cur = conn.cursor()
    q = ("SELECT f.id, f.path, f.ext FROM files f"
         " LEFT JOIN file_meta m ON m.file_id=f.id"
         " WHERE f.library_root=? AND f.state='active'"
         " AND f.is_duplicate_of IS NULL")
    params: List = [root]
    if not reprocess:
        q += " AND m.file_id IS NULL"
    q += " ORDER BY f.size_bytes DESC"
    if limit is not None:
        q += " LIMIT ?"
        params.append(limit)
    return [(r['id'], r['path'], r['ext']) for r in cur.execute(q, params)]


def write_meta(cur, fid: int, res: Dict) -> None:
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
    cur.execute(f"INSERT OR REPLACE INTO file_meta({cols},extracted_at)"
                f" VALUES({nq})", vals)


def propagate_leader_meta(conn, root: str) -> int:
    """Copy file_meta from each leader to dup members missing rows.

    Members reuse the leader's row content; only file_id changes.
    Returns rows copied.
    """
    cur = conn.cursor()
    n = 0
    cols = ("title, creator, publisher, pub_date, language, isbn, pages,"
            " has_text, snippet, identifiers, description, archive_count,"
            " archive_members, raw_json, error")
    for r in cur.execute(
            "SELECT f.id AS member, f.is_duplicate_of AS leader"
            " FROM files f LEFT JOIN file_meta m ON m.file_id=f.id"
            " WHERE f.library_root=? AND f.state='active'"
            " AND f.is_duplicate_of IS NOT NULL AND m.file_id IS NULL", (root,)):
        m = cur.execute(
            f"SELECT {cols} FROM file_meta WHERE file_id=?",
            (r['leader'],)).fetchone()
        if not m:
            continue
        vals = [r['member']] + list(m)
        cur.execute(
            "INSERT OR REPLACE INTO file_meta(file_id, title, creator,"
            " publisher, pub_date, language, isbn, pages, has_text, snippet,"
            " identifiers, description, archive_count, archive_members,"
            " raw_json, error, extracted_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,datetime('now'))", vals)
        n += 1
        if n % 2000 == 0:
            conn.commit()
    conn.commit()
    return n

def run_extract(cfg: dict, conn, limit: Optional[int], workers: int,
                reprocess: bool, verbose: bool = True) -> Dict:
    root = cfg['library_root']
    jobs = pending_jobs(conn, root, limit, reprocess)
    total = len(jobs)
    if verbose:
        print(f"extract: {total} leader files pending"
              f" (limit={limit} workers={workers} reprocess={reprocess})",
              flush=True)
    t0 = time.time()
    done = ok = errs = 0
    batch: List[Tuple[int, Dict]] = []
    with ProcessPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(extract_one, j): j for j in jobs}
        for fut in as_completed(futs):
            fid, res = fut.result()
            batch.append((fid, res))
            done += 1
            if res.get('error'):
                errs += 1
            else:
                ok += 1
            if len(batch) >= 200:
                cur = conn.cursor()
                for bfid, bres in batch:
                    write_meta(cur, bfid, bres)
                conn.commit()
                batch.clear()
            if verbose and done % 500 == 0:
                print(f"extract: {done}/{total} ok={ok} err={errs}"
                      f" elapsed={time.time()-t0:.0f}s", flush=True)
    if batch:
        cur = conn.cursor()
        for bfid, bres in batch:
            write_meta(cur, bfid, bres)
        conn.commit()
    copied = propagate_leader_meta(conn, root)
    elapsed = time.time() - t0
    if verbose:
        print(f"extract done: {done} leaders ok={ok} err={errs}"
              f" member_rows_copied={copied} elapsed={elapsed:.0f}s", flush=True)
    return {'leaders': done, 'ok': ok, 'errors': errs,
            'copied': copied, 'elapsed_s': round(elapsed, 1)}

def _migrate(conn) -> None:
    """Create file_meta table/indexes for DBs built before Phase 2."""
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS file_meta("
        " file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,"
        " title TEXT, creator TEXT, publisher TEXT, pub_date TEXT,"
        " language TEXT, isbn TEXT, pages INTEGER, has_text INTEGER,"
        " snippet TEXT, identifiers TEXT, description TEXT,"
        " archive_count INTEGER, archive_members TEXT, raw_json TEXT,"
        " error TEXT, extracted_at TEXT NOT NULL DEFAULT (datetime('now')));"
        " CREATE INDEX IF NOT EXISTS idx_fm_isbn ON file_meta(isbn);"
        " CREATE INDEX IF NOT EXISTS idx_fm_title ON file_meta(title);")
    conn.commit()


def _parse_args(argv: List[str]):
    import argparse
    ap = argparse.ArgumentParser(description="Phase 2 metadata extraction.")
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--workers', type=int, default=12)
    ap.add_argument('--reprocess', action='store_true')
    ap.add_argument('--only-file', type=int, default=None,
                    help='extract one file id only (used by lib/backfill.py)')
    ap.add_argument('--dump-json', default=None,
                    help='with --only-file: write the result to this JSON path')
    return ap.parse_args(argv)


def run_single(fid: int, dump_json: Optional[str]) -> int:
    """Extract exactly one file.

    The backfill driver runs this in a child process under a hard timeout so
    pathological PDFs cannot stall the whole catalogue pass.
    """
    cfg = db.load_config()
    with db.connect(cfg) as conn:
        db.init_db(conn)
        _migrate(conn)
        row = conn.execute(
            "SELECT id, path, ext FROM files WHERE id=?", (fid,)).fetchone()
        if row is None:
            print(f"no such file id={fid}", file=sys.stderr)
            return 2
        _, res = extract_one((row['id'], row['path'], row['ext']))
        if dump_json:
            with open(dump_json, 'w', encoding='utf-8') as fh:
                json.dump(res, fh)
        else:
            cur = conn.cursor()
            write_meta(cur, fid, res)
            conn.commit()
        print(json.dumps({'file_id': fid, 'error': res.get('error'),
                          'title': res.get('title'),
                          'pages': res.get('pages')}))
    return 0


def main(argv=None):
    cfg = db.load_config()
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.only_file is not None:
        return run_single(args.only_file, args.dump_json)
    with db.connect(cfg) as conn:
        db.init_db(conn)
        _migrate(conn)
        res = run_extract(cfg, conn, args.limit, args.workers, args.reprocess)
    print("done:", res)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
# EXTRACT_END_CLEAN





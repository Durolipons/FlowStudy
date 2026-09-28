"""Phase 3 normalisation: filename parse + embedded metadata -> works catalogue.

Reads:  files (leaders), file_meta, collections.
Writes: works, work_files, authors, author_alias-staging, publishers, series,
        subjects (from folder taxonomy), work_subjects, review_queue.

Rules:
  * Embedded file_meta title/creator beats the filename parse when present.
  * Filename grammar (lib/parse_names) fills title/author/year/publisher/isbn.
  * Confidence = weighted blend; below 0.8 the work lands entries in
    review_queue (one row per weak field).
  * Author names normalised to sort form "Family, Given"; folder taxonomy
    (e.g. Humanities/Esoteric Occult/Alchemy Collection) seeds subjects and
    attaches work_subjects with source='taxonomy'.
  * Manual rows (source='manual') are never touched: normalise only creates
    or updates rows whose source != 'manual'.

Usage:
    python lib/normalise.py [--limit 2000] [--reprocess] [--report-only]
"""
from __future__ import annotations
import json
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lib.db as db  # noqa: E402
from lib.parse_names import parse_filename  # noqa: E402

SORT_ARTICLES = ('the ', 'a ', 'an ')
SHELF_SUBJECT_STOP = {'collection', 'collections', 'books', 'book', 'ebooks',
                      'ebook', 'library', 'pack', 'set', 'series', 'new'}

# Folder names that are publisher imprints or release-group packs rather than
# subjects. 'Unsorted/New Books/CUP' and '.../ePub - 6539 [ECLiPSE]' must not
# become encyclopedia entries.
SHELF_PUBLISHER_FOLDERS = {
    'cup', 'oup', 'cambridge', 'cambridge university press', 'oxford',
    'oxford university press', 'routledge', 'springer', 'wiley', 'blackwell',
    'palgrave', 'palgrave macmillan', 'pearson', 'elsevier', 'macmillan',
    'taylor', 'taylor & francis', 'sage', 'bloomsbury', 'penguin',
    'harpercollins', 'random house', 'hodder', 'norton', 'crc', 'mit',
    'mit press', 'princeton', 'yale', 'harvard', 'stanford', 'reaktion',
    'rebellion', 'versus', 'verso', 'vintage', 'faber', 'granta',
}
SHELF_JUNK_MARKERS = ('best seller', 'bestseller', 'epub', 'mobi', 'pdf',
                      'ebook', 'retail', 'repack', 'gnv64', 'eclipse',
                      'stormrg', 'tkrg', 'trg', 'tb', 'true pdf', 'scan',
                      'libgen', 'library genesis', 'myanonamouse', 'calibre')
SHELF_JUNK_TAG_RE = re.compile(r'\[[^\]]*\]|\((?:\d{3,4}|gnv64|true pdf)\)', re.I)

# Creators that are really software or the machine's own user, not the author.
# The Windows account name is added at runtime: PDFs exported by the library
# owner carry /Author = <username>, e.g. 'Durolipons'.
JUNK_CREATORS = {
    'unknown', 'anonymous', 'none', 'user', 'usuario', 'administrator',
    'admin', 'owner', 'pc', 'acrobat', 'scan', 'scanner', 'scanned',
    'hp', 'canon', 'epson', 'fujitsu', 'xerox', 'brother', 'kodak',
    'lesterha', 'deepalip', 'ngooch', 'fqk', 'pinnacle', 'mike hendry',
    'microsoft', 'microsoft office', 'word', 'windows', 'unregistered',
    'adobe', 'adobe systems', 'adobe acrobat', 'pdfcreator', 'pdf24',
    'djvu', 'gnu ghostscript', 'latex', 'tex', 'office', 'home',
}
for _env_user in (os.environ.get('USERNAME'), os.environ.get('USER'),
                  os.environ.get('LOGNAME')):
    if _env_user:
        JUNK_CREATORS.add(_env_user.strip().lower())


def sort_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    t = title.strip()
    low = t.lower()
    for art in SORT_ARTICLES:
        if low.startswith(art) and len(t) > len(art) + 2:
            return t[len(art):].strip() + ', ' + t[:len(art)].strip().title()
    return t


def author_sort_name(display: str) -> Tuple[str, Optional[str], Optional[str]]:
    """'Alan W. Watts' -> ('Watts, Alan W.', 'Alan W.', 'Watts').

    Handles 'Family, Given' input, particles (de/van/von), and suffixes.
    """
    d = (display or '').strip().strip(',')
    if not d:
        return ('Unknown', None, None)
    if ',' in d:
        fam, giv = [x.strip() for x in d.split(',', 1)]
        return (f"{fam}, {giv}" if giv else fam, giv or None, fam or None)
    parts = d.split()
    if len(parts) == 1:
        return (parts[0], None, parts[0])
    # particles attach to family: de Waal, van Gogh, von Daniken
    particles = {'de', 'da', 'di', 'van', 'von', 'der', 'den', 'del',
                 'la', 'le', 'du', 'des', 'ter', "d'", "l'"}
    fam = parts[-1]
    i = len(parts) - 2
    while i >= 0 and parts[i].lower().rstrip('.') in particles:
        fam = parts[i] + ' ' + fam
        i -= 1
    giv = ' '.join(parts[:i + 1]) if i >= 0 else None
    return (f"{fam}, {giv}" if giv else fam, giv, fam)

def slugify(term: str) -> str:
    s = (term or '').lower()
    s = re.sub(r"['']", '', s)
    s = re.sub(r'[^a-z0-9]+', '-', s).strip('-')
    return s or 'untitled'


def shelf_subjects(dir_rel: str) -> List[str]:
    """Derive subject terms from a folder path, dropping stopwords, publisher
    imprints and release-pack names.

    'Humanities/Esoteric Occult/Alchemy Collection' ->
      ['Humanities', 'Esoteric Occult', 'Alchemy']
    'Unsorted/New Books/CUP' -> ['Unsorted']   (CUP an imprint, 'New'/'Books'
      are structural stopwords)
    """
    out = []
    for part in (dir_rel or '').replace('\\', '/').split('/'):
        p = SHELF_JUNK_TAG_RE.sub(' ', part).strip()
        if not p:
            continue
        low = p.lower()
        # drop publisher imprints / release packs outright
        if low in SHELF_PUBLISHER_FOLDERS:
            continue
        if any(m in low for m in SHELF_JUNK_MARKERS):
            continue
        if re.fullmatch(r'(19|20)\d{2}', low):      # bare year folders
            continue
        words = [w for w in p.split() if w.lower() not in SHELF_SUBJECT_STOP]
        term = ' '.join(words).strip(' -_')
        if term and len(term) >= 2:
            out.append(term)
    return out


def ensure_subject(cur, term: str, parent_id: Optional[int] = None,
                   kind: str = 'subject') -> int:
    slug = slugify(term)
    row = cur.execute("SELECT id, parent_id FROM subjects WHERE slug=?",
                      (slug,)).fetchone()
    if row:
        if parent_id is not None and row['parent_id'] is None:
            cur.execute("UPDATE subjects SET parent_id=? WHERE id=?",
                        (parent_id, row['id']))
        return int(row['id'])
    cur.execute("INSERT INTO subjects(term, slug, kind, parent_id, source)"
                " VALUES(?,?,?,?,'taxonomy')", (term, slug, kind, parent_id))
    return int(cur.lastrowid)


def ensure_taxonomy(cur, dir_rel: str) -> List[int]:
    """Create/locate the subject chain for a folder path; return subject ids."""
    terms = shelf_subjects(dir_rel)
    ids: List[int] = []
    parent = None
    for t in terms:
        parent = ensure_subject(cur, t, parent)
        ids.append(parent)
    return ids


def ensure_author(cur, display: str) -> Optional[int]:
    sort, giv, fam = author_sort_name(display)
    if not sort or sort.lower() == 'unknown':
        return None
    row = cur.execute("SELECT id FROM authors WHERE sort_name=?",
                      (sort,)).fetchone()
    if row:
        return int(row['id'])
    disp = display.strip()
    if ',' in disp:
        fam_p, giv_p = [x.strip() for x in disp.split(',', 1)]
        disp = f"{giv_p} {fam_p}".strip()
    cur.execute("INSERT INTO authors(sort_name, display_name, given, family)"
                " VALUES(?,?,?,?)", (sort, disp or sort, giv, fam))
    return int(cur.lastrowid)


def ensure_publisher(cur, name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    name = ' '.join(name.split())
    row = cur.execute("SELECT id FROM publishers WHERE name=?", (name,)).fetchone()
    if row:
        return int(row['id'])
    cur.execute("INSERT INTO publishers(name) VALUES(?)", (name,))
    return int(cur.lastrowid)
# --- merge: prefer embedded metadata, fill gaps from filename ---------------
def merge_fields(fn: str, ext: str, meta: Optional[dict],
                 dir_rel: str) -> Dict:
    """Return work-worthy fields + provenance + confidence from both sources."""
    p = parse_filename(fn)
    out: Dict = {
        'title': None, 'subtitle': p.subtitle, 'authors': [], 'year': p.year,
        'publisher': p.publisher, 'isbn10': p.isbn10, 'isbn13': p.isbn13,
        'edition': p.edition, 'series_number': p.series_number,
        'work_type': p.work_type or 'book', 'kind': p.kind or 'book',
        'language': None, 'pages': None, 'description': None,
        'source_parts': [], 'conf': 0.0, 'signals': list(p.signals),
    }
    w = 0.0
    # filename base layer
    if p.title:
        out['title'] = p.title
        w += 0.35 * p.confidence
        out['source_parts'].append('filename:title')
    if p.authors:
        out['authors'] = list(p.authors)
        w += 0.20 * p.confidence
        out['source_parts'].append('filename:authors')
    if p.year:
        w += 0.12
        out['source_parts'].append('filename:year')
    if p.publisher:
        w += 0.10
        out['source_parts'].append('filename:publisher')
    if p.isbn:
        w += 0.10
        out['source_parts'].append('filename:isbn')
    # embedded layer overrides title/creator/publisher when present
    mt = (meta.get('title') or '').strip() if meta else ''
    mc = (meta.get('creator') or '').strip() if meta else ''
    fn_low = fn.lower()
    # junk PDF titles: the filename itself with an extension, creator software,
    # or 'untitled' placeholders. Prefer the filename parse instead.
    junk_title = False
    if mt:
        mtl = mt.lower()
        junk_title = (
            mtl in ('untitled', 'untitled 1', 'untitled document', 'unknown',
                    'scan', 'scanned document')
            or mtl == fn_low
            or mtl == os.path.splitext(fn_low)[0]
            or mtl.startswith(('c:\\', 'microsoft word', 'microsoft powerpoint',
                               'microsoft excel', 'pscript', 'word.', 'acrobat '))
            or mtl.endswith(('.pdf', '.doc', '.ppt', '.tif', '.tiff'))
            or len(mt) < 3)
    junk_creator = False
    if mc:
        mcl = mc.lower()
        junk_creator = (
            mcl in JUNK_CREATORS
            or mcl.startswith(('adobe ', 'microsoft ', 'mac os x', 'windows '))
            or mcl.endswith((' .exe', '.dll'))
            or len(mcl) < 3)
    if meta:
        if not junk_title and len(mt) >= 3:
            out['title'] = mt
            w += 0.30
            out['source_parts'].append('embedded:title')
        if not junk_creator and len(mc) >= 2:
            parts = [x.strip() for x in re.split(r'\s*;\s*', mc) if x.strip()]
            if parts:
                out['authors'] = parts[:6]
                w += 0.25
                out['source_parts'].append('embedded:authors')
        # The fields below are independent of the creator: a PDF with a title
        # and ISBN but no /Author must still keep its ISBN, publisher and date.
        if meta.get('publisher'):
            out['publisher'] = str(meta['publisher'])[:200]
            w += 0.08
            out['source_parts'].append('embedded:publisher')
        if meta.get('isbn'):
            iso = re.sub(r'[^0-9Xx]', '', str(meta['isbn'])).upper()
            if len(iso) == 13 and iso.startswith(('978', '979')):
                out['isbn13'] = iso
                w += 0.05
                out['source_parts'].append('embedded:isbn')
            elif len(iso) == 10:
                out['isbn10'] = iso
                w += 0.05
                out['source_parts'].append('embedded:isbn')
        if meta.get('pages'):
            try:
                n = int(meta['pages'])
                # for EPUB 'pages' is really an item count, so only trust PDFs
                if ext == '.pdf' and 0 < n < 20000:
                    out['pages'] = n
            except (TypeError, ValueError):
                pass
        if meta.get('language'):
            out['language'] = str(meta['language'])[:20]
        if meta.get('description'):
            out['description'] = str(meta['description'])[:2000]
        yd = meta.get('pub_date')
        if yd:
            m = re.search(r'(1[89]\d{2}|20\d{2})', str(yd))
            if m:
                try:
                    yv = int(m.group(1))
                    if 1500 <= yv <= 2032 and not out['year']:
                        out['year'] = yv
                        w += 0.08
                        out['source_parts'].append('embedded:year')
                except ValueError:
                    pass
    if not out['title'] or len(out['title']) < 2:
        out['title'] = os.path.splitext(fn)[0][:300]
        out['source_parts'].append('filename:raw-fallback')
    out['conf'] = round(min(0.98, w), 3)
    return out

# --- work writer ----------------------------------------------------------
REVIEW_THRESHOLD = 0.8


def upsert_work(cur, f: Dict, meta_row: Optional[dict],
                dir_rel: str, root: str) -> Tuple[int, bool, List[Dict]]:
    """Create or update the work for one leader file. Returns
    (work_id, created, review_items). Never touches source='manual' rows."""
    fn, ext, fid = f['filename'], f['ext'], f['id']
    meta = dict(meta_row) if meta_row else None
    if meta and meta.get('file_id') is not None:
        meta.pop('file_id', None)
    m = merge_fields(fn, ext, meta, dir_rel)
    pub_id = ensure_publisher(cur, m['publisher'])
    stitle = sort_title(m['title'])
    row = cur.execute(
        "SELECT id, source FROM works WHERE id IN (SELECT work_id FROM work_files"
        " WHERE file_id=?) LIMIT 1", (fid,)).fetchone()
    created = False
    if row and row['source'] == 'manual':
        wid = int(row['id'])
    elif row:
        wid = int(row['id'])
        cur.execute(
            "UPDATE works SET title=?, subtitle=?, sort_title=?, work_type=?,"
            " edition=?, publisher_id=?, year=?, language=?, isbn10=?,"
            " isbn13=?, pages=?, description=?, source=?, confidence=?,"
            " updated=datetime('now') WHERE id=?",
            (m['title'], m['subtitle'], stitle, m['work_type'], m['edition'],
             pub_id, m['year'], m['language'], m['isbn10'], m['isbn13'],
             m['pages'], m['description'],
             'embedded' if 'embedded:title' in m['source_parts'] else 'filename',
             m['conf'], wid))
        cur.execute("DELETE FROM work_authors WHERE work_id=?", (wid,))
    else:
        cur.execute(
            "INSERT INTO works(title, subtitle, sort_title, work_type, edition,"
            " publisher_id, year, language, isbn10, isbn13, pages, description,"
            " source, confidence) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (m['title'], m['subtitle'], stitle, m['work_type'], m['edition'],
             pub_id, m['year'], m['language'], m['isbn10'], m['isbn13'],
             m['pages'], m['description'],
             'embedded' if 'embedded:title' in m['source_parts'] else 'filename',
             m['conf']))
        wid = int(cur.lastrowid)
        created = True
    cur.execute("INSERT OR IGNORE INTO work_files(work_id, file_id)"
                " VALUES(?,?)", (wid, fid))
    # authors
    seq = 0
    for a in (m['authors'] or [])[:6]:
        aid = ensure_author(cur, a)
        if aid is None:
            continue
        seq += 1
        cur.execute("INSERT OR IGNORE INTO work_authors(work_id, author_id,"
                    " role, seq) VALUES(?,?, 'author', ?)", (wid, aid, seq))
    # subjects from folder taxonomy
    for sid in ensure_taxonomy(cur, dir_rel):
        cur.execute("INSERT OR IGNORE INTO work_subjects(work_id, subject_id,"
                    " weight, source) VALUES(?,?,1.0,'taxonomy')", (wid, sid))
    # duplicate members share the leader's work
    for mem in cur.execute("SELECT id FROM files WHERE is_duplicate_of=? AND"
                           " state='active'", (fid,)).fetchall():
        cur.execute("INSERT OR IGNORE INTO work_files(work_id, file_id)"
                    " VALUES(?,?)", (wid, int(mem['id'])))
    # review queue for weak fields (clear stale open items first so that a
    # re-run does not stack duplicates)
    cur.execute("DELETE FROM review_queue WHERE work_id=? AND file_id=?"
                " AND status='open'", (wid, fid))
    reviews: List[Dict] = []
    if m['conf'] < REVIEW_THRESHOLD:
        reviews.append({'field': 'record', 'current': m['title'],
                        'suggested': ';'.join(m['source_parts']),
                        'confidence': m['conf'],
                        'reason': 'overall confidence below threshold'})
    if not m['authors']:
        reviews.append({'field': 'author', 'current': None, 'suggested': None,
                        'confidence': m['conf'], 'reason': 'no author found'})
    if not m['year']:
        reviews.append({'field': 'year', 'current': None, 'suggested': None,
                        'confidence': m['conf'], 'reason': 'no year found'})
    for rv in reviews:
        cur.execute("INSERT INTO review_queue(work_id, file_id, field, current,"
                    " suggested, confidence, reason, status) VALUES(?,?,?,?,?,"
                    "?,?,'open')",
                    (wid, fid, rv['field'], rv['current'], rv['suggested'],
                     rv['confidence'], rv['reason']))
    return wid, created, reviews

# --- driver -----------------------------------------------------------------
BOOKISH = ('.pdf', '.epub', '.mobi', '.djvu', '.txt', '.html', '.htm',
           '.doc', '.ppt', '.lit', '.prc', '.rtf', '.chm', '.azw', '.azw3')


def pending_leaders(conn, root: str, limit: Optional[int],
                    reprocess: bool = False) -> List[Dict]:
    cur = conn.cursor()
    q = ("SELECT f.id, f.filename, f.ext, f.rel_path FROM files f"
         " WHERE f.library_root=? AND f.state='active'"
         " AND f.is_duplicate_of IS NULL")
    params: List = [root]
    if BOOKISH:
        q += " AND f.ext IN (%s)" % ",".join("?" * len(BOOKISH))
        params.extend(BOOKISH)
    if not reprocess:
        q += " AND NOT EXISTS (SELECT 1 FROM work_files wf WHERE wf.file_id=f.id)"
    q += " ORDER BY f.size_bytes DESC"
    if limit is not None:
        q += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in cur.execute(q, params)]


def run_normalise(cfg: dict, conn, limit: Optional[int] = None,
                  reprocess: bool = False, verbose: bool = True) -> Dict:
    root = cfg['library_root']
    leaders = pending_leaders(conn, root, limit, reprocess)
    if verbose:
        print(f"normalise: {len(leaders)} leader files pending"
              f" (limit={limit} reprocess={reprocess})", flush=True)
    import time as _t
    t0 = _t.time()
    cur = conn.cursor()
    created = updated = review_n = 0
    for i, f in enumerate(leaders):
        rel = f['rel_path'] or ''
        dir_rel = os.path.dirname(rel)
        mrow = cur.execute("SELECT * FROM file_meta WHERE file_id=?",
                           (f['id'],)).fetchone()
        meta = dict(mrow) if mrow else None
        _, was_created, revs = upsert_work(cur, f, meta, dir_rel, root)
        if was_created:
            created += 1
        else:
            updated += 1
        review_n += len(revs)
        if (i + 1) % 500 == 0:
            conn.commit()
            if verbose:
                print(f"normalise: {i+1}/{len(leaders)} works+{created}"
                      f" reviews+{review_n} elapsed={_t.time()-t0:.0f}s",
                      flush=True)
    conn.commit()
    elapsed = _t.time() - t0
    stats = {'works_created': created, 'works_updated': updated,
             'review_items': review_n, 'elapsed_s': round(elapsed, 1)}
    if verbose:
        print("normalise done:", stats, flush=True)
    # summary rollups
    tot = conn.execute("SELECT COUNT(*) FROM works").fetchone()[0]
    au = conn.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
    su = conn.execute("SELECT COUNT(*) FROM subjects").fetchone()[0]
    rq = conn.execute("SELECT COUNT(*) FROM review_queue WHERE status='open'").fetchone()[0]
    print(f"catalogue totals: works={tot} authors={au} subjects={su}"
          f" open_reviews={rq}", flush=True)
    stats.update({'works': tot, 'authors': au, 'subjects': su,
                  'open_reviews': rq})
    return stats


def _parse_args(argv: List[str]):
    import argparse
    ap = argparse.ArgumentParser(description="Phase 3 normalisation.")
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--reprocess', action='store_true',
                    help='rebuild works that already exist (manual rows kept)')
    ap.add_argument('--report-only', action='store_true')
    return ap.parse_args(argv)


def main(argv=None):
    cfg = db.load_config()
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    with db.connect(cfg) as conn:
        db.init_db(conn)
        if args.report_only:
            cur = conn.cursor()
            print("works:", cur.execute("SELECT COUNT(*) FROM works").fetchone()[0])
            print("authors:", cur.execute("SELECT COUNT(*) FROM authors").fetchone()[0])
            print("subjects:", cur.execute("SELECT COUNT(*) FROM subjects").fetchone()[0])
            print("open_reviews:", cur.execute(
                "SELECT COUNT(*) FROM review_queue WHERE status='open'").fetchone()[0])
            print("top subjects:")
            for r in cur.execute("SELECT s.term, COUNT(*) n FROM work_subjects ws"
                                 " JOIN subjects s ON s.id=ws.subject_id"
                                 " GROUP BY s.id ORDER BY n DESC LIMIT 15"):
                print(f"  {r[1]:6d}  {r[0]}")
            return 0
        res = run_normalise(cfg, conn, args.limit, args.reprocess)
    print("done:", res)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())





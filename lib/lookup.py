"""Offline ISBN/DOI metadata enrichment.

    enrich_work(conn, work_id)   # cache-first, fills empty fields only

Sources: OpenLibrary (ISBN) and Crossref (DOI). Responses are cached in
`lookup_cache` (key 'isbn:<n>' / 'doi:<doi>', value = normalised JSON) so a
second run or --offline never touches the network.

Provenance rules (see README):
    * only *empty* `works` fields are filled — existing data is never replaced
    * source='manual' rows are never modified (human corrections win)
    * source becomes 'lookup' only when it was 'filename' or ''
    * every filled field is recorded in `edit_audit`

CLI: python lib/lookup.py [--offline] [--workers 2] [--limit N]
                          [--min-interval 0.35] [--dry-run]
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lib.db as db  # noqa: E402

OPENLIBRARY = 'https://openlibrary.org/api/volumes/brief/isbn/{isbn}.json'
CROSSREF = 'https://api.crossref.org/works/{doi}'

# OpenLibrary asks every client to identify itself with a User-Agent that
# carries a contact address; identified requests get a higher rate limit.
# Set `contact_email` in config.json (or the BIBLIOTHECA_CONTACT env var).
CONTACT = os.environ.get('BIBLIOTHECA_CONTACT', '').strip()
if not CONTACT:
    try:
        CONTACT = str(db.load_config().get('contact_email') or '').strip()
    except Exception:  # noqa: BLE001 - config is optional for the UA
        CONTACT = ''
UA = ('Bibliotheca/1.0 (offline research catalogue; contact: %s)' % CONTACT
      if CONTACT else 'Bibliotheca/1.0 (offline research catalogue)')


class RateLimiter:
    """Process-wide minimum spacing between outbound requests.

    A full pass is thousands of calls; OpenLibrary asks for <=3 req/s and may
    throttle or block unidentified clients.  A shared limiter keeps the whole
    process (every worker thread) under one request rate.
    """

    def __init__(self, interval: float = 0.0) -> None:
        self.interval = float(interval)
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            if delay > 0:
                time.sleep(delay)
                now = time.monotonic()
            self._next_allowed = now + self.interval


RATE = RateLimiter()


def http_json(url: str, timeout: float = 15.0) -> Optional[dict]:
    """GET a URL and parse JSON; None on any network/parse failure.

    A JSON body that is not an object (OpenLibrary answers a bare `[]` for an
    ISBN it does not know) is treated as "no result", so callers always get a
    dict or None.
    """
    RATE.wait()
    req = urllib.request.Request(url, headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode('utf-8', 'replace'))
        return data if isinstance(data, dict) else None
    except (urllib.error.URLError, ValueError, OSError, TimeoutError):
        return None


def normalize_isbn(raw: str) -> str:
    s = re.sub(r'[^0-9Xx]', '', raw or '')
    return s.upper() if len(s) in (10, 13) else ''


def normalize_doi(raw: str) -> str:
    s = (raw or '').strip()
    s = re.sub(r'^(https?://(dx\.)?doi\.org/|doi:\s*)', '', s, flags=re.I)
    return s.strip()


def _year_of(text) -> Optional[int]:
    """A 4-digit year from '2003', 'September 2003', '2003-05-01'."""
    if text is None:
        return None
    m = re.search(r'\b(1[5-9]\d{2}|20\d{2}|21\d{2})\b', str(text))
    return int(m.group(1)) if m else None


def _pages_int(text) -> Optional[int]:
    s = str(text or '').strip()
    return int(s) if s.isdigit() else None


def _strip_tags(s: str) -> str:
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', s or '')).strip()


# Upstream publisher strings that are reliably corrupt, mapped to a sane value.
PUBLISHER_ALIASES = {
    # leading character lost in the upstream record
    'andom house publishing group': 'Random House Publishing Group',
    'andom house': 'Random House',
    'ndom house publishing group': 'Random House Publishing Group',
}

# Tokens that legitimately end a corporate name, so a token *after* one of
# these is a forename that got appended by a MARC import, not part of the name.
_CORP_TAIL = {
    'inc', 'incorporated', 'ltd', 'limited', 'llc', 'llp', 'plc', 'co',
    'company', 'corp', 'corporation', 'group', 'press', 'books', 'book',
    'publishers', 'publishing', 'house', 'son', 'sons', 'associates',
    'booksellers', 'gmbh', 'ag', 'bv', 'nv', 'sa', 'plc', 'pty', 'srl',
    'oy', 'ab', 'as', 'edition', 'editions', 'verlag', 'publishing group',
}


def clean_publisher(raw) -> Optional[str]:
    """Normalise an upstream publisher string, or None if it is unusable.

    Handles the damage seen in OpenLibrary records: lost leading characters,
    doubled whitespace, and a forename appended after the corporate tail
    ("Wiley & Sons, Limited, John" -> "John Wiley & Sons, Limited").
    """
    s = re.sub(r'\s+', ' ', str(raw or '')).strip()
    if not s:
        return None
    low = s.lower().strip(' .,')
    if low in PUBLISHER_ALIASES:
        return PUBLISHER_ALIASES[low]
    # Nothing but digits/punctuation is not a publisher.
    if not re.search(r'[A-Za-z]{2}', s):
        return None
    # A forename stranded after the corporate tail belongs at the front.
    if ',' in s:
        head, _, tail = s.rpartition(',')
        tail, head = tail.strip(), head.strip()
        if (tail and ',' in head and re.fullmatch(r"[A-Z][A-Za-z'\-]{1,20}", tail)
                and head.rpartition(',')[2].strip().lower() in _CORP_TAIL):
            s = f'{tail} {head}'
    s = s.strip(' ,')
    if len(s) > 120:          # junk field, not a name
        return None
    return s or None


# ---------------------------------------------------------------------------
# source parsers -> normalised dict
# ---------------------------------------------------------------------------
def parse_openlibrary(data: dict, isbn: str) -> Dict[str, object]:
    """Parse OpenLibrary response (brief API format or legacy jscmd=data)."""
    if not isinstance(data, dict):
        return {}

    # Brief API structure: {'records': {'/books/...': {'data': {...}, 'details': {'details': {...}}}}}
    if 'records' in data:
        records = data.get('records')
        if not isinstance(records, dict) or not records:
            return {}
        rec = list(records.values())[0]
        rec_data = rec.get('data') or {}
        rec_details = rec.get('details', {}).get('details') or {}

        title = rec_data.get('title') or rec_details.get('title')
        subtitle = rec_data.get('subtitle') or rec_details.get('subtitle')

        authors = []
        for a in rec_data.get('authors') or rec_details.get('authors') or []:
            if isinstance(a, dict) and a.get('name'):
                authors.append(a['name'])
            elif isinstance(a, str) and a.strip():
                authors.append(a.strip())

        publisher = None
        for p in rec_data.get('publishers') or rec_details.get('publishers') or []:
            if isinstance(p, dict) and p.get('name'):
                publisher = clean_publisher(p['name'])
                if publisher:
                    break
            elif isinstance(p, str) and p.strip():
                publisher = clean_publisher(p)
                if publisher:
                    break

        place = None
        for pl in rec_data.get('publish_places') or rec_details.get('publish_places') or []:
            if isinstance(pl, dict) and pl.get('name'):
                place = pl['name']
                break
            elif isinstance(pl, str) and pl.strip():
                place = pl.strip()
                break

        year = _year_of(rec_data.get('publish_date') or rec_details.get('publish_date'))
        pages = _pages_int(rec_data.get('number_of_pages') or rec_details.get('number_of_pages'))

        langs = []
        for l in rec_details.get('languages') or rec_data.get('languages') or []:
            if isinstance(l, dict) and l.get('key'):
                langs.append(re.sub(r'^/languages/', '', l['key']))
            elif isinstance(l, str):
                langs.append(re.sub(r'^/languages/', '', l))

        raw_13 = (rec_details.get('isbn_13') or []) + (rec_data.get('identifiers', {}).get('isbn_13') or [])
        raw_10 = (rec_details.get('isbn_10') or []) + (rec_data.get('identifiers', {}).get('isbn_10') or [])
        norm_13 = [normalize_isbn(x) for x in raw_13 if len(normalize_isbn(x)) == 13]
        norm_10 = [normalize_isbn(x) for x in raw_10 if len(normalize_isbn(x)) == 10]

        pub13 = norm_13[0] if norm_13 else None
        pub10 = norm_10[0] if norm_10 else None

        return {
            'title': title,
            'subtitle': subtitle,
            'authors': authors,
            'publisher': publisher,
            'place': place,
            'year': year,
            'pages': pages,
            'language': langs[0] if langs else None,
            'isbn13': pub13 or (normalize_isbn(isbn) if len(isbn) == 13 else None),
            'isbn10': pub10 or (normalize_isbn(isbn) if len(isbn) == 10 else None),
            'source_key': 'openlibrary',
        }

    # Classic jscmd=data single-entry format
    langs = [re.sub(r'^/languages/', '', l.get('key', ''))
             for l in data.get('languages', []) if isinstance(l, dict)]
    pub13 = normalize_isbn(data['isbn_13'][0]) if data.get('isbn_13') else None
    pub10 = normalize_isbn(data['isbn_10'][0]) if data.get('isbn_10') else None
    return {
        'title': data.get('title'),
        'subtitle': data.get('subtitle'),
        'authors': [a.get('name') for a in data.get('authors', [])
                    if isinstance(a, dict) and a.get('name')],
        'publisher': clean_publisher(
            (data.get('publishers') or [{}])[0].get('name')
            if isinstance((data.get('publishers') or [{}])[0], dict)
            else (data.get('publishers') or [None])[0]),
        'place': (data.get('publish_places') or [{}])[0].get('name'),
        'year': _year_of(data.get('publish_date')),
        'pages': _pages_int(data.get('number_of_pages')),
        'language': langs[0] if langs else None,
        'isbn13': pub13 or (normalize_isbn(isbn) if len(isbn) == 13 else None),
        'isbn10': pub10 or (normalize_isbn(isbn) if len(isbn) == 10 else None),
        'source_key': 'openlibrary',
    }


def parse_crossref(msg: dict) -> Dict[str, object]:
    """`msg` is the Crossref `message` object."""
    if not isinstance(msg, dict):
        return {}
    title = (msg.get('title') or [None])[0] if isinstance(msg.get('title'), list) else msg.get('title')
    subtitle = (msg.get('subtitle') or [None])[0] if isinstance(msg.get('subtitle'), list) else msg.get('subtitle')
    year = None
    for k in ('issued', 'published-print'):
        dp = (msg.get(k) or {}).get('date-parts') or []
        if dp and dp[0]:
            year = dp[0][0]
            break
    authors = []
    for a in msg.get('author') or []:
        if isinstance(a, dict):
            name = ', '.join(x for x in (a.get('family'), a.get('given')) if x)
            if name:
                authors.append(name)
    isbn10 = isbn13 = None
    for s in msg.get('ISBN') or []:
        n = normalize_isbn(s)
        if len(n) == 13 and not isbn13:
            isbn13 = n
        elif len(n) == 10 and not isbn10:
            isbn10 = n
    raw_pub = msg.get('publisher')
    publisher = clean_publisher(
        raw_pub[0] if isinstance(raw_pub, list) and raw_pub else raw_pub)
    return {
        'title': title, 'subtitle': subtitle, 'authors': authors,
        'publisher': publisher,
        'place': None, 'year': _year_of(year),
        'pages': _pages_int(msg.get('page')),   # only pure ints survive
        'language': msg.get('language'),
        'isbn13': isbn13, 'isbn10': isbn10,
        'doi': normalize_doi(msg.get('DOI')),
        'volume': msg.get('volume'),
        'description': _strip_tags(msg.get('abstract') or '')[:2000] or None,
        'source_key': 'crossref',
    }



# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------
def cache_get(conn, key: str) -> Optional[Dict[str, object]]:
    row = conn.execute("SELECT value FROM lookup_cache WHERE key=?",
                       (key,)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except (ValueError, TypeError):
        return None


def cache_put(conn, key: str, value: dict, source: str) -> None:
    conn.execute("INSERT OR REPLACE INTO lookup_cache(key,value,source)"
                 " VALUES(?,?,?)", (key, json.dumps(value), source))
    conn.commit()


def _cached_or_fetch(conn, key: str, url: str, parse, arg: str,
                     offline: bool) -> Optional[Dict[str, object]]:
    cached = cache_get(conn, key)
    if cached:
        return cached
    if offline:
        return None
    data = http_json(url)
    if not isinstance(data, dict):
        return None
    if parse is parse_openlibrary:
        entry = parse(data if 'records' in data else data.get('ISBN:' + arg, data), arg)
    else:
        entry = parse(data.get('message', {}))
    if entry:
        cache_put(conn, key, entry, entry.get('source_key', 'net'))
        return entry
    return None


def fetch_metadata(conn, isbn: str = '', doi: str = '',
                   offline: bool = False) -> Optional[Dict[str, object]]:
    """Cache-first metadata: OpenLibrary by ISBN, then Crossref by DOI."""
    isbn = normalize_isbn(isbn)
    doi = normalize_doi(doi)
    if isbn:
        entry = _cached_or_fetch(conn, 'isbn:' + isbn,
                                 OPENLIBRARY.format(isbn=isbn),
                                 parse_openlibrary, isbn, offline)
        if entry:
            return entry
    if doi:
        return _cached_or_fetch(conn, 'doi:' + doi,
                                CROSSREF.format(doi=urllib.parse.quote(doi)),
                                parse_crossref, doi, offline)
    return None



# ---------------------------------------------------------------------------
# enrich a single work
# ---------------------------------------------------------------------------
def enrich_work(conn, work_id: int, offline: bool = False,
                dry_run: bool = False) -> Tuple[List[str], str]:
    """Fill empty fields of one work from the lookup cache / network.

    Returns (changed_fields, message). Never touches source='manual' rows.
    """
    w = conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
    if not w:
        return [], 'Work #%s not found' % work_id
    if w['source'] == 'manual':
        return [], 'Record is source=manual — edit it by hand instead'
    meta = fetch_metadata(conn, w['isbn13'] or w['isbn10'] or '',
                          w['doi'] or '', offline=offline)
    if not meta:
        hint = ' (offline, not cached)' if offline else ''
        return [], 'No metadata found for this work%s' % hint

    changes: List[Tuple[str, object]] = []
    for field in ('title', 'subtitle', 'place', 'language', 'volume',
                  'description'):
        if meta.get(field) and not w[field]:
            changes.append((field, meta[field]))
    for field in ('year', 'pages'):
        if meta.get(field) and not w[field]:
            changes.append((field, meta[field]))
    for field in ('isbn10', 'isbn13', 'doi'):
        if meta.get(field) and not w[field]:
            changes.append((field, meta[field]))
    if meta.get('publisher') and not w['publisher_id']:
        if dry_run:
            changes.append(('publisher', meta['publisher']))
        else:
            pid = db.insert_or_get(conn.cursor(), 'publishers', ['name'],
                                   {'name': meta['publisher']})
            changes.append(('publisher_id', pid))

    names = [f for f, _ in changes]
    if dry_run:
        return names, ('Would fill: ' + ', '.join(names) if names
                       else 'Would fill nothing — all known fields already set')
    if not changes:
        return [], 'Nothing to fill — all known fields already set'

    for field, value in changes:
        conn.execute("UPDATE works SET %s=? WHERE id=?" % field,
                     (value, work_id))
        conn.execute(
            "INSERT INTO edit_audit(table_name,row_id,field,old_value,"
            "new_value,user_note) VALUES('works',?,?,?,?,?)",
            (work_id, field, str(w[field]), str(value),
             'lookup:' + str(meta.get('source_key', 'net'))))
    new_source = 'lookup' if w['source'] in ('filename', '', None) \
        else w['source']
    conn.execute("UPDATE works SET source=?, updated=datetime('now')"
                 " WHERE id=?", (new_source, work_id))
    conn.commit()
    return names, 'Filled: ' + ', '.join(names)



# ---------------------------------------------------------------------------
# CLI: batch enrichment
# ---------------------------------------------------------------------------
# Fields `enrich_work` can fill. A work is a candidate when it has an
# identifier and at least one of these is empty. `doi`/`isbn13` are the lookup
# keys themselves, so they are not listed - otherwise every ISBN-only work
# would be re-selected on every run just to "fill" a DOI it does not have.
FILLABLE = ('title', 'subtitle', 'place', 'language', 'volume', 'description',
            'year', 'pages', 'publisher_id', 'isbn10')


def candidates(conn, limit: int) -> List[int]:
    """Works that have an identifier and at least one empty fillable field."""
    empty = ' OR '.join(f"COALESCE(TRIM({f}), '') = ''" for f in FILLABLE)
    return [r['id'] for r in conn.execute(
        "SELECT id FROM works WHERE COALESCE(source, '') <> 'manual'"
        " AND (isbn13 IS NOT NULL OR isbn10 IS NOT NULL OR doi IS NOT NULL)"
        f" AND ({empty}) ORDER BY id LIMIT ?", (limit,))]


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description='Fill missing work metadata from OpenLibrary/Crossref.')
    ap.add_argument('--offline', action='store_true',
                    help='use the cache only; never touch the network')
    ap.add_argument('--workers', type=int, default=2,
                    help='parallel workers (default 2; the rate limit, not '
                         'the worker count, is what bounds throughput)')
    ap.add_argument('--limit', type=int, default=500)
    ap.add_argument('--min-interval', type=float, default=0.35,
                    help='minimum seconds between outbound requests across all '
                         'workers; 0.35 ~= 3 req/s, OpenLibrary\'s limit for an '
                         'identified client (0 disables throttling)')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))

    RATE.interval = max(0.0, args.min_interval)

    cfg = db.load_config()
    with db.connect(cfg) as conn:
        ids = candidates(conn, args.limit)
    print(f'lookup: {len(ids)} candidate work(s)'
          + (f', throttled to {1 / RATE.interval:.1f} req/s'
             if RATE.interval else ', unthrottled'), flush=True)
    if not ids:
        return 0

    tls = threading.local()
    open_conns: List = []
    conns_lock = threading.Lock()
    filled = empty = errors = 0

    def work_one(wid: int) -> Tuple[int, str]:
        c = getattr(tls, 'conn', None)
        if c is None:
            c = db.get_conn(cfg)
            tls.conn = c
            with conns_lock:
                open_conns.append(c)
        fields, msg = enrich_work(c, wid, offline=args.offline,
                                  dry_run=args.dry_run)
        return len(fields), msg

    def close_thread_conns() -> None:
        with conns_lock:
            conns, open_conns[:] = list(open_conns), []
        for c in conns:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass

    with db.connect(cfg) as main_conn:
        if args.workers <= 1:
            for wid in ids:
                try:
                    n, msg = work_one(wid)
                except Exception as e:  # noqa: BLE001
                    n, msg = 0, f'ERROR {type(e).__name__}: {e}'
                print(f'  work {wid:6d}  {msg}', flush=True)
                if n:
                    filled += 1
                elif msg.startswith('ERROR'):
                    errors += 1
                else:
                    empty += 1
        else:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=args.workers) as pool:
                futs = {pool.submit(work_one, wid): wid for wid in ids}
                for fut in as_completed(futs):
                    wid = futs[fut]
                    try:
                        n, msg = fut.result()
                    except Exception as e:  # noqa: BLE001
                        n, msg = 0, f'ERROR {type(e).__name__}: {e}'
                    print(f'  work {wid:6d}  {msg}', flush=True)
                    if n:
                        filled += 1
                    elif msg.startswith('ERROR'):
                        errors += 1
                    else:
                        empty += 1
        close_thread_conns()
        total = main_conn.execute(
            "SELECT COUNT(*) FROM lookup_cache").fetchone()[0]
    verb = 'would fill' if args.dry_run else 'filled'
    print(f'lookup done: {verb} {filled}, no changes {empty}, '
          f'errors {errors}, cache entries {total}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())


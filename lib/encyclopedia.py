"""Real encyclopedia definitions from the collection's own encyclopedias.

lib/describe.py writes generated scope notes so no subject is blank. This pass
upgrades a subject to a *real* definition whenever the collection holds a
matching encyclopedia article. Today the source is the Stanford Encyclopedia of
Philosophy: 883 local HTML articles, each with a clean opening definition
between the <h1> title block and the first <h2> section.

A match is written with source='encyclopedia' and takes priority over the
generated 'auto' note; your own edits (source='manual') are never touched.

Precedence: manual > encyclopedia > auto > (empty).

Usage:
    python lib/encyclopedia.py [--dry-run] [--limit N] [--verbose]
"""
from __future__ import annotations

import html as _html
import os
import re
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import lib.db as db  # noqa: E402

MANUAL = 'manual'            # human edit — never touched
ENCYCLOPEDIA = 'encyclopedia'  # real definition pulled from a book
MAX_DEFINITION = 700         # cap the pulled definition length

# The SEP lives under this folder in the library (the real subfolder is
# misspelled 'Standford', so we discover it by pattern rather than by name).
SEP_PARENT_REL = os.path.join('Humanities', 'Philosophy',
                               'Encycopedias Companions')

# Van Nostrand's Scientific Encyclopedia (A-Z reference, 3 PDF volumes).
VN_FOLDER_HINT = 'van nostrand'


def find_sep_root(library_root: str) -> Optional[str]:
    """Locate the SEP HTML folder under the library, matching by pattern so a
    misspelling ('Standford') or rename does not break discovery."""
    parent = os.path.join(library_root, SEP_PARENT_REL)
    if not os.path.isdir(parent):
        return None
    for name in sorted(os.listdir(parent)):
        full = os.path.join(parent, name)
        if os.path.isdir(full) and 'encyclopedia of philosophy' in name.lower():
            return full
    return None


_TAG_RE = re.compile(r'<[^>]+>')


def strip_html(fragment: str) -> str:
    """Turn a small HTML fragment into clean, whitespace-collapsed text."""
    t = _TAG_RE.sub(' ', fragment)
    t = _html.unescape(t)
    t = t.replace(' ', ' ')            # non-breaking spaces
    return re.sub(r'\s+', ' ', t).strip()


def _sep_definition(txt: str) -> str:
    """Pull the opening definition from one SEP article.

    The article body lives in <div id="aueditable">; the definition is the text
    after the <h1> title / pubinfo line and before the first <h2> section.
    """
    m = re.search(r'<div\s+id="aueditable"[^>]*>(.*)', txt, re.I | re.S)
    if not m:
        return ''
    body = m.group(1)
    # drop everything up to and including the <h1> title (and any pubinfo line)
    h1 = re.search(r'<h1[^>]*>.*?</h1>', body, re.I | re.S)
    if not h1:
        return ''
    body = body[h1.end():]
    pub = re.match(r'\s*<div\s+id="pubinfo"[^>]*>.*?</div>', body, re.I | re.S)
    if pub:
        body = body[pub.end():]
    # definition = up to the first <h2>/<h3 section heading
    nxt = re.search(r'<h[23][^>]*>', body, re.I)
    if nxt:
        body = body[:nxt.start()]
    return strip_html(body)


def extract_sep(root: str) -> List[Dict[str, str]]:
    """Return [{'title','definition','file'}, ...] for every SEP article found."""
    out: List[Dict[str, str]] = []
    if not os.path.isdir(root):
        return out
    for fn in sorted(os.listdir(root)):
        if not fn.lower().endswith('.htm'):
            continue
        try:
            with open(os.path.join(root, fn), encoding='utf-8',
                      errors='replace') as fh:
                txt = fh.read()
        except OSError:
            continue
        m = re.search(r'<meta\s+name="DC\.title"\s+content="([^"]+)"', txt, re.I)
        if not m:
            continue
        title = m.group(1).strip()
        definition = _sep_definition(txt)
        if not title or len(definition) < 40:
            continue
        if len(definition) > MAX_DEFINITION:
            definition = definition[:MAX_DEFINITION].rsplit(' ', 1)[0] + '…'
        out.append({'title': title, 'definition': definition, 'file': fn[:-4]})
    return out



def _slug(s: str) -> str:
    s = (s or '').lower()
    s = re.sub(r"['']", '', s)
    return re.sub(r'[^a-z0-9]+', '-', s).strip('-')


def _variants(term: str) -> List[str]:
    """Match keys for a term: the slug itself, plus a 'First Last' form so a
    folder named 'Kant, Immanuel' matches an article titled 'Immanuel Kant'."""
    keys = [_slug(term)]
    if ',' in term:
        last, first = term.split(',', 1)
        rev = _slug(first.strip() + ' ' + last.strip())
        if rev and rev not in keys:
            keys.append(rev)
    return [k for k in keys if k]


def match_entries(entries: List[Dict[str, str]],
                  subjects: List[Tuple[int, str]]) -> Dict[int, Dict[str, str]]:
    """Map subject ids -> best encyclopedia entry.

    Exact slug matches win over 'First Last' reversal matches; ties are broken
    alphabetically so the result is deterministic. A subject is only matched on
    an exact slug, never on a partial/containment guess, so a definition is
    never attached to the wrong subject.
    """
    # subject id -> key -> (exact, [keys]) so we can prefer exact over reversal
    sub_keys: Dict[int, Dict[str, bool]] = {}
    for sid, term in subjects:
        ks = _variants(term)
        # first key is the plain slug (exact), rest are reversal forms
        for i, k in enumerate(ks):
            sub_keys.setdefault(sid, {})[k] = (i == 0)

    best: Dict[int, Dict[str, str]] = {}
    for ent in entries:
        for i, k in enumerate(_variants(ent['title'])):
            table = sub_keys
            # exact key match wins; reversal only if no exact for that subject
            for sid, keys in table.items():
                if k in keys and keys[k]:
                    cand = (0, ent['title'])
                elif k in keys:
                    cand = (1, ent['title'])
                else:
                    continue
                cur = best.get(sid)
                cur_rank = (cur['rank'], cur['title']) if cur else None
                if cur_rank is None or cand < cur_rank:
                    best[sid] = {
                        'title': ent['title'],
                        'definition': ent['definition'],
                        'file': ent.get('file', ''),
                        'rank': cand[0],
                    }
    # drop the internal rank key
    for v in best.values():
        v.pop('rank', None)
    return best



_JUNK_DEF_RE = re.compile(
    r'(Corp\.|Inc\.|University|Laborator|Ph\.D|Adapted from|Symposium|'
    r'ed\.,|Proceedings|et al\.|ISBN|Department of)', re.I)


def is_clean_definition(term: str, definition: str) -> bool:
    """Reject fragments/citations/affiliations that are not real definitions.

    Guards the PDF path: an A-Z scan can mis-detect a contributor header
    ('ROBERT M. WHITTIER, Endevco Corp., ...') or a figure caption as an
    entry. Such a 'definition' must never be written to a subject.
    """
    t = (term or '').strip()
    d = definition or ''
    if len(t) < 4 or not d:
        return False
    if re.search(r'\s[A-Z]$', t):
        return False              # fragment ending in a lone capital (ROBERT M)
    if _JUNK_DEF_RE.search(d):
        return False
    if len(re.findall(r'\b[a-z]{3,}\b', d)) < 4:
        return False              # not sentence-like prose
    return True


def apply_definitions(conn, entries, dry_run: bool = False,
                      limit: Optional[int] = None,
                      verbose: bool = False) -> Dict[str, int]:
    """Match entries to subjects and write clean definitions (source=encyclopedia).

    Shared by every encyclopedia source. Never overwrites source='manual' (your
    edits) or an existing source='encyclopedia' entry (idempotent). Junk matches
    (see is_clean_definition) are counted and skipped, not written.
    """
    subjects = [(r['id'], r['term'], r['source'] or '')
                for r in conn.execute('SELECT id, term, source FROM subjects')]
    by_id = {sid: (term, src) for sid, term, src in subjects}
    matched = match_entries(entries, [(sid, term) for sid, term, _ in subjects])
    stats = {'candidates': len(matched), 'written': 0, 'skipped_manual': 0,
             'skipped_existing': 0, 'skipped_junk': 0}
    items = sorted(matched.items())
    if limit is not None:
        items = items[:limit]
    for sid, ent in items:
        term, src = by_id[sid]
        if not is_clean_definition(ent['title'], ent['definition']):
            stats['skipped_junk'] += 1
            continue
        if src == MANUAL:
            stats['skipped_manual'] += 1
            continue
        if src == ENCYCLOPEDIA:
            stats['skipped_existing'] += 1
            continue
        body = ('%s: %s' % (ent['title'].rstrip('.'), ent['definition']))
        if not dry_run:
            conn.execute(
                "UPDATE subjects SET description=?, source=?,"
                " updated=datetime('now') WHERE id=?", (body, ENCYCLOPEDIA, sid))
        stats['written'] += 1
        if verbose and stats['written'] <= 12:
            print('  %-30s <- %s' % (term[:30], ent['title']), flush=True)
    if not dry_run:
        conn.commit()
    return stats


def find_van_nostrand(library_root: str) -> Optional[str]:
    """Locate the Van Nostrand's PDF folder by walking the tree for a folder
    whose name contains 'van nostrand'. Robust to renames/typos."""
    needle = VN_FOLDER_HINT
    skip = {'system volume information', '$recycle.bin'}
    for root, dirs, _files in os.walk(library_root):
        dirs[:] = [d for d in dirs if d.lower() not in skip]
        for d in dirs:
            if needle in d.lower():
                return os.path.join(root, d)
    return None


def _vn_pdfs(vn_dir: str) -> List[str]:
    return sorted(os.path.join(vn_dir, f) for f in os.listdir(vn_dir)
                  if f.lower().endswith('.pdf'))


def load_or_extract_vn(vn_dir: str, cache_path: Optional[str] = None,
                       rebuild: bool = False) -> List[Dict[str, str]]:
    """Return Van Nostrand's entries, using a JSON cache to avoid re-scanning
    the ~6k-page PDFs on every run. Extracts (and caches) if absent/rebuild."""
    if cache_path and not rebuild and os.path.exists(cache_path):
        import json
        with open(cache_path, encoding='utf-8') as fh:
            return json.load(fh)
    from lib import pdftoc
    entries: List[Dict[str, str]] = []
    for p in _vn_pdfs(vn_dir):
        for e in pdftoc.extract_entries(p):
            entries.append({'title': e['term'], 'definition': e['definition'],
                            'file': os.path.basename(p)})
    if cache_path:
        import json
        os.makedirs(os.path.dirname(cache_path) or '.', exist_ok=True)
        with open(cache_path, 'w', encoding='utf-8') as fh:
            json.dump(entries, fh)
    return entries


def run_van_nostrand(conn, vn_dir: str, dry_run: bool = False,
                     cache_path: Optional[str] = None, rebuild: bool = False,
                     verbose: bool = False) -> Dict[str, int]:
    """Write real definitions from Van Nostrand's Scientific Encyclopedia."""
    entries = load_or_extract_vn(vn_dir, cache_path, rebuild)
    print('  (%d Van Nostrand entries loaded)' % len(entries), flush=True)
    return apply_definitions(conn, entries, dry_run=dry_run, verbose=verbose)


def run_encyclopedia(conn, sep_root: str, dry_run: bool = False,
                     limit: Optional[int] = None,
                     verbose: bool = False) -> Dict[str, int]:
    """Write real encyclopedia definitions onto matching subjects.

    Never overwrites source='manual' (your edits) or an existing
    source='encyclopedia' entry (idempotent). Upgrades a generated 'auto' or
    empty entry to a real definition. Returns a stats dict.
    """
    entries = extract_sep(sep_root)
    subjects = [(r['id'], r['term'], r['source'] or '')
                for r in conn.execute('SELECT id, term, source FROM subjects')]
    # match only against subject id+term; keep a side map for source
    by_id = {sid: (term, src) for sid, term, src in subjects}
    matched = match_entries(entries, [(sid, term) for sid, term, _ in subjects])

    stats = {'candidates': len(matched), 'written': 0, 'skipped_manual': 0,
             'skipped_existing': 0, 'skipped_nochange': 0}
    items = sorted(matched.items())
    if limit is not None:
        items = items[:limit]
    for sid, ent in items:
        term, src = by_id[sid]
        if src == MANUAL:
            stats['skipped_manual'] += 1
            continue
        if src == ENCYCLOPEDIA:
            stats['skipped_existing'] += 1
            continue
        body = ('%s: %s' % (ent['title'].rstrip('.'), ent['definition']))
        if not dry_run:
            conn.execute(
                "UPDATE subjects SET description=?, source=?,"
                " updated=datetime('now') WHERE id=?",
                (body, ENCYCLOPEDIA, sid))
        stats['written'] += 1
        if verbose and stats['written'] <= 12:
            print('  %-30s <- %s' % (term[:30], ent['title']), flush=True)
    if not dry_run:
        conn.commit()
    return stats


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Pull real encyclopedia definitions from the collection's "
                    "encyclopedias (Stanford Encyclopedia of Philosophy and "
                    "Van Nostrand's Scientific Encyclopedia) onto matching "
                    "subjects. Offline, idempotent, never overwrites your own "
                    "edits.")
    ap.add_argument('--dry-run', action='store_true',
                    help="show what would be written without writing")
    ap.add_argument('--limit', type=int, default=None)
    ap.add_argument('--verbose', action='store_true')
    ap.add_argument('--source', default='all', choices=('all', 'sep', 'van-nostrand'),
                    help='which encyclopedia(s) to pull from')
    ap.add_argument('--sep-root', default=None,
                    help='override path to the SEP HTML folder')
    ap.add_argument('--vn-root', default=None,
                    help='override path to the Van Nostrand PDF folder')
    ap.add_argument('--rebuild-cache', action='store_true',
                    help="re-extract Van Nostrand entries (ignore JSON cache)")
    args = ap.parse_args(list(sys.argv[1:] if argv is None else argv))

    cfg = db.load_config()
    cache = os.path.join(db.BASE_DIR, 'data', 'van_nostrand_cache.json')
    with db.connect(cfg) as conn:
        db.init_db(conn)
        total = {}
        if args.source in ('all', 'sep'):
            sep_root = args.sep_root or find_sep_root(cfg['library_root'])
            if not sep_root:
                print('encyclopedia: no SEP folder found under %s'
                      % os.path.join(cfg['library_root'], SEP_PARENT_REL))
            else:
                print('encyclopedia: SEP source at %s' % sep_root, flush=True)
                total['sep'] = run_encyclopedia(
                    conn, sep_root, dry_run=args.dry_run,
                    limit=args.limit, verbose=args.verbose)
        if args.source in ('all', 'van-nostrand'):
            vn_dir = args.vn_root or find_van_nostrand(cfg['library_root'])
            if not vn_dir:
                print("encyclopedia: no 'van nostrand' folder found under %s"
                      % cfg['library_root'])
            else:
                print('encyclopedia: Van Nostrand source at %s' % vn_dir,
                      flush=True)
                total['van-nostrand'] = run_van_nostrand(
                    conn, vn_dir, dry_run=args.dry_run, cache_path=cache,
                    rebuild=args.rebuild_cache, verbose=args.verbose)
        if args.dry_run:
            print('--- dry run, nothing written ---')
        for src, stats in total.items():
            print('--- %s results ---' % src)
            for k, v in stats.items():
                print('  %-18s %d' % (k, v))
        real = conn.execute(
            "SELECT COUNT(*) FROM subjects WHERE source=?", (ENCYCLOPEDIA,)
        ).fetchone()[0]
        print('  subjects with a real definition: %d' % real)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())


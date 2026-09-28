"""Bibliotheca — local research catalogue web app (127.0.0.1 only).

Serves the catalogue in data/library.db. Read-mostly: the only writes are
manual corrections (work fields, subjects, notes, reading lists, review
triage) which are recorded as source='manual' and never overwritten by
re-scans.

Run:  python app.py            (then open http://127.0.0.1:5000)
"""
from __future__ import annotations
import json
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

import lib.db as db  # noqa: E402
import lib.crossrefs as xrefs  # noqa: E402
import lib.lookup as lookup  # noqa: E402
from lib.cite import HarvardBuilder, Work as CiteWork, Author as CiteAuthor  # noqa: E402
import lib.export as export_mod  # noqa: E402

from flask import Flask, Response, abort, flash, g, redirect, \
    render_template, request, url_for  # noqa: E402

CFG = db.load_config()
ROOT = CFG['library_root']

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, 'templates'),
    static_folder=os.path.join(BASE_DIR, 'static'))
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
_app_secret = CFG.get('flask_secret')
if not _app_secret:
    _app_secret = os.urandom(32).hex()
app.secret_key = _app_secret


# ---------------------------------------------------------------------------
# request-scoped connection
# ---------------------------------------------------------------------------
@app.before_request
def _open_conn() -> None:
    g.db = db.get_conn(CFG)


@app.teardown_request
def _close_conn(exc: Optional[BaseException]) -> None:
    c = getattr(g, 'db', None)
    if c is not None:
        try:
            c.commit()
        except Exception:
            pass
        c.close()


def q(sql: str, params: Tuple = ()) -> List[Dict]:
    return [dict(r) for r in g.db.execute(sql, params).fetchall()]


def one(sql: str, params: Tuple = ()) -> Optional[Dict]:
    r = g.db.execute(sql, params).fetchone()
    return dict(r) if r else None


def scalar(sql: str, params: Tuple = ()) -> Any:
    return g.db.execute(sql, params).fetchone()[0]


# ---------------------------------------------------------------------------
# formatting helpers
# ---------------------------------------------------------------------------
def human_size(n: Optional[int]) -> str:
    if n is None:
        return '—'
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f"{n:,.0f} {unit}" if unit == 'B' else f"{n:,.1f} {unit}"
        n /= 1024
    return f"{n:,.1f} TB"


def pct(x: float) -> str:
    return f"{100 * (x or 0):.0f}%"


def shelf_crumbs(rel_path: str) -> List[Dict]:
    parts = [p for p in rel_path.split('\\') if p]
    crumbs, acc = [], ''
    for p in parts:
        acc = p if not acc else acc + '\\' + p
        crumbs.append({'name': p, 'rel': acc})
    return crumbs


def fts_query(raw: str) -> str:
    """Make a user string safe for FTS5 MATCH: every token quoted, exact match.

    Prefix-search (``"token"*``) breaks ``snippet()`` in this SQLite build
    (OperationalError: SQL logic error), so we use exact quoted tokens only.
    """
    tokens = re.findall(r'[A-Za-z0-9À-ÿ]+', raw or '')
    if not tokens:
        return ''
    return ' '.join('"%s"' % t for t in tokens)


def fmt_date(iso: Optional[str]) -> str:
    return (iso or '')[:16].replace('T', ' ')


app.jinja_env.filters['humansize'] = human_size
app.jinja_env.filters['pct'] = pct
app.jinja_env.filters['crumbs'] = shelf_crumbs
app.jinja_env.filters['fmtdt'] = fmt_date

# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------
@app.route('/')
def dashboard() -> str:
    stats = one(
        "SELECT (SELECT COUNT(*) FROM works) AS works,"
        " (SELECT COUNT(*) FROM authors) AS authors,"
        " (SELECT COUNT(*) FROM subjects) AS subjects,"
        " (SELECT COUNT(*) FROM files WHERE state='active') AS files,"
        " (SELECT COUNT(*) FROM files WHERE is_duplicate_of IS NOT NULL) AS dupes,"
        " (SELECT COUNT(*) FROM files WHERE state='missing') AS missing,"
        " (SELECT COUNT(*) FROM review_queue WHERE status='open') AS reviews,"
        " (SELECT COUNT(*) FROM notes) AS notes,"
        " (SELECT COUNT(*) FROM reading_lists) AS lists")
    stats['bytes'] = scalar(
        "SELECT COALESCE(SUM(size_bytes),0) FROM files WHERE state='active'")
    recent = q("SELECT id, title, year, work_type, confidence, source"
               " FROM works ORDER BY id DESC LIMIT 12")
    top_subjects = q(
        "SELECT s.id, s.term, s.slug, COUNT(*) AS n FROM work_subjects ws"
        " JOIN subjects s ON s.id=ws.subject_id GROUP BY s.id"
        " ORDER BY n DESC LIMIT 15")
    by_kind = q("SELECT kind, COUNT(*) AS n FROM files WHERE state='active'"
                " GROUP BY kind ORDER BY n DESC")
    by_ext = q("SELECT ext, COUNT(*) AS n FROM files WHERE state='active'"
               " AND kind='book' GROUP BY ext ORDER BY n DESC LIMIT 12")
    no_year = scalar("SELECT COUNT(*) FROM works WHERE year IS NULL")
    no_author = scalar(
        "SELECT COUNT(*) FROM works w WHERE NOT EXISTS"
        " (SELECT 1 FROM work_authors wa WHERE wa.work_id=w.id)")
    return render_template('dashboard.html', s=stats, recent=recent,
                           top_subjects=top_subjects, by_kind=by_kind,
                           by_ext=by_ext, no_year=no_year, no_author=no_author)


# ---------------------------------------------------------------------------
# shelves (folder tree)
# ---------------------------------------------------------------------------
@app.route('/shelves')
def shelves() -> str:
    root = q("SELECT * FROM collections WHERE library_root=? AND depth=0"
             " ORDER BY name", (ROOT,))
    tops = q("SELECT * FROM collections WHERE library_root=? AND depth=1"
             " ORDER BY name", (ROOT,))
    total_files = scalar(
        "SELECT COUNT(*) FROM files WHERE library_root=? AND state='active'",
        (ROOT,))
    return render_template('shelves.html', tops=tops, root_rows=root,
                           total_files=total_files)


def _shelf_row(coll_id: int) -> Optional[Dict]:
    return one("SELECT * FROM collections WHERE id=?", (coll_id,))


@app.route('/shelves/<int:coll_id>')
def shelf(coll_id: int) -> str:
    c = _shelf_row(coll_id)
    if not c:
        abort(404)
    children = q("SELECT * FROM collections WHERE parent_id=? ORDER BY name",
                 (coll_id,))
    like = (c['rel_path'] + '\\%') if c['rel_path'] else '%'
    if c['rel_path']:
        works = q(
            "SELECT w.id, w.title, w.subtitle, w.year, w.work_type,"
            " w.confidence, f.rel_path, f.ext, f.size_bytes"
            " FROM works w JOIN work_files wf ON wf.work_id=w.id"
            " JOIN files f ON f.id=wf.file_id"
            " WHERE f.library_root=? AND f.state='active'"
            " AND (f.rel_path = ? OR f.rel_path LIKE ?)"
            " ORDER BY f.rel_path LIMIT 400",
            (ROOT, c['rel_path'], like))
    else:
        works = q(
            "SELECT w.id, w.title, w.subtitle, w.year, w.work_type,"
            " w.confidence, f.rel_path, f.ext, f.size_bytes"
            " FROM works w JOIN work_files wf ON wf.work_id=w.id"
            " JOIN files f ON f.id=wf.file_id"
            " WHERE f.library_root=? AND f.state='active'"
            " ORDER BY f.rel_path LIMIT 400", (ROOT,))
    loose = q(
        "SELECT id, filename, ext, size_bytes, rel_path FROM files"
        " WHERE library_root=? AND state='active'"
        " AND (rel_path = ? OR rel_path LIKE ?)"
        " AND id NOT IN (SELECT file_id FROM work_files)"
        " ORDER BY rel_path LIMIT 200",
        (ROOT, c['rel_path'], like))
    n_files = scalar(
        "SELECT COUNT(*) FROM files WHERE library_root=? AND state='active'"
        " AND (rel_path = ? OR rel_path LIKE ?)", (ROOT, c['rel_path'], like))
    return render_template('shelf.html', c=c, children=children,
                           works=works, loose=loose, n_files=n_files)

# ---------------------------------------------------------------------------
# work detail
# ---------------------------------------------------------------------------
def _work_row(work_id: int) -> Optional[Dict]:
    return one(
        "SELECT w.*, p.name AS publisher_name, se.name AS series_name"
        " FROM works w LEFT JOIN publishers p ON p.id=w.publisher_id"
        " LEFT JOIN series se ON se.id=w.series_id WHERE w.id=?", (work_id,))


@app.route('/work/<int:work_id>')
def work_page(work_id: int) -> str:
    w = _work_row(work_id)
    if not w:
        abort(404)
    authors = q(
        "SELECT a.id, a.display_name, a.sort_name, wa.role, wa.seq"
        " FROM work_authors wa JOIN authors a ON a.id=wa.author_id"
        " WHERE wa.work_id=? ORDER BY wa.seq, wa.role", (work_id,))
    subjects = q(
        "SELECT s.id, s.term, s.slug, s.kind, ws.weight, ws.source"
        " FROM work_subjects ws JOIN subjects s ON s.id=ws.subject_id"
        " WHERE ws.work_id=? ORDER BY ws.weight DESC, s.term", (work_id,))
    files = q(
        "SELECT f.* FROM work_files wf JOIN files f ON f.id=wf.file_id"
        " WHERE wf.work_id=? ORDER BY f.is_duplicate_of IS NULL DESC, f.rel_path",
        (work_id,))
    notes = q("SELECT * FROM notes WHERE work_id=? ORDER BY id DESC", (work_id,))
    reviews = q(
        "SELECT * FROM review_queue WHERE work_id=? AND status='open'"
        " ORDER BY id", (work_id,))
    # related works: share an author or a subject
    related_by_author = q(
        "SELECT DISTINCT w2.id, w2.title, w2.year, w2.confidence FROM"
        " work_authors wa JOIN work_authors wa2 ON wa2.author_id=wa.author_id"
        " AND wa2.work_id<>wa.work_id JOIN works w2 ON w2.id=wa2.work_id"
        " WHERE wa.work_id=? ORDER BY w2.year LIMIT 10", (work_id,))
    related_by_subject = q(
        "SELECT w2.id, w2.title, w2.year, w2.confidence, COUNT(*) AS shared"
        " FROM work_subjects ws JOIN work_subjects ws2"
        " ON ws2.subject_id=ws.subject_id AND ws2.work_id<>ws.work_id"
        " JOIN works w2 ON w2.id=ws2.work_id WHERE ws.work_id=?"
        " GROUP BY w2.id ORDER BY shared DESC, w2.year LIMIT 10", (work_id,))
    cite = build_citation(w, authors)
    return render_template(
        'work.html', w=w, authors=authors, subjects=subjects, files=files,
        notes=notes, reviews=reviews, rel_author=related_by_author,
        rel_subject=related_by_subject, cite=cite)


def build_citation(w: Dict, authors: List[Dict]) -> Dict:
    """Render the work through the Harvard formatter."""
    def mk(rows: List[Dict], role: str) -> List[CiteAuthor]:
        return [CiteAuthor.parse(r['display_name']) for r in rows
                if r.get('role') == role]
    cw = CiteWork(
        title=w['title'] or '', subtitle=w.get('subtitle'),
        authors=mk(authors, 'author'), editors=mk(authors, 'editor'),
        year=w.get('year'), edition=w.get('edition'),
        place=w.get('place'), publisher=w.get('publisher_name'),
        isbn13=w.get('isbn13'), doi=w.get('doi'),
        pages=str(w.get('pages')) if w.get('pages') else None,
        series=w.get('series_name'), work_type=w.get('work_type', 'book'))
    ref = HarvardBuilder().reference(cw)
    return {'list': ref.rendered_list, 'in_text': ref.rendered_in,
            'html': HarvardBuilder().render_html(ref),
            'issues': ref.issues}


@app.route('/work/<int:work_id>/edit', methods=['POST'])
def work_edit(work_id: int):
    w = _work_row(work_id)
    if not w:
        abort(404)
    fields = ('title', 'subtitle', 'year', 'edition', 'place', 'pages',
              'isbn10', 'isbn13', 'doi', 'language', 'work_type', 'volume')
    changed = []
    for f in fields:
        val = (request.form.get(f) or '').strip()
        if f in ('year', 'pages'):
            val = val or None
            if val is not None:
                try:
                    val = int(val)
                except ValueError:
                    continue
        elif f == 'volume':
            val = val or None
        if (w.get(f) or '') != (val or ''):
            g.db.execute(
                "INSERT INTO edit_audit(table_name,row_id,field,old_value,"
                " new_value,user_note) VALUES('works',?,?,?,?,NULL)",
                (work_id, f, str(w.get(f)), str(val)))
            g.db.execute(f"UPDATE works SET {f}=?, source='manual',"
                         " updated=datetime('now') WHERE id=?", (val, work_id))
            changed.append(f)
    if changed:
        flash(f"Updated: {', '.join(changed)} (source=manual)")
    return redirect(url_for('work_page', work_id=work_id))


@app.route('/work/<int:work_id>/enrich', methods=['POST'])
def work_enrich(work_id: int):
    """Fill empty fields from OpenLibrary/Crossref (cache-first, offline-safe)."""
    if not one("SELECT id FROM works WHERE id=?", (work_id,)):
        abort(404)
    _, msg = lookup.enrich_work(g.db, work_id)
    flash(msg)
    return redirect(url_for('work_page', work_id=work_id))


@app.route('/work/<int:work_id>/note', methods=['POST'])
def work_note(work_id: int):
    body = (request.form.get('body') or '').strip()
    if body:
        g.db.execute(
            "INSERT INTO notes(work_id, kind, page_ref, body) VALUES(?,?,?,?)",
            (work_id, request.form.get('kind') or 'note',
             (request.form.get('page_ref') or '').strip() or None, body))
        flash('Note saved')
    return redirect(url_for('work_page', work_id=work_id))


@app.route('/note/<int:note_id>/delete', methods=['POST'])
def note_delete(note_id: int):
    wid = scalar("SELECT work_id FROM notes WHERE id=?", (note_id,))
    g.db.execute("DELETE FROM notes WHERE id=?", (note_id,))
    flash('Note deleted')
    return redirect(url_for('work_page', work_id=int(wid or 0)))

# ---------------------------------------------------------------------------
# files: open / reveal a physical file or its folder (localhost only)
# ---------------------------------------------------------------------------
def _real_path(path: str) -> str:
    """Strip the Windows long-path prefix so Explorer / startfile understand it."""
    return path[4:] if path.startswith('\\\\?\\') else path


def _startfile(path: str) -> bool:
    if hasattr(os, 'startfile'):          # Windows
        os.startfile(path)
        return True
    return False


def _reveal(path: str) -> bool:
    import subprocess
    subprocess.Popen(['explorer', '/select,', os.path.normpath(path)])
    return True


def _back(default_endpoint: str = 'dashboard'):
    return redirect(request.referrer or url_for(default_endpoint))


@app.route('/file/<int:file_id>/open')
def file_open(file_id: int):
    f = one("SELECT id, path, state FROM files WHERE id=?", (file_id,))
    if not f:
        abort(404)
    p = _real_path(f['path'])
    try:
        ok = _startfile(p)
    except OSError:
        ok = False
    if not ok:
        flash('Could not open on this platform: %s' % p)
    return _back()


@app.route('/file/<int:file_id>/reveal')
def file_reveal(file_id: int):
    f = one("SELECT id, path, state FROM files WHERE id=?", (file_id,))
    if not f:
        abort(404)
    try:
        _reveal(_real_path(f['path']))
    except OSError:
        flash('Could not reveal on this platform.')
    return _back()


# ---------------------------------------------------------------------------
# authors
# ---------------------------------------------------------------------------
@app.route('/authors')
def authors() -> str:
    flt = (request.args.get('q') or '').strip()
    page = max(int(request.args.get('page', 1) or 1), 1)
    per = 60
    params: List = []
    where = " WHERE 1=1 "
    if flt:
        where += " AND a.sort_name LIKE ? "
        params.append(f"%{flt}%")
    total = scalar(f"SELECT COUNT(*) FROM authors a{where}", tuple(params))
    rows = q(
        "SELECT a.id, a.sort_name, a.display_name,"
        " (SELECT COUNT(*) FROM work_authors wa WHERE wa.author_id=a.id) AS n"
        f" FROM authors a{where} ORDER BY a.sort_name LIMIT ? OFFSET ?",
        tuple(params) + (per, (page - 1) * per))
    return render_template('authors.html', rows=rows, total=total,
                           page=page, per=per, flt=flt)


@app.route('/author/<int:author_id>')
def author(author_id: int) -> str:
    a = one("SELECT * FROM authors WHERE id=?", (author_id,))
    if not a:
        abort(404)
    works = q(
        "SELECT w.id, w.title, w.subtitle, w.year, w.work_type, wa.role,"
        " w.confidence FROM work_authors wa JOIN works w ON w.id=wa.work_id"
        " WHERE wa.author_id=? ORDER BY wa.role, w.year, w.title", (author_id,))
    aliases = q("SELECT alias FROM author_alias WHERE author_id=?",
                (author_id,))
    return render_template('author.html', a=a, works=works, aliases=aliases)


# ---------------------------------------------------------------------------
# subjects (the encyclopedia)
# ---------------------------------------------------------------------------
@app.route('/subjects')
def subjects() -> str:
    tops = q(
        "SELECT s.*, (SELECT COUNT(*) FROM subjects c WHERE c.parent_id=s.id)"
        " AS n_children,"
        " (SELECT COUNT(*) FROM work_subjects ws WHERE ws.subject_id=s.id)"
        " AS n_works FROM subjects s WHERE s.parent_id IS NULL"
        " ORDER BY s.term")
    return render_template('subjects.html', tops=tops)


@app.route('/subject/<slug>')
def subject(slug: str) -> str:
    s = one("SELECT * FROM subjects WHERE slug=?", (slug,))
    if not s:
        abort(404)
    children = q(
        "SELECT s2.*, (SELECT COUNT(*) FROM work_subjects ws"
        " WHERE ws.subject_id=s2.id) AS n_works FROM subjects s2"
        " WHERE s2.parent_id=? ORDER BY s2.term", (s['id'],))
    works = q(
        "SELECT w.id, w.title, w.subtitle, w.year, w.work_type, w.confidence,"
        " ws.weight, ws.source FROM work_subjects ws"
        " JOIN works w ON w.id=ws.work_id WHERE ws.subject_id=?"
        " ORDER BY ws.weight DESC, w.year, w.title LIMIT 200", (s['id'],))
    # co-occurrence suggests "see also" candidates
    related = q(
        "SELECT s2.id, s2.term, s2.slug, COUNT(*) AS co FROM work_subjects ws1"
        " JOIN work_subjects ws2 ON ws2.work_id=ws1.work_id"
        " AND ws2.subject_id<>ws1.subject_id JOIN subjects s2 ON s2.id=ws2.subject_id"
        " WHERE ws1.subject_id=? GROUP BY s2.id ORDER BY co DESC LIMIT 16",
        (s['id'],))
    crossrefs = xrefs.outgoing(g.db, s['id'])
    xref_in = xrefs.incoming(g.db, s['id'])
    parents: List[Dict] = []
    p = s.get('parent_id')
    while p:
        row = one("SELECT id, term, slug, parent_id FROM subjects WHERE id=?",
                  (p,))
        if not row:
            break
        parents.append(row)
        p = row['parent_id']
    parents.reverse()
    all_subjects = q("SELECT id, term, slug FROM subjects ORDER BY term")
    return render_template('subject.html', s=s, children=children,
                           works=works, related=related, crossrefs=crossrefs,
                           xref_in=xref_in, parents=parents,
                           all_subjects=all_subjects,
                           relations=xrefs.VALID_RELATIONS)


@app.route('/subject/<slug>/describe', methods=['POST'])
def subject_describe(slug: str):
    s = one("SELECT id FROM subjects WHERE slug=?", (slug,))
    if not s:
        abort(404)
    desc = (request.form.get('description') or '').strip()
    g.db.execute("UPDATE subjects SET description=?, source='manual',"
                 " updated=datetime('now') WHERE id=?", (desc or None, s['id']))
    flash('Encyclopedia entry saved')
    return redirect(url_for('subject', slug=slug))


@app.route('/subject/<slug>/crossref', methods=['POST'])
def subject_crossref(slug: str):
    s = one("SELECT id FROM subjects WHERE slug=?", (slug,))
    if not s:
        abort(404)
    target = (request.form.get('target') or '').strip()
    relation = (request.form.get('relation') or 'see_also').strip()
    note = (request.form.get('note') or '').strip() or None
    t = None
    if target:
        t = one("SELECT id FROM subjects WHERE slug=?", (target,))
        if not t:
            t = one("SELECT id FROM subjects WHERE term=? COLLATE NOCASE"
                    " LIMIT 1", (target,))
    if not t:
        flash('Unknown target subject: %s' % (target or '(empty)'))
    else:
        _, msg = xrefs.add(g.db, s['id'], t['id'], relation, note)
        flash(msg)
    return redirect(url_for('subject', slug=slug))


@app.route('/subject/<slug>/crossref/delete', methods=['POST'])
def subject_crossref_delete(slug: str):
    s = one("SELECT id FROM subjects WHERE slug=?", (slug,))
    if not s:
        abort(404)
    to_id = int(request.form.get('to_id') or 0)
    relation = (request.form.get('relation') or '').strip()
    _, msg = xrefs.remove(g.db, s['id'], to_id, relation)
    flash(msg)
    return redirect(url_for('subject', slug=slug))

# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------
@app.route('/search')
def search() -> str:
    raw = (request.args.get('q') or '').strip()
    results: List[Dict] = []
    notes: List[Dict] = []
    m = fts_query(raw)
    search_err = None
    if m:
        try:
            results = q(
                "SELECT w.id, w.title, w.year, w.confidence,"
                " p.name AS publisher,"
                " (SELECT group_concat(a.display_name, '; ')"
                "  FROM work_authors wa"
                "  JOIN authors a ON a.id=wa.author_id"
                "  WHERE wa.work_id=w.id AND wa.role IN ('author','editor')) AS authors,"
                " w.description AS description"
                " FROM doc_fts JOIN works w ON w.id=doc_fts.rowid"
                " LEFT JOIN publishers p ON p.id=w.publisher_id"
                " WHERE doc_fts MATCH ? ORDER BY bm25(doc_fts) LIMIT 60", (m,))
            notes = q(
                "SELECT n.id, n.work_id, n.kind, n.page_ref, n.body,"
                " w.title AS work_title"
                " FROM quote_fts JOIN notes n ON n.id=quote_fts.rowid"
                " LEFT JOIN works w ON w.id=n.work_id"
                " WHERE quote_fts MATCH ? ORDER BY bm25(quote_fts) LIMIT 25",
                (m,))
        except Exception:
            search_err = 'Search query could not be parsed — try simpler words'
    _toks = re.findall(r'[A-Za-z0-9À-ÿ]+', raw or '')
    for r in results:
        r['title_snip'] = _highlight(_trunc(r.get('title') or '', 90), _toks)
        r['authors_snip'] = _highlight(r.get('authors') or '', _toks)
        r['sn'] = _highlight(_trunc(r.get('description') or '', 200), _toks)
    for n in notes:
        n['work_title'] = n.get('work_title') or ''
        n['sn'] = _highlight(_trunc(n.get('body') or '', 200), _toks)
    return render_template('search.html', q=raw, results=results,
                           notes=notes, m=m, search_err=search_err)


def _trunc(text: str, max_len: int) -> str:
    """Trim to max_len at a word boundary, appending an ellipsis."""
    text = (text or '').strip()
    if len(text) <= max_len:
        return text
    cut = text[:max_len].rsplit(' ', 1)[0]
    return cut + ('…' if cut else text[:max_len])


def _highlight(text: str, tokens: List[str]) -> str:
    """Wrap each search token in <mark> (case-insensitive, whole words)."""
    if not text or not tokens:
        return text or ''
    out = text
    for tok in tokens:
        out = re.sub(r'\b' + re.escape(tok) + r'\b',
                     r'<mark>\g<0></mark>', out, flags=re.I)
    return out


# ---------------------------------------------------------------------------
# review queue
# ---------------------------------------------------------------------------
@app.route('/review')
def review() -> str:
    status = (request.args.get('status') or 'open').strip()
    page = max(int(request.args.get('page', 1) or 1), 1)
    per = 60
    total = scalar("SELECT COUNT(*) FROM review_queue WHERE status=?",
                   (status,))
    rows = q(
        "SELECT r.*, w.title AS work_title, w.confidence AS work_conf,"
        " f.rel_path FROM review_queue r"
        " LEFT JOIN works w ON w.id=r.work_id"
        " LEFT JOIN files f ON f.id=r.file_id"
        " WHERE r.status=? ORDER BY r.confidence, r.id LIMIT ? OFFSET ?",
        (status, per, (page - 1) * per))
    return render_template('review.html', rows=rows, status=status,
                           total=total, page=page, per=per)


@app.route('/review/<int:item_id>/resolve', methods=['POST'])
def review_resolve(item_id: int):
    status = request.form.get('status') or 'done'
    g.db.execute("UPDATE review_queue SET status=? WHERE id=?",
                 (status, item_id))
    return redirect(request.form.get('next') or url_for('review'))

# ---------------------------------------------------------------------------
# citations & bibliography
# ---------------------------------------------------------------------------
def _cite_work_from_row(w: Dict) -> CiteWork:
    wid = w['id']
    return CiteWork(
        title=w['title'] or '', subtitle=w.get('subtitle'),
        authors=[CiteAuthor.parse(a['display_name'])
                 for a in q("SELECT a.display_name FROM work_authors wa"
                            " JOIN authors a ON a.id=wa.author_id"
                            " WHERE wa.work_id=? AND wa.role IN"
                            " ('author','editor') ORDER BY wa.seq", (wid,))],
        editors=[CiteAuthor.parse(a['display_name'])
                 for a in q("SELECT a.display_name FROM work_authors wa"
                            " JOIN authors a ON a.id=wa.author_id"
                            " WHERE wa.work_id=? AND wa.role='editor'"
                            " ORDER BY wa.seq", (wid,))],
        year=w.get('year'), edition=w.get('edition'), place=w.get('place'),
        publisher=w.get('publisher_name'), isbn13=w.get('isbn13'),
        doi=w.get('doi'), work_type=w.get('work_type', 'book'))


def _cite_fetch(ids: List[int]) -> List[Dict]:
    if not ids:
        return []
    marks = ','.join('?' * len(ids))
    return q("SELECT w.*, p.name AS publisher_name FROM works w"
             " LEFT JOIN publishers p ON p.id=w.publisher_id"
             f" WHERE w.id IN ({marks})", tuple(ids))


@app.route('/cite')
def cite_page() -> str:
    ids = [int(x) for x in (request.args.get('ids') or '').split(',')
           if x.strip().isdigit()][:100]
    b = HarvardBuilder()
    refs = []
    for w in _cite_fetch(ids):
        ref = b.reference(_cite_work_from_row(w))
        refs.append({'work_id': w['id'], 'title': w['title'],
                     'list': ref.rendered_list, 'in_text': ref.rendered_in,
                     'issues': ref.issues})
    bib = b.bibliography([_cite_work_from_row(w) for w in _cite_fetch(ids)])
    return render_template('cite.html', refs=refs, ids=ids, bib=bib)

# ---------------------------------------------------------------------------
# bibliography export
# ---------------------------------------------------------------------------
_EXPORT_TEXT = ('bibtex', 'ris', 'csv')
_EXPORT_BIN = ('docx', 'pdf')

_EXPORT_MIME = {
    'bibtex': 'application/x-bibtex',
    'ris': 'application/x-research-info-systems',
    'csv': 'text/csv',
    'docx': 'application/vnd.openxmlformats-officedocument'
             '.wordprocessingml.document',
    'pdf': 'application/pdf',
}


def _export_source(ids: List[int], list_id: int = 0,
                   query: str = '') -> Tuple[List[Dict], List[int], str]:
    """Resolve ids / a reading list / a search query to full work rows.

    Returns (rows, ids, label). DOCX/PDF are capped at 2,000 rows; text
    formats accept up to 50,000.
    """
    if list_id:
        l = one("SELECT name FROM reading_lists WHERE id=?", (list_id,))
        if not l:
            abort(404)
        rows = q("SELECT w.*, p.name AS publisher_name FROM"
                 " reading_list_items i JOIN works w ON w.id=i.work_id"
                 " LEFT JOIN publishers p ON p.id=w.publisher_id"
                 " WHERE i.list_id=?", (list_id,))
        return rows, [r['id'] for r in rows], 'list-' + str(list_id)
    if query:
        m = fts_query(query)
        if not m:
            return [], [], 'search'
        ids = [r['id'] for r in q(
            "SELECT w.id FROM doc_fts JOIN works w ON w.id=doc_fts.rowid"
            " WHERE doc_fts MATCH ? ORDER BY bm25(doc_fts) LIMIT 2000", (m,))]
        if ids:
            rows = _cite_fetch(ids)
            return rows, ids, 'search'
        return [], [], 'search'
    rows = _cite_fetch(ids)
    return rows, [r['id'] for r in rows], 'cite'


@app.route('/export/<fmt>')
def export(fmt: str):
    fmt = (fmt or '').lower()
    if fmt not in _EXPORT_MIME:
        abort(404)
    ids = [int(x) for x in (request.args.get('ids') or '').split(',')
           if x.strip().isdigit()][:50000]
    list_id = int(request.args.get('list_id') or 0)
    query = (request.args.get('q') or '').strip()
    rows, wids, label = _export_source(ids, list_id, query)
    if fmt in _EXPORT_BIN and len(rows) > 2000:
        flash('DOCX/PDF exports are capped at 2,000 works — '
              'use BibTeX, RIS or CSV for the full %d.' % len(rows))
        return redirect(request.args.get('next') or
                        url_for('settings'))
    works = [_cite_work_from_row(w) for w in rows]
    title = 'Bibliotheca bibliography (%s)' % label
    try:
        if fmt == 'bibtex':
            payload, binary = export_mod.bibtex(works), False
        elif fmt == 'ris':
            payload, binary = export_mod.ris(works), False
        elif fmt == 'csv':
            payload, binary = export_mod.csv_text(works, wids), False
        elif fmt == 'docx':
            payload, binary = export_mod.docx_bytes(works, title), True
        else:
            payload, binary = export_mod.pdf_bytes(works, title), True
    except ImportError as e:
        flash('Export unavailable: %s' % e)
        return redirect(request.args.get('next') or url_for('settings'))
    if binary:
        assert isinstance(payload, bytes)
        return Response(payload, mimetype=_EXPORT_MIME[fmt],
                        headers={'Content-Disposition':
                                 'attachment; filename="bibliotheca-%s.%s"'
                                 % (label, fmt)})
    text = payload if payload is not None else ''
    return Response(text, mimetype=_EXPORT_MIME[fmt] + '; charset=utf-8',
                    headers={'Content-Disposition':
                             'attachment; filename="bibliotheca-%s.%s"'
                             % (label, fmt)})


# ---------------------------------------------------------------------------
# reading lists
# ---------------------------------------------------------------------------
@app.route('/lists')
def lists() -> str:
    rows = q(
        "SELECT l.*, (SELECT COUNT(*) FROM reading_list_items i"
        " WHERE i.list_id=l.id) AS n FROM reading_lists l ORDER BY l.name")
    return render_template('lists.html', rows=rows)


@app.route('/lists/new', methods=['POST'])
def list_new():
    name = (request.form.get('name') or '').strip()
    if name:
        g.db.execute("INSERT INTO reading_lists(name, description) VALUES(?,?)",
                     (name, (request.form.get('description') or '').strip()
                      or None))
        flash('Reading list created')
    return redirect(url_for('lists'))


@app.route('/lists/<int:list_id>')
def list_page(list_id: int) -> str:
    l = one("SELECT * FROM reading_lists WHERE id=?", (list_id,))
    if not l:
        abort(404)
    items = q(
        "SELECT w.id, w.title, w.subtitle, w.year, w.work_type, i.added"
        " FROM reading_list_items i JOIN works w ON w.id=i.work_id"
        " WHERE i.list_id=? ORDER BY i.added DESC", (list_id,))
    bib = HarvardBuilder().bibliography(
        [_cite_work_from_row(w) for w in
         q("SELECT w.*, p.name AS publisher_name FROM reading_list_items i"
           " JOIN works w ON w.id=i.work_id"
           " LEFT JOIN publishers p ON p.id=w.publisher_id"
           " WHERE i.list_id=?", (list_id,))])
    return render_template('list.html', l=l, items=items, bib=bib)


@app.route('/lists/<int:list_id>/add', methods=['POST'])
def list_add(list_id: int):
    wid = request.form.get('work_id')
    if wid and wid.isdigit():
        g.db.execute("INSERT OR IGNORE INTO reading_list_items(list_id,"
                     " work_id) VALUES(?,?)", (list_id, int(wid)))
    return redirect(request.form.get('next') or
                    url_for('list_page', list_id=list_id))


@app.route('/lists/<int:list_id>/remove', methods=['POST'])
def list_remove(list_id: int):
    wid = request.form.get('work_id')
    if wid and wid.isdigit():
        g.db.execute("DELETE FROM reading_list_items WHERE list_id=?"
                     " AND work_id=?", (list_id, int(wid)))
    return redirect(url_for('list_page', list_id=list_id))


# ---------------------------------------------------------------------------
# settings + run
# ---------------------------------------------------------------------------
@app.route('/settings')
def settings() -> str:
    stats = one(
        "SELECT (SELECT COUNT(*) FROM works) AS works,"
        " (SELECT COUNT(*) FROM files) AS files,"
        " (SELECT COUNT(*) FROM file_meta) AS file_meta,"
        " (SELECT COUNT(*) FROM review_queue WHERE status='open') AS reviews")
    return render_template('settings.html', cfg=CFG, s=stats, base_dir=BASE_DIR)


def main() -> None:
    host = CFG.get('host', '127.0.0.1')
    port = int(CFG.get('port', 5000))
    print(f"Bibliotheca: http://{host}:{port}   (Ctrl-C stops)")
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == '__main__':
    main()







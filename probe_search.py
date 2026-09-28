"""Test the new no-snippet search queries that the app will use."""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "library.db")
c = sqlite3.connect(DB)
M = '"occult"'

def try_(label, sql, param=(M,)):
    try:
        r = c.execute(sql, param).fetchall()
        print('OK   | %-50s | %2d rows' % (label, len(r)))
        if r:
            print('       first: id=%s title=%r pub=%r' % (
                r[0][0], r[0][1], r[0][4] if len(r[0]) > 4 else '?'))
        return r
    except Exception as e:
        print('ERROR| %-50s | %s: %s' % (label, type(e).__name__, e))
        return []

print('New doc_fts query (JOIN + bm25 + authors subquery, NO snippet):\n')
rows = try_(
    'doc_fts: join+bm25+authors-subquery+pub',
    "SELECT w.id, w.title, w.year, w.confidence,"
    " p.name AS publisher,"
    " (SELECT group_concat(a.display_name, '; ')"
    "  FROM work_authors wa"
    "  JOIN authors a ON a.id=wa.author_id"
    "  WHERE wa.work_id=w.id AND wa.role IN ('author','editor')) AS authors"
    " FROM doc_fts"
    " JOIN works w ON w.id=doc_fts.rowid"
    " LEFT JOIN publishers p ON p.id=w.publisher_id"
    " WHERE doc_fts MATCH ?"
    " ORDER BY bm25(doc_fts) LIMIT 5")

print('\nAuthors IN query for those ids:\n')
if rows:
    ids = [r[0] for r in rows]
    qmarks = ','.join('?' * len(ids))
    author_rows = try_(
        'authors IN query',
        ("SELECT wa.work_id, a.display_name FROM work_authors wa"
         " JOIN authors a ON a.id=wa.author_id"
         " WHERE wa.work_id IN (%s) AND wa.role IN ('author','editor')" % qmarks),
        ids)
    amap = {}
    for ar in author_rows:
        amap.setdefault(ar[0], []).append(ar[1])
    for r in rows:
        print('  work %s authors=%r' % (r[0], amap.get(r[0])))

print('\nNew notes query (JOIN + bm25, NO snippet):\n')
try_(
    'quote_fts: join+bm25, no snippet',
    "SELECT n.id, n.work_id, n.kind, n.page_ref, n.body,"
    " w.title AS work_title"
    " FROM quote_fts"
    " JOIN notes n ON n.id=quote_fts.rowid"
    " LEFT JOIN works w ON w.id=n.work_id"
    " WHERE quote_fts MATCH ?"
    " ORDER BY bm25(quote_fts) LIMIT 5")
print('(notes table has %d rows -- 0 matches expected)' %
      c.execute("SELECT count(*) FROM notes").fetchone()[0])
c.close()

"""Confirm the FTS5 snippet bug for a prefix-match query is systematic."""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "library.db")

c = sqlite3.connect(DB)

for term in ['occult', 'alchemy', 'magic', 'surreal']:
    plain = c.execute(
        "SELECT count(*) FROM doc_fts WHERE doc_fts MATCH ?", ('"' + term + '"',)
    ).fetchone()[0]
    star = c.execute(
        "SELECT count(*) FROM doc_fts WHERE doc_fts MATCH ?",
        ('"' + term + '"*',)
    ).fetchone()[0]
    try:
        sn = c.execute(
            "SELECT snippet(doc_fts, 0, '<mark>', '</mark>', '…', 10) AS sn"
            " FROM doc_fts WHERE doc_fts MATCH ?", ('"' + term + '"*',)
        ).fetchone()
        snv = sn['sn'] if sn else 'NONE'
        print("%-10s plain=%3d star=%3d snippet_star=%s" % (term, plain, star, snv))
    except Exception as e:
        print("%-10s plain=%3d star=%3d snippet_star=ERROR %s: %s"
              % (term, plain, star, type(e).__name__, e))

c.close()

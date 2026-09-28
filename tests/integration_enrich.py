"""Opt-in integration test for metadata enrichment against the LIVE APIs.

Not part of `python -m unittest discover` - those tests are fully offline and
must stay that way. Run this explicitly to confirm OpenLibrary and Crossref
still answer and that the end-to-end Flask route still enriches a record:

    python tests/integration_enrich.py

It builds a throwaway database in %TEMP%, never touches data/library.db, and
exits non-zero if any check fails.
"""
import os
import sys
import tempfile

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as appmod          # noqa: E402
import lib.db as db           # noqa: E402

# Real identifiers with stable, well-formed metadata at both providers.
ISBN_BOOK = '9780141439518'              # Pride and Prejudice
DOI_PAPER = '10.1112/plms/s2-42.1.230'  # On Computable Numbers

ok = True


def check(label, cond):
    global ok
    print(('PASS' if cond else 'FAIL'), label, flush=True)
    ok = ok and bool(cond)


def main() -> int:
    tmp = os.path.join(tempfile.gettempdir(), 'enrich_integration.db')
    for ext in ('', '-wal', '-shm'):
        try:
            os.remove(tmp + ext)
        except OSError:
            pass

    cfg = dict(appmod.CFG)
    cfg['db_path'] = tmp
    conn = db.get_conn(cfg)
    db.init_db(conn)

    conn.execute(
        "INSERT INTO works(title, source, confidence, isbn13, work_type)"
        " VALUES(?,?,?,?,?)",
        ('Pride and Prejudice (draft)', 'filename', 0.5, ISBN_BOOK, 'book'))
    conn.execute(
        "INSERT INTO works(title, source, confidence, doi, work_type)"
        " VALUES(?,?,?,?,?)",
        ('On Computable Numbers', 'filename', 0.5, DOI_PAPER, 'paper'))
    conn.execute(
        "INSERT INTO works(title, source, confidence, isbn13)"
        " VALUES(?,?,?,?)",
        ('Hand-corrected book', 'manual', 0.9, ISBN_BOOK))
    conn.commit()
    manual_before = tuple(
        conn.execute("SELECT * FROM works WHERE source='manual'").fetchone())

    appmod.CFG['db_path'] = tmp
    appmod.app.config['TESTING'] = True
    client = appmod.app.test_client()

    # --- ISBN enrichment through the real route ---------------------------
    r1 = client.post('/work/1/enrich', follow_redirects=True)
    b1 = r1.data.decode('utf-8', 'replace')
    check('ISBN route returns 200', r1.status_code == 200)
    check('ISBN flash reports a fill', 'Filled:' in b1)
    check('ISBN publisher on page (Penguin Books)', 'Penguin Books' in b1)
    check('ISBN year on page (2003)', '2003' in b1)

    # --- DOI enrichment through the real route ----------------------------
    r2 = client.post('/work/2/enrich', follow_redirects=True)
    b2 = r2.data.decode('utf-8', 'replace')
    check('DOI route returns 200', r2.status_code == 200)
    check('DOI flash reports a fill', 'Filled:' in b2)
    check('DOI volume on page (s2-42)', 's2-42' in b2)
    check('DOI publisher on page (Wiley)', 'Wiley' in b2)

    # --- persisted state and provenance ------------------------------------
    conn = db.get_conn(appmod.CFG)
    w1 = conn.execute("SELECT * FROM works WHERE id=1").fetchone()
    check('ISBN work year filled', w1['year'] == 2003)
    check('ISBN work pages filled', w1['pages'] == 435)
    check('ISBN work place filled', w1['place'] == 'London')
    check('ISBN work source -> lookup', w1['source'] == 'lookup')
    w2 = conn.execute("SELECT * FROM works WHERE id=2").fetchone()
    check('DOI work year filled', w2['year'] is not None)
    check('DOI work source -> lookup', w2['source'] == 'lookup')

    manual_after = tuple(
        conn.execute("SELECT * FROM works WHERE source='manual'").fetchone())
    check('source=manual row untouched', manual_after == manual_before)

    audit = conn.execute("SELECT COUNT(*) FROM edit_audit").fetchone()[0]
    check('edit_audit rows written', audit >= 8)
    cache = conn.execute("SELECT COUNT(*) FROM lookup_cache").fetchone()[0]
    check('lookup_cache populated', cache >= 2)
    pubs = conn.execute("SELECT COUNT(*) FROM publishers").fetchone()[0]
    check('publishers created', pubs >= 1)

    # the search index must still work after enrichment writes
    hit = conn.execute(
        "SELECT COUNT(*) FROM doc_fts WHERE doc_fts MATCH 'prejudice'"
    ).fetchone()[0]
    check('doc_fts still searchable after the writes', hit >= 1)
    conn.close()

    for ext in ('', '-wal', '-shm'):
        try:
            os.remove(tmp + ext)
        except OSError:
            pass

    print('INTEGRATION:', 'ALL PASS' if ok else 'FAILED')
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
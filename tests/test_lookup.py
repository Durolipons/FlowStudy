"""Tests for lib/lookup.py — parsers, cache-first flow, merge provenance.

No test here touches the network: `http_json` is replaced with a stub.
"""
import os
import sqlite3
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.db as db
from lib import lookup as L

OL_FIXTURE = {
    'title': 'The Tao of Physics',
    'subtitle': 'An Exploration of the Parallels Between Modern Physics '
                'and Eastern Mysticism',
    'authors': [{'name': 'Fritjof Capra'}],
    'publishers': [{'name': 'Bantam'}],
    'publish_places': [{'name': 'New York'}],
    'publish_date': 'September 1975',
    'number_of_pages': 336,
    'languages': [{'key': '/languages/eng'}],
    'isbn_13': ['9780553343076'],
    'isbn_10': ['0553343073'],
}

CR_FIXTURE = {
    'title': ['On Computable Numbers, with an Application to the '
              'Entscheidungsproblem'],
    'author': [{'family': 'Turing', 'given': 'Alan M.'}],
    'publisher': ['London Mathematical Society'],
    'issued': {'date-parts': [[1936]]},
    'page': '230-265',
    'volume': '42',
    'DOI': '10.1112/plms/s2-42.1.230',
    'ISBN': ['9780070000000'],
    'language': 'en',
    'abstract': '<jats:p>Received 13 May 1935.</jats:p>',
}


class LookupBase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        db.init_db(self.conn)          # real schema.sql on a fresh DB
        self._orig_http = L.http_json
        self.http_calls = []
        L.http_json = self._stub_http

    def tearDown(self):
        L.http_json = self._orig_http
        self.conn.close()

    def _stub_http(self, url, timeout=15.0):
        self.http_calls.append(url)
        if 'openlibrary' in url:
            return {'ISBN:9780553343076': OL_FIXTURE}
        if 'crossref' in url:
            return {'message': CR_FIXTURE}
        return None

    def add_work(self, **kw):
        row = dict(title='T', source='filename', confidence=0.5)
        row.update(kw)
        cols = ', '.join(row)
        ph = ', '.join('?' * len(row))
        cur = self.conn.execute(f"INSERT INTO works({cols}) VALUES({ph})",
                                list(row.values()))
        self.conn.commit()
        return cur.lastrowid


class TestHelpers(unittest.TestCase):
    def test_normalize_isbn(self):
        self.assertEqual(L.normalize_isbn('978-0-14-044913-6'), '9780140449136')
        self.assertEqual(L.normalize_isbn('0-14-044913-0'), '0140449130')
        self.assertEqual(L.normalize_isbn('055334307X'), '055334307X')
        self.assertEqual(L.normalize_isbn('123'), '')
        self.assertEqual(L.normalize_isbn(''), '')

    def test_normalize_doi(self):
        self.assertEqual(L.normalize_doi('https://doi.org/10.1/a'), '10.1/a')
        self.assertEqual(L.normalize_doi('doi:10.1/A'), '10.1/A')
        self.assertEqual(L.normalize_doi('  10.1112/plms  '),
                         '10.1112/plms')

    def test_year_and_pages(self):
        self.assertEqual(L._year_of('2003'), 2003)
        self.assertEqual(L._year_of('September 1975'), 1975)
        self.assertEqual(L._year_of('1936-05-13'), 1936)
        self.assertIsNone(L._year_of(None))
        self.assertIsNone(L._year_of('forthcoming'))
        self.assertEqual(L._pages_int('336'), 336)
        self.assertIsNone(L._pages_int('230-265'))

    def test_parse_openlibrary(self):
        m = L.parse_openlibrary(OL_FIXTURE, '9780553343076')
        self.assertEqual(m['title'], 'The Tao of Physics')
        self.assertEqual(m['authors'], ['Fritjof Capra'])
        self.assertEqual(m['publisher'], 'Bantam')
        self.assertEqual(m['place'], 'New York')
        self.assertEqual(m['year'], 1975)
        self.assertEqual(m['pages'], 336)
        self.assertEqual(m['language'], 'eng')
        self.assertEqual(m['isbn13'], '9780553343076')

    def test_parse_openlibrary_brief_api(self):
        brief_fixture = {
            'records': {
                '/books/OL37076991M': {
                    'data': {
                        'title': 'Pride and Prejudice',
                        'authors': [{'name': 'Jane Austen'}],
                        'publishers': [{'name': 'Penguin Books'}],
                        'publish_places': [{'name': 'London'}],
                        'publish_date': '2003',
                        'number_of_pages': 435,
                        'identifiers': {'isbn_10': ['0141439513'], 'isbn_13': ['9780141439518']},
                    },
                    'details': {
                        'details': {
                            'languages': [{'key': '/languages/eng'}],
                        }
                    }
                }
            }
        }
        m = L.parse_openlibrary(brief_fixture, '9780141439518')
        self.assertEqual(m['title'], 'Pride and Prejudice')
        self.assertEqual(m['authors'], ['Jane Austen'])
        self.assertEqual(m['publisher'], 'Penguin Books')
        self.assertEqual(m['place'], 'London')
        self.assertEqual(m['year'], 2003)
        self.assertEqual(m['pages'], 435)
        self.assertEqual(m['language'], 'eng')
        self.assertEqual(m['isbn13'], '9780141439518')
        self.assertEqual(m['isbn10'], '0141439513')

    def test_clean_publisher(self):
        # evidence-based repairs of corrupt upstream strings
        self.assertEqual(L.clean_publisher('andom House Publishing Group'),
                         'Random House Publishing Group')
        self.assertEqual(L.clean_publisher('Wiley & Sons, Limited, John'),
                         'John Wiley & Sons, Limited')
        self.assertEqual(L.clean_publisher('Wiley & Sons, Incorporated, John'),
                         'John Wiley & Sons, Incorporated')
        # ordinary names must pass through untouched
        for good in ('Penguin Books', 'Simon & Schuster', 'Bantam Books',
                     'Quarto Publishing Group USA'):
            self.assertEqual(L.clean_publisher(good), good)
        self.assertEqual(L.clean_publisher('  Bantam   Books '),
                         'Bantam Books')
        # a trailing place is not a forename
        self.assertEqual(L.clean_publisher('W. W. Norton & Company, New York'),
                         'W. W. Norton & Company, New York')
        # junk is rejected
        self.assertIsNone(L.clean_publisher(''))
        self.assertIsNone(L.clean_publisher(None))
        self.assertIsNone(L.clean_publisher('  12345  '))
        self.assertIsNone(L.clean_publisher('--'))
        self.assertIsNone(L.clean_publisher('x' * 200))

    def test_publisher_cleaned_in_parsers(self):
        brief = {'records': {'/books/OL1M': {'data': {
            'title': 'T',
            'publishers': [{'name': 'andom House Publishing Group'}],
        }}}}
        self.assertEqual(L.parse_openlibrary(brief, '9780553343076')['publisher'],
                         'Random House Publishing Group')
        cr = dict(CR_FIXTURE, publisher='Wiley & Sons, Limited, John')
        self.assertEqual(L.parse_crossref(cr)['publisher'],
                         'John Wiley & Sons, Limited')

    def test_rate_limiter_serialises(self):
        lim = L.RateLimiter(interval=0.0)
        lim.wait()                      # disabled -> no sleep
        self.assertEqual(lim.interval, 0.0)
        lim = L.RateLimiter(interval=0.05)
        t0 = time.monotonic()
        for _ in range(4):
            lim.wait()
        self.assertGreaterEqual(time.monotonic() - t0, 0.15)

    def test_parse_crossref(self):
        m = L.parse_crossref(CR_FIXTURE)
        self.assertEqual(m['year'], 1936)
        self.assertEqual(m['authors'], ['Turing, Alan M.'])
        self.assertEqual(m['doi'], '10.1112/plms/s2-42.1.230')
        self.assertEqual(m['volume'], '42')
        self.assertIsNone(m['pages'])            # range is not an int
        self.assertIn('Received 13 May 1935', m['description'])
        self.assertNotIn('<jats:p>', m['description'])

    def test_parse_garbage(self):
        self.assertEqual(L.parse_openlibrary(None, 'x'), {})
        self.assertEqual(L.parse_crossref([]), {})


class TestCacheFirst(LookupBase):
    def test_cache_roundtrip(self):
        key = 'isbn:9780553343076'
        self.assertIsNone(L.cache_get(self.conn, key))
        L.cache_put(self.conn, key, {'title': 'T'}, 'openlibrary')
        self.assertEqual(L.cache_get(self.conn, key)['title'], 'T')

    def test_fetch_uses_cache_without_network(self):
        L.cache_put(self.conn, 'isbn:9780553343076',
                    {'title': 'Cached', 'source_key': 'openlibrary'},
                    'openlibrary')
        meta = L.fetch_metadata(self.conn, '978-0-553-34307-6', '')
        self.assertEqual(meta['title'], 'Cached')
        self.assertEqual(self.http_calls, [])    # network never consulted

    def test_fetch_offline_without_cache_returns_none(self):
        self.assertIsNone(L.fetch_metadata(self.conn, '9780553343076', '',
                                           offline=True))
        self.assertEqual(self.http_calls, [])    # offline never fetches

    def test_unknown_isbn_list_payload_is_not_an_error(self):
        # OpenLibrary answers a bare `[]` for an ISBN it does not know; that
        # used to reach `.get()` on a list and raise AttributeError.
        def list_stub(url, timeout=15.0):
            self.http_calls.append(url)
            return []
        L.http_json = list_stub
        try:
            self.assertIsNone(L.fetch_metadata(self.conn, '9780553343076', ''))
        finally:
            L.http_json = self._stub_http
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM lookup_cache").fetchone()[0], 0)

    def test_empty_records_payload_is_not_an_error(self):
        def empty_stub(url, timeout=15.0):
            self.http_calls.append(url)
            return {'records': {}, 'items': []}
        L.http_json = empty_stub
        try:
            self.assertIsNone(L.fetch_metadata(self.conn, '9780553343076', ''))
        finally:
            L.http_json = self._stub_http


class TestCandidates(LookupBase):
    def test_skips_fully_populated_work(self):
        wid = self.add_work(isbn13='9780553343076')
        L.enrich_work(self.conn, wid)
        wid2 = self.add_work(isbn13='9780553343076', title='X')
        L.enrich_work(self.conn, wid2)
        # saturate every field the fixture cannot supply on its own
        for col in ('volume', 'description'):
            self.conn.execute(f"UPDATE works SET {col}='x' WHERE id IN (?,?)",
                              (wid, wid2))
        self.conn.execute("UPDATE works SET isbn10='0553343073' WHERE id IN (?,?)",
                          (wid, wid2))
        self.conn.commit()
        self.assertNotIn(wid, L.candidates(self.conn, 100))
        self.assertNotIn(wid2, L.candidates(self.conn, 100))

    def test_includes_work_missing_only_language(self):
        # regression: the old filter only looked at year/publisher/place/pages
        wid = self.add_work(isbn13='9780553343076')
        L.enrich_work(self.conn, wid)
        self.conn.execute("UPDATE works SET isbn10='0553343073' WHERE id=?",
                          (wid,))
        self.conn.commit()
        self.assertIn(wid, L.candidates(self.conn, 100))

    def test_excludes_work_without_identifier(self):
        wid = self.add_work(title='No identifier')
        self.assertNotIn(wid, L.candidates(self.conn, 100))

    def test_excludes_manual_work(self):
        wid = self.add_work(isbn13='9780553343076', source='manual')
        self.assertNotIn(wid, L.candidates(self.conn, 100))

    def test_respects_limit(self):
        for _ in range(3):
            self.add_work(isbn13='9780553343076')
        self.assertEqual(len(L.candidates(self.conn, 2)), 2)



class TestEnrich(LookupBase):
    def _get(self, wid):
        return self.conn.execute("SELECT * FROM works WHERE id=?",
                                 (wid,)).fetchone()

    def test_fills_only_empty_fields(self):
        wid = self.add_work(title='My Own Title', isbn13='9780553343076')
        fields, msg = L.enrich_work(self.conn, wid)
        w = self._get(wid)
        self.assertEqual(w['title'], 'My Own Title')   # existing wins
        self.assertEqual(w['year'], 1975)
        self.assertEqual(w['pages'], 336)
        self.assertEqual(w['place'], 'New York')
        self.assertEqual(w['language'], 'eng')
        self.assertIn('year', fields)
        self.assertNotIn('title', fields)
        self.assertIn('Filled', msg)

    def test_publisher_created_and_reused(self):
        wid1 = self.add_work(isbn13='9780553343076')
        L.enrich_work(self.conn, wid1)
        wid2 = self.add_work(isbn13='9780553343076')
        L.enrich_work(self.conn, wid2)
        n = self.conn.execute("SELECT COUNT(*) FROM publishers"
                              " WHERE name='Bantam'").fetchone()[0]
        self.assertEqual(n, 1)
        self.assertEqual(self._get(wid1)['publisher_id'],
                         self._get(wid2)['publisher_id'])

    def test_manual_rows_untouched(self):
        wid = self.add_work(isbn13='9780553343076', source='manual')
        fields, msg = L.enrich_work(self.conn, wid)
        self.assertEqual(fields, [])
        self.assertIn('manual', msg)
        self.assertIsNone(self._get(wid)['year'])
        self.assertEqual(len(self.http_calls), 0)   # not even fetched

    def test_source_filename_becomes_lookup(self):
        wid = self.add_work(isbn13='9780553343076', source='filename')
        L.enrich_work(self.conn, wid)
        self.assertEqual(self._get(wid)['source'], 'lookup')

    def test_source_embedded_kept(self):
        wid = self.add_work(isbn13='9780553343076', source='embedded')
        L.enrich_work(self.conn, wid)
        self.assertEqual(self._get(wid)['source'], 'embedded')

    def test_edit_audit_records_every_fill(self):
        wid = self.add_work(isbn13='9780553343076')
        L.enrich_work(self.conn, wid)
        rows = self.conn.execute(
            "SELECT field, old_value, new_value, user_note FROM edit_audit"
            " WHERE row_id=?", (wid,)).fetchall()
        by_field = {r['field']: r for r in rows}
        self.assertIn('year', by_field)
        self.assertEqual(by_field['year']['new_value'], '1975')
        self.assertEqual(by_field['year']['old_value'], 'None')
        self.assertEqual(by_field['year']['user_note'],
                         'lookup:openlibrary')

    def test_dry_run_changes_nothing(self):
        wid = self.add_work(isbn13='9780553343076')
        fields, msg = L.enrich_work(self.conn, wid, dry_run=True)
        self.assertTrue(fields)
        self.assertIn('Would fill', msg)
        self.assertIsNone(self._get(wid)['year'])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM edit_audit").fetchone()[0], 0)

    def test_second_run_reports_nothing_to_fill(self):
        wid = self.add_work(isbn13='9780553343076')
        L.enrich_work(self.conn, wid)
        fields, msg = L.enrich_work(self.conn, wid)
        self.assertEqual(fields, [])
        self.assertIn('Nothing to fill', msg)

    def test_offline_second_work_uses_cache(self):
        wid1 = self.add_work(isbn13='9780553343076')
        L.enrich_work(self.conn, wid1)                # populates the cache
        calls = len(self.http_calls)
        wid2 = self.add_work(isbn13='9780553343076', title='Other')
        fields, msg = L.enrich_work(self.conn, wid2, offline=True)
        self.assertTrue(fields, msg)
        self.assertEqual(len(self.http_calls), calls)  # no new network

    def test_offline_no_cache_message(self):
        wid = self.add_work(isbn13='9780553343076')
        fields, msg = L.enrich_work(self.conn, wid, offline=True)
        self.assertEqual(fields, [])
        self.assertIn('offline, not cached', msg)

    def test_no_identifier_no_fetch(self):
        wid = self.add_work(title='No ID')
        fields, msg = L.enrich_work(self.conn, wid)
        self.assertEqual(fields, [])
        self.assertEqual(self.http_calls, [])

    def test_missing_work(self):
        fields, msg = L.enrich_work(self.conn, 999999)
        self.assertIn('not found', msg)


if __name__ == '__main__':
    unittest.main()


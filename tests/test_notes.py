"""Tests for lib/notes.py — source migration, HTML cleaning, summary seeding.

Offline: builds a tiny in-memory DB; never touches the live library.
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import notes as N

DDL = """
CREATE TABLE works (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT);
CREATE TABLE notes (
    id INTEGER PRIMARY KEY,
    work_id INTEGER REFERENCES works(id) ON DELETE CASCADE,
    subject_id INTEGER,
    kind TEXT NOT NULL DEFAULT 'note',
    page_ref TEXT,
    body TEXT NOT NULL,
    created TEXT, updated TEXT);
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)

    def tearDown(self):
        self.conn.close()

    def add_work(self, title, desc=None):
        cur = self.conn.execute('INSERT INTO works(title, description) VALUES(?,?)',
                                (title, desc))
        return cur.lastrowid

    def add_note(self, work_id, kind, body, source=None):
        if source is None:
            cur = self.conn.execute(
                'INSERT INTO notes(work_id, kind, body) VALUES(?,?,?)',
                (work_id, kind, body))
        else:
            N.ensure_source_column(self.conn)
            cur = self.conn.execute(
                'INSERT INTO notes(work_id, kind, body, source) VALUES(?,?,?,?)',
                (work_id, kind, body, source))
        return cur.lastrowid


class TestEnsureSourceColumn(Base):
    def test_adds_column_when_missing(self):
        self.assertTrue(N.ensure_source_column(self.conn))
        cols = {r['name'] for r in self.conn.execute('PRAGMA table_info(notes)')}
        self.assertIn('source', cols)

    def test_idempotent(self):
        N.ensure_source_column(self.conn)
        self.assertFalse(N.ensure_source_column(self.conn))


class TestCleanHtml(unittest.TestCase):
    def test_strips_tags(self):
        self.assertEqual(N.clean_html('<p>Hello <b>world</b></p>'), 'Hello world')

    def test_decodes_entities(self):
        self.assertEqual(N.clean_html('a &#8212; b'), 'a — b')

    def test_collapses_whitespace(self):
        self.assertEqual(N.clean_html('a  \n  b'), 'a b')


class TestSeedSummaries(Base):
    def test_writes_summary_with_extracted_source(self):
        w = self.add_work('Book A', '<p>A great book about ' + 'x' * 60 + '.</p>')
        stats = N.seed_summaries(self.conn)
        self.assertEqual(stats['written'], 1)
        row = self.conn.execute('SELECT kind, source, body FROM notes WHERE work_id=?',
                                (w,)).fetchone()
        self.assertEqual(row['kind'], 'summary')
        self.assertEqual(row['source'], 'extracted')
        self.assertNotIn('<', row['body'])

    def test_skips_work_with_existing_summary(self):
        w = self.add_work('Book B', 'A blurb that is definitely long enough to keep. ' * 2)
        self.add_note(w, 'summary', 'my own summary', source='manual')
        stats = N.seed_summaries(self.conn)
        self.assertEqual(stats['written'], 0)

    def test_idempotent(self):
        self.add_work('Book C', 'Another blurb long enough to be stored as a summary here. ' * 2)
        N.seed_summaries(self.conn)
        n1 = self.conn.execute('SELECT COUNT(*) FROM notes').fetchone()[0]
        N.seed_summaries(self.conn)
        n2 = self.conn.execute('SELECT COUNT(*) FROM notes').fetchone()[0]
        self.assertEqual(n1, n2)

    def test_skips_short_blurb(self):
        self.add_work('Book D', 'tiny')
        stats = N.seed_summaries(self.conn)
        self.assertEqual(stats['written'], 0)

    def test_truncates_long_blurb(self):
        self.add_work('Book E', 'word ' * 400)
        N.seed_summaries(self.conn)
        body = self.conn.execute('SELECT body FROM notes').fetchone()['body']
        self.assertLessEqual(len(body), N.MAX_SUMMARY + 2)
        self.assertTrue(body.endswith('…'))

    def test_dry_run_writes_nothing(self):
        self.add_work('Book F', 'A blurb long enough to be a summary. ' * 2)
        N.seed_summaries(self.conn, dry_run=True)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM notes').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()


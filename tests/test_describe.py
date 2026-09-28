"""Tests for lib/describe.py — note composition and the backfill pass.

All offline: they build a tiny in-memory SQLite with the subset of the real
schema that describe.py touches, and never open the live library.db.
"""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import describe as d

DDL = """
CREATE TABLE subjects (
    id INTEGER PRIMARY KEY, term TEXT NOT NULL, slug TEXT NOT NULL,
    parent_id INTEGER, description TEXT, source TEXT NOT NULL DEFAULT 'taxonomy',
    created TEXT, updated TEXT);
CREATE TABLE works (
    id INTEGER PRIMARY KEY, title TEXT NOT NULL, year INTEGER);
CREATE TABLE authors (
    id INTEGER PRIMARY KEY, display_name TEXT NOT NULL);
CREATE TABLE work_authors (
    work_id INTEGER NOT NULL, author_id INTEGER NOT NULL, role TEXT DEFAULT 'author');
CREATE TABLE work_subjects (
    work_id INTEGER NOT NULL, subject_id INTEGER NOT NULL, weight REAL DEFAULT 1.0,
    PRIMARY KEY (work_id, subject_id));
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)

    def tearDown(self):
        self.conn.close()

    def add_subject(self, term, source='taxonomy', description=None, parent_id=None):
        slug = term.lower().replace(' ', '-')
        cur = self.conn.execute(
            "INSERT INTO subjects(term, slug, parent_id, description, source)"
            " VALUES(?,?,?,?,?)", (term, slug, parent_id, description, source))
        return cur.lastrowid

    def add_work(self, title, year=None, subject_id=None, weight=1.0):
        cur = self.conn.execute("INSERT INTO works(title, year) VALUES(?,?)",
                                (title, year))
        wid = cur.lastrowid
        if subject_id is not None:
            self.conn.execute(
                "INSERT INTO work_subjects(work_id, subject_id, weight)"
                " VALUES(?,?,?)", (wid, subject_id, weight))
        return wid

    def add_author(self, name):
        cur = self.conn.execute("INSERT INTO authors(display_name) VALUES(?)", (name,))
        return cur.lastrowid


class TestBuildNote(unittest.TestCase):
    def test_zero_works(self):
        note = d.build_note(0, None, None, 0, [], [])
        self.assertIn('no catalogued works', note)

    def test_singular_work_and_subtopic(self):
        note = d.build_note(1, 1998, 1998, 1, [], [])
        self.assertIn('1 catalogued work,', note)
        self.assertIn('1 sub-topic,', note)
        self.assertIn('dated 1998', note)

    def test_plural_and_year_span(self):
        note = d.build_note(7, 1990, 2020, 3, [], [])
        self.assertIn('7 catalogued works', note)
        self.assertIn('3 sub-topics', note)
        self.assertIn('spanning 1990–2020', note)

    def test_authors_and_titles_present(self):
        note = d.build_note(5, 1990, 2020, 0,
                            [('A Smith', 3), ('B Jones', 2)],
                            ['First Book', 'Second Book'])
        self.assertIn('Leading authors: A Smith (3), B Jones (2).', note)
        self.assertIn('Representative titles: First Book; Second Book.', note)

    def test_long_title_truncated(self):
        long = 'X' * 200
        note = d.build_note(1, None, None, 0, [], [long])
        self.assertIn('…', note)
        self.assertLess(len(note), 300)

    def test_missing_years_omitted(self):
        note = d.build_note(3, None, None, 0, [], ['T'])
        self.assertNotIn('spanning', note)
        self.assertNotIn('dated', note)


class TestRunDescribe(Base):
    def test_writes_auto_source_and_description(self):
        s = self.add_subject('Alchemy')
        self.add_work('The Alchemist', 1925, subject_id=s)
        stats = d.run_describe(self.conn)
        self.assertEqual(stats['written'], 1)
        row = self.conn.execute(
            "SELECT source, description FROM subjects WHERE id=?",
            (s,)).fetchone()
        self.assertEqual(row['source'], d.AUTO)
        self.assertIn('1 catalogued work', row['description'])

    def test_manual_is_never_overwritten(self):
        s = self.add_subject('Tarot', source='manual',
                             description='Hand-written entry.')
        self.add_work('Book', 2000, subject_id=s)
        stats = d.run_describe(self.conn)
        self.assertEqual(stats['skipped_manual'], 1)
        self.assertEqual(stats['written'], 0)
        row = self.conn.execute(
            "SELECT source, description FROM subjects WHERE id=?",
            (s,)).fetchone()
        self.assertEqual(row['source'], 'manual')
        self.assertEqual(row['description'], 'Hand-written entry.')

    def test_idempotent_regenerates_auto_identically(self):
        s = self.add_subject('Biology')
        self.add_work('Cell', 1990, subject_id=s)
        d.run_describe(self.conn)
        first = self.conn.execute(
            "SELECT description FROM subjects WHERE id=?", (s,)).fetchone()['description']
        d.run_describe(self.conn)  # second pass
        second = self.conn.execute(
            "SELECT description FROM subjects WHERE id=?", (s,)).fetchone()['description']
        self.assertEqual(first, second)

    def test_dry_run_writes_nothing(self):
        s = self.add_subject('Chemistry')
        self.add_work('Acid', 1950, subject_id=s)
        stats = d.run_describe(self.conn, dry_run=True)
        self.assertEqual(stats['written'], 1)
        row = self.conn.execute(
            "SELECT description, source FROM subjects WHERE id=?",
            (s,)).fetchone()
        self.assertIsNone(row['description'])
        self.assertEqual(row['source'], 'taxonomy')

    def test_leading_authors_and_titles_deterministic(self):
        s = self.add_subject('Physics')
        a1 = self.add_author('Ada Lovelace')
        a2 = self.add_author('Grace Hopper')
        w1 = self.add_work('Analytical Engine', 1843, subject_id=s, weight=5.0)
        w2 = self.add_work('Babbage Engine', 1844, subject_id=s, weight=1.0)
        self.conn.execute("INSERT INTO work_authors(work_id, author_id) VALUES(?,?)",
                          (w1, a1))
        self.conn.execute("INSERT INTO work_authors(work_id, author_id) VALUES(?,?)",
                          (w2, a1))
        self.conn.execute("INSERT INTO work_authors(work_id, author_id) VALUES(?,?)",
                          (w2, a2))
        titles = d.top_titles_by_subject(self.conn)
        self.assertEqual(titles[s][0], 'Analytical Engine')
        authors = d.top_authors_by_subject(self.conn)
        self.assertEqual(authors[s][0], ('Ada Lovelace', 2))

    def test_children_counted(self):
        parent = self.add_subject('Science')
        self.add_subject('Physics', parent_id=parent)
        self.add_subject('Chemistry', parent_id=parent)
        self.add_work('Book', 2000, subject_id=parent)
        d.run_describe(self.conn)
        note = self.conn.execute("SELECT description FROM subjects WHERE id=?",
                                 (parent,)).fetchone()['description']
        self.assertIn('2 sub-topics', note)

    def test_no_works_counted(self):
        s = self.add_subject('Empty')
        stats = d.run_describe(self.conn)
        self.assertEqual(stats['no_works'], 1)
        note = self.conn.execute("SELECT description FROM subjects WHERE id=?",
                                 (s,)).fetchone()['description']
        self.assertIn('no catalogued works', note)


if __name__ == '__main__':
    unittest.main()

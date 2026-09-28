"""Tests for lib/encyclopedia.py — extraction, matching, precedence, idempotency.

All offline. The SEP folder is faked with a tiny temp dir containing one
sample article; the live library is never touched.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import encyclopedia as e

ARTICLE = """<html><head>
<meta name="DC.title" content="Immanuel Kant" />
</head><body>
<div id="aueditable">
<h1>Immanuel Kant</h1><div id="pubinfo"><em>First published Tue 1 Jan, 1996</em></div>
<p>Immanuel Kant was a German philosopher whose influence on modern
philosophy is immense and far-reaching.</p>
<p>He wrote the <i>Critique</i> of Pure Reason and the Critique of Practical
Reason.</p>
<h2><a name="1">1. Life and Character</a></h2>
<p>This section is not part of the definition and must be excluded.</p>
</div></body></html>
"""

DDL = """
CREATE TABLE subjects (
    id INTEGER PRIMARY KEY, term TEXT NOT NULL, slug TEXT NOT NULL,
    parent_id INTEGER, description TEXT, source TEXT NOT NULL DEFAULT 'taxonomy',
    created TEXT, updated TEXT);
"""


class Base(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)
        self.tmp = tempfile.mkdtemp()
        with open(os.path.join(self.tmp, 'kant.htm'), 'w', encoding='utf-8') as fh:
            fh.write(ARTICLE)

    def tearDown(self):
        self.conn.close()
        try:
            os.remove(os.path.join(self.tmp, 'kant.htm'))
            os.rmdir(self.tmp)
        except OSError:
            pass

    def add_subject(self, term, source='taxonomy', description=None):
        slug = e._slug(term)
        cur = self.conn.execute(
            "INSERT INTO subjects(term, slug, description, source) VALUES(?,?,?,?)",
            (term, slug, description, source))
        return cur.lastrowid


class TestStripHtml(unittest.TestCase):
    def test_strips_tags_and_collapses_space(self):
        self.assertEqual(e.strip_html('<p>a  <b>b</b></p>'), 'a b')

    def test_decodes_entities(self):
        self.assertEqual(e.strip_html('S&#248;ren &amp; &quot;x&quot;'),
                         'Søren & "x"')

    def test_nbsp_becomes_space(self):
        self.assertEqual(e.strip_html('a&nbsp;&nbsp;b'), 'a b')


class TestExtraction(Base):
    def test_extracts_title_and_definition(self):
        entries = e.extract_sep(self.tmp)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['title'], 'Immanuel Kant')
        d = entries[0]['definition']
        self.assertIn('German philosopher', d)
        self.assertIn('Critique of Pure Reason', d)

    def test_definition_stops_before_first_h2(self):
        d = e.extract_sep(self.tmp)[0]['definition']
        self.assertNotIn('not part of the definition', d)
        self.assertNotIn('First published', d)  # pubinfo stripped

    def test_missing_dir_returns_empty(self):
        self.assertEqual(e.extract_sep(os.path.join(self.tmp, 'nope')), [])


class TestSlugAndVariants(unittest.TestCase):
    def test_slug_normalises(self):
        # apostrophes are stripped (same slugify convention as normalise.py)
        self.assertEqual(e._slug("Bell's Theorem"), 'bells-theorem')

    def test_variants_include_reversal(self):
        v = e._variants('Kant, Immanuel')
        self.assertIn('kant-immanuel', v)
        self.assertIn('immanuel-kant', v)

    def test_plain_term_single_variant(self):
        self.assertEqual(e._variants('Plato'), ['plato'])


class TestMatchEntries(unittest.TestCase):
    def _entries(self):
        return [{'title': 'Immanuel Kant', 'definition': 'def', 'file': 'kant'}]

    def test_exact_and_reversal_both_match(self):
        m = e.match_entries(self._entries(), [(1, 'Immanuel Kant')])
        self.assertIn(1, m)
        m = e.match_entries(self._entries(), [(2, 'Kant, Immanuel')])
        self.assertIn(2, m)

    def test_no_partial_match(self):
        m = e.match_entries(self._entries(), [(3, 'Kantian')])
        self.assertEqual(m, {})



class TestRunEncyclopedia(Base):
    def test_writes_real_definition_and_source(self):
        s = self.add_subject('Kant, Immanuel', source='auto',
                             description='old generated note')
        stats = e.run_encyclopedia(self.conn, self.tmp)
        self.assertEqual(stats['written'], 1)
        row = self.conn.execute("SELECT source, description FROM subjects WHERE id=?",
                                (s,)).fetchone()
        self.assertEqual(row['source'], e.ENCYCLOPEDIA)
        self.assertIn('Immanuel Kant:', row['description'])
        self.assertIn('German philosopher', row['description'])

    def test_manual_never_overwritten(self):
        s = self.add_subject('Immanuel Kant', source='manual',
                             description='My own entry.')
        stats = e.run_encyclopedia(self.conn, self.tmp)
        self.assertEqual(stats['skipped_manual'], 1)
        row = self.conn.execute("SELECT source, description FROM subjects WHERE id=?",
                                (s,)).fetchone()
        self.assertEqual(row['source'], 'manual')
        self.assertEqual(row['description'], 'My own entry.')

    def test_idempotent_skips_existing_encyclopedia(self):
        self.add_subject('Immanuel Kant', source='taxonomy')
        e.run_encyclopedia(self.conn, self.tmp)
        stats = e.run_encyclopedia(self.conn, self.tmp)
        self.assertEqual(stats['written'], 0)
        self.assertEqual(stats['skipped_existing'], 1)

    def test_dry_run_writes_nothing(self):
        s = self.add_subject('Immanuel Kant', source='taxonomy')
        stats = e.run_encyclopedia(self.conn, self.tmp, dry_run=True)
        self.assertEqual(stats['written'], 1)
        row = self.conn.execute("SELECT source, description FROM subjects WHERE id=?",
                                (s,)).fetchone()
        self.assertEqual(row['source'], 'taxonomy')
        self.assertIsNone(row['description'])

    def test_unmatched_subject_untouched(self):
        s = self.add_subject('Ontology', source='taxonomy')
        stats = e.run_encyclopedia(self.conn, self.tmp)
        self.assertEqual(stats['written'], 0)
        row = self.conn.execute("SELECT source FROM subjects WHERE id=?",
                                (s,)).fetchone()
        self.assertEqual(row['source'], 'taxonomy')


class TestIsCleanDefinition(unittest.TestCase):
    def test_accepts_real_prose(self):
        self.assertTrue(e.is_clean_definition(
            'ANATOMY',
            'A branch of biology dealing with the structure of organisms '
            'and their parts.'))

    def test_rejects_short_term(self):
        self.assertFalse(e.is_clean_definition('AG', 'some body of text here'))

    def test_rejects_author_fragment_term(self):
        self.assertFalse(e.is_clean_definition(
            'ROBERT M', 'WHITTIER, Endevco Corp., San Juan Capistrano, CA'))

    def test_rejects_affiliation_definition(self):
        self.assertFalse(e.is_clean_definition(
            'ANATOMY',
            'WHITTIER, Endevco Corporation, San Juan Capistrano, California'))

    def test_rejects_citation(self):
        self.assertFalse(e.is_clean_definition(
            'ANATOMY',
            'Adapted from R.Q.W. Stone, Jr. in Graybiel, ed., Symposium on X'))

    def test_rejects_non_prose(self):
        self.assertFalse(e.is_clean_definition('ANATOMY', 'Fig. 16 galaxies'))



if __name__ == '__main__':
    unittest.main()


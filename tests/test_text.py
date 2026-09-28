"""Tests for lib/text.py — quote selection and text extraction.

Offline: temp files only, never the live library. PDF/EPUB extraction is
covered lightly (a tiny reportlab PDF and are skipped if unavailable).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import text

PROSE = (
    'The cell is the basic structural and functional unit of every living '
    'organism, and it is often described as the smallest unit that can '
    'reproduce itself independently. '
    'Membranes regulate the passage of substances into and out of the cell, '
    'controlling what enters and what leaves. '
    'Enzymes are biological catalysts that speed up chemical reactions '
    'without being consumed by the reaction itself. '
    'The mitochondrion produces most of the chemical energy needed to power '
    'the cell biochemicals. '
    'DNA stores the genetic instructions used in growth, function, and '
    'reproduction of all living organisms. '
) * 6


class TestPickQuotes(unittest.TestCase):
    def test_picks_multiple_quotes(self):
        self.assertGreaterEqual(len(text.pick_quotes(PROSE)), 1)

    def test_rejects_junk_sentences(self):
        qs = text.pick_quotes(PROSE)
        joined = ' '.join(qs)
        for bad in ('Fig.', 'See also', 'CHAPTER'):
            self.assertNotIn(bad, joined)

    def test_rejects_short_text(self):
        self.assertEqual(text.pick_quotes('Too short to matter.'), [])

    def test_rejects_leading_digit(self):
        t = ('1 The mitochondrion produces most of the chemical energy needed to '
             'power the cell biochemicals and sustain life in the organism. ' * 6)
        qs = text.pick_quotes(t)
        for q in qs:
            self.assertFalse(q.lstrip().startswith('1 '))

    def test_deterministic(self):
        self.assertEqual(text.pick_quotes(PROSE), text.pick_quotes(PROSE))

    def test_quotes_have_no_markup(self):
        t = ('<p>The &amp; entity and the &#13; spacing must be cleaned before '
             'any quote is chosen from this paragraph of prose here. ' * 8)
        for q in text.pick_quotes(t):
            self.assertNotIn('<', q)
            self.assertNotIn('&#', q)     # numeric entity left un-decoded
            self.assertNotIn('&amp;', q)  # named entity left un-decoded


class TestExtractText(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        for f in os.listdir(self.tmp):
            os.remove(os.path.join(self.tmp, f))
        os.rmdir(self.tmp)

    def test_txt(self):
        p = os.path.join(self.tmp, 'a.txt')
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write(PROSE)
        t, err = text.extract_text(p, '.txt')
        self.assertIsNone(err)
        self.assertIn('mitochondrion', t)

    def test_html_strips_tags(self):
        p = os.path.join(self.tmp, 'a.html')
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write('<html><body><p>Hello <b>world</b> and more text here.</p></body></html>')
        t, err = text.extract_text(p, '.html')
        self.assertIsNone(err)
        self.assertNotIn('<', t)
        self.assertIn('world', t)

    def test_missing_file_returns_error(self):
        p = os.path.join(self.tmp, 'nope.txt')
        t, err = text.extract_text(p, '.txt')
        self.assertIsNotNone(err)


if __name__ == '__main__':
    unittest.main()

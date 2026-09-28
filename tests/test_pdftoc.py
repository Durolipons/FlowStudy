"""Tests for lib/pdftoc.py — text cleaning, heading detection, PDF extraction.

Offline. A tiny synthetic PDF is generated with reportlab in setUp and parsed
with the real pypdf path, so extract_entries is exercised end-to-end. The live
library is never touched.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import pdftoc


def _make_pdf(path, lines):
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(path)
    y = 780
    for ln in lines:
        c.setFont('Helvetica', 11)
        c.drawString(40, y, ln)
        y -= 16
    c.save()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.pdf = os.path.join(self.tmp, 'ref.pdf')
        _make_pdf(self.pdf, [
            '/CR',
            'ANTIKNOCK AGENTS. Any of a number of organic compounds that '
            'increase the octane number of a gasoline when added in low '
            'percentages by reducing knock in high compression engines.',
            'See also Octane Number; and Petroleum.',
            'ANTIMATTER. Matter that consists of antiparticles; for every '
            'elementary type of matter there is a corresponding conjugate '
            'entity which is an antiparticle with reversed properties.',
            'TABLE 1. A table caption that is not an entry heading at all.',
        ])

    def tearDown(self):
        try:
            os.remove(self.pdf)
            os.rmdir(self.tmp)
        except OSError:
            pass


class TestCleanText(unittest.TestCase):
    def test_ligatures_fixed(self):
        self.assertEqual(pdftoc.clean_text('ﬁnal ﬂow'), 'final flow')

    def test_whitespace_collapsed_keep_lines(self):
        out = pdftoc.clean_text('a  b\n\tc')
        self.assertEqual(out.splitlines()[0], 'a b')
        self.assertEqual(out.splitlines()[1], 'c')


class TestIsHeading(unittest.TestCase):
    def test_accepts_caps_term(self):
        self.assertTrue(pdftoc._is_heading('ANTIMATTER'))
        self.assertTrue(pdftoc._is_heading('ANTIKNOCK AGENTS'))

    def test_rejects_sentence(self):
        self.assertFalse(pdftoc._is_heading('Matter that consists'))

    def test_rejects_caption_prefix(self):
        self.assertFalse(pdftoc._is_heading('TABLE 1'))

    def test_rejects_too_short(self):
        self.assertFalse(pdftoc._is_heading('A'))


class TestExtractEntries(Base):
    def test_extracts_real_entries(self):
        ents = pdftoc.extract_entries(self.pdf)
        terms = [e['term'] for e in ents]
        self.assertIn('ANTIKNOCK AGENTS', terms)
        self.assertIn('ANTIMATTER', terms)

    def test_definition_captured(self):
        ents = {e['term']: e['definition'] for e in pdftoc.extract_entries(self.pdf)}
        self.assertIn('octane number', ents['ANTIKNOCK AGENTS'])

    def test_see_also_terminates_entry(self):
        ents = {e['term']: e['definition'] for e in pdftoc.extract_entries(self.pdf)}
        self.assertNotIn('See also', ents['ANTIKNOCK AGENTS'])
        self.assertNotIn('Octane Number', ents['ANTIKNOCK AGENTS'])

    def test_caption_not_an_entry(self):
        terms = [e['term'] for e in pdftoc.extract_entries(self.pdf)]
        self.assertFalse(any(t.startswith('TABLE') for t in terms))

    def test_antimatter_defined(self):
        ents = {e['term']: e['definition'] for e in pdftoc.extract_entries(self.pdf)}
        self.assertIn('antiparticles', ents['ANTIMATTER'])


if __name__ == '__main__':
    unittest.main()


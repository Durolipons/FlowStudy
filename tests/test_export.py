"""Tests for lib/export.py (BibTeX, RIS, CSV, DOCX, PDF).

Everything is pure-function output, so the library itself is never touched.
"""
import io
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from docx import Document

from lib.cite import Author, Work
from lib import export as ex


def _w(**kw):
    base = dict(title='Untitled', work_type='book')
    base.update(kw)
    return Work(**base)

BOOK = _w(
    title='The Book', subtitle='On the Taboo Against Knowing Who You Are',
    authors=[Author.parse('Watts, Alan W.')], year=1966, place='New York',
    publisher='Vintage Books', isbn13='9780394705859')
BOOK2 = _w(  # same surname+year as BOOK: BibTeX key must be deduplicated
    title='The Book Club', authors=[Author.parse('Watts, Alan W.')],
    year=1966, publisher='Penguin')
CHAPTER = _w(
    title='The Brain in a Vat',
    authors=[Author.parse('Putnam, Hilary')], year=1981,
    collection_title='Reason, Truth and History',
    editors=[Author.parse('Putnam, Hilary')], place='Cambridge',
    publisher='Cambridge University Press', pages='1-21', work_type='chapter')
JOURNAL = _w(
    title='On Computable Numbers',
    authors=[Author.parse('Turing, Alan M.')], year=1936,
    series='Proceedings of the London Mathematical Society', volume='42',
    pages='230-265', doi='10.1112/plms/s2-42.1.230', work_type='journal')
MAGAZINE = _w(
    title='The End of Physics', authors=[Author.parse('Lindley, David')],
    year=1993, series='New Scientist', pages='30-33', work_type='magazine')
WEB = _w(
    title='SEP: Zombies', authors=[], year=None,
    url='https://plato.stanford.edu/entries/zombies/',
    website_name='Stanford Encyclopedia of Philosophy',
    accessed='2026-09-20', work_type='web')
THESIS = _w(
    title='A Theory of Grounding', authors=[Author.parse('Fine, Kit')],
    year=1974, thesis_inst='University of Warwick', work_type='thesis')
REPORT = _w(
    title='Small Is Beautiful: A Study of Economics',
    authors=[Author.parse('Schumacher, E. F.')], year=1973,
    publisher='Blond & Briggs', place='London', report_no='E-77',
    work_type='report')

ALL = [BOOK, BOOK2, CHAPTER, JOURNAL, MAGAZINE, WEB, THESIS, REPORT]


class TestOrdering(unittest.TestCase):
    def test_sorted_like_bibliography(self):
        ordered = ex.sorted_works([BOOK, JOURNAL])
        self.assertEqual(ordered[0].title, 'On Computable Numbers')
        self.assertEqual(ordered[1].title, 'The Book')

    def test_bib_keys_are_unique(self):
        out = ex.bibtex([BOOK, BOOK2])
        keys = re.findall(r'@\w+\{([^,]+),', out)
        self.assertEqual(len(keys), 2)
        self.assertEqual(len(set(keys)), 2)


class TestBibtex(unittest.TestCase):
    def test_entry_types_and_fields(self):
        out = ex.bibtex(ALL)
        self.assertIn('@book{', out)
        self.assertIn('@incollection{', out)
        self.assertIn('@article{', out)
        self.assertIn('@phdthesis{', out)
        self.assertIn('@techreport{', out)
        self.assertIn('@misc{', out)
        self.assertIn('author = {Watts, Alan W.}', out)
        self.assertIn('title = {The Book: On the Taboo Against Knowing Who You Are}', out)
        self.assertIn('year = {1966}', out)
        self.assertIn('publisher = {Vintage Books}', out)
        self.assertIn('isbn = {9780394705859}', out)
        self.assertIn('booktitle = {Reason, Truth and History}', out)
        self.assertIn('journal = {Proceedings of the London Mathematical Society}', out)
        self.assertIn('doi = {10.1112/plms/s2-42.1.230}', out)
        self.assertIn('school = {University of Warwick}', out)
        self.assertIn('number = {E-77}', out)
        self.assertIn('howpublished = {https://plato.stanford.edu/entries/zombies/}', out)

    def test_pages_use_double_dash(self):
        out = ex.bibtex([CHAPTER])
        self.assertIn('pages = {1--21}', out)

    def test_anon_and_latex_escapes(self):
        w = _w(title='100% Sure & Certain_things', authors=[], year=2000)
        out = ex.bibtex([w])
        self.assertIn('author = {Anon}', out)
        self.assertIn(r'100\% Sure \& Certain\_things', out)

    def test_empty_input(self):
        self.assertEqual(ex.bibtex([]), '')


class TestRis(unittest.TestCase):
    def test_records_and_types(self):
        out = ex.ris(ALL)
        self.assertEqual(out.count('TY  - '), len(ALL))
        # every record carries an ER terminator (also the final line)
        self.assertEqual(out.count('ER  -'), len(ALL))
        self.assertTrue(out.strip().endswith('ER  -'))
        self.assertIn('TY  - BOOK', out)
        self.assertIn('TY  - CHAP', out)
        self.assertIn('TY  - JOUR', out)
        self.assertIn('TY  - ELEC', out)
        self.assertIn('TY  - THES', out)
        self.assertIn('TY  - RPRT', out)
        self.assertIn('AU  - Watts, Alan W.', out)
        self.assertIn('A2  - Putnam, Hilary', out)
        self.assertIn('TI  - The Book: On the Taboo Against Knowing Who You Are', out)
        self.assertIn('PY  - 1966', out)
        self.assertIn('PB  - Vintage Books', out)
        self.assertIn('CY  - New York', out)
        self.assertIn('SP  - 1', out)
        self.assertIn('EP  - 21', out)
        self.assertIn('SN  - 9780394705859', out)
        self.assertIn('DO  - 10.1112/plms/s2-42.1.230', out)
        self.assertIn('UR  - https://plato.stanford.edu/entries/zombies/', out)

    def test_empty_input(self):
        self.assertEqual(ex.ris([]), '')


class TestCsv(unittest.TestCase):
    def test_header_and_rows(self):
        out = ex.csv_text(ALL, ids=list(range(101, 101 + len(ALL))))
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith('work_id,'))
        self.assertIn('harvard', lines[0])
        self.assertIn('in_text', lines[0])
        self.assertEqual(len(lines), len(ALL) + 1)
        self.assertIn('Watts, Alan W.', out)
        self.assertIn('9780394705859', out)


class TestDocxPdf(unittest.TestCase):
    def test_docx_is_a_document_with_content(self):
        data = ex.docx_bytes(ALL)
        self.assertTrue(data[:2] == b'PK')
        self.assertGreater(len(data), 2000)
        doc = Document(io.BytesIO(data))
        text = '\n'.join(p.text for p in doc.paragraphs)
        self.assertIn('Bibliography', text)
        self.assertIn('The Book', text)
        self.assertIn('In-text citations', text)

    def test_pdf_starts_with_header(self):
        data = ex.pdf_bytes(ALL)
        self.assertTrue(data[:4] == b'%PDF')
        self.assertGreater(len(data), 2000)

    def test_empty_set_still_builds(self):
        self.assertGreater(len(ex.docx_bytes([])), 1000)
        self.assertGreater(len(ex.pdf_bytes([])), 1000)


if __name__ == '__main__':
    unittest.main()

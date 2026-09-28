"""Tests for normalisation helpers: names, sort titles, shelf subjects, merge."""
from __future__ import annotations
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.normalise import (  # noqa: E402
    author_sort_name, sort_title, shelf_subjects, slugify, merge_fields,
)


class TestAuthorSortName(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(author_sort_name('Max Stirner'),
                         ('Stirner, Max', 'Max', 'Stirner'))

    def test_initials_kept(self):
        self.assertEqual(author_sort_name('Alan W. Watts'),
                         ('Watts, Alan W.', 'Alan W.', 'Watts'))

    def test_particle_stays_with_family(self):
        self.assertEqual(author_sort_name('Frans De Waal'),
                         ('De Waal, Frans', 'Frans', 'De Waal'))

    def test_already_surname_first(self):
        self.assertEqual(author_sort_name('Watts, Alan W.'),
                         ('Watts, Alan W.', 'Alan W.', 'Watts'))

    def test_single_token(self):
        self.assertEqual(author_sort_name('Homer'), ('Homer', None, 'Homer'))


class TestSortTitle(unittest.TestCase):
    def test_leading_article_moved(self):
        self.assertEqual(sort_title('The Ego and His Own'),
                         'Ego and His Own, The')

    def test_no_article_unchanged(self):
        self.assertEqual(sort_title('Singularity Hypotheses'),
                         'Singularity Hypotheses')

    def test_none_safe(self):
        self.assertIsNone(sort_title(None))


class TestShelfSubjects(unittest.TestCase):
    def test_taxonomy_chain(self):
        self.assertEqual(
            shelf_subjects(r'Humanities\Esoteric Occult\Alchemy Collection'),
            ['Humanities', 'Esoteric Occult', 'Alchemy'])

    def test_publisher_folder_dropped(self):
        self.assertEqual(shelf_subjects(r'Unsorted\New Books\CUP'), ['Unsorted'])
        self.assertEqual(shelf_subjects(r'New Books\Routledge'), [])

    def test_release_pack_folder_dropped(self):
        self.assertEqual(
            shelf_subjects('Fiction/The New York Times Best Sellers - [ePub]'),
            ['Fiction'])
        self.assertEqual(shelf_subjects(r'Books\[ECLiPSE]'), [])

    def test_bare_year_dropped(self):
        self.assertEqual(shelf_subjects(r'Science\2014'), ['Science'])


class TestSlugify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify('Esoteric Occult'), 'esoteric-occult')

    def test_punctuation(self):
        self.assertEqual(slugify("Agrippa's Three Books"),
                         'agrippas-three-books')

    def test_empty(self):
        self.assertEqual(slugify(''), 'untitled')


META_BASE = {'title': None, 'creator': None, 'publisher': None, 'isbn': None,
             'pages': None, 'language': None, 'description': None,
             'pub_date': None}

USER = os.environ.get('USERNAME', 'Durolipons')


class TestMergeFields(unittest.TestCase):
    def test_filename_only(self):
        m = merge_fields('Frans De Waal - Evolved Morality [2014][A].epub',
                         '.epub', None, '')
        self.assertEqual(m['title'], 'Evolved Morality')
        self.assertEqual(m['authors'], ['Frans De Waal'])
        self.assertEqual(m['year'], 2014)
        self.assertIn('filename:title', m['source_parts'])

    def test_embedded_beats_filename(self):
        meta = dict(META_BASE, title='Evolved Morality: The Biology of Morals',
                    creator='Waal, Frans de', pub_date='2014-03-01')
        m = merge_fields('Some_Messy_File_Name.pdf', '.pdf', meta, '')
        self.assertEqual(m['title'], 'Evolved Morality: The Biology of Morals')
        self.assertIn('embedded:title', m['source_parts'])

    def test_os_username_creator_rejected(self):
        """PDFs exported by the library owner say /Author = <windows user>."""
        meta = dict(META_BASE, title='The Wealth of Nations', creator=USER)
        m = merge_fields('The Wealth of Nations - Adam Smith.pdf', '.pdf',
                         meta, '')
        self.assertNotIn(USER, m['authors'])
        self.assertEqual(m['authors'], ['Adam Smith'])

    def test_software_creator_rejected(self):
        for bad in ('Usuario', 'Administrator', 'Adobe Acrobat', 'unknown'):
            meta = dict(META_BASE, title='A Real Title', creator=bad)
            m = merge_fields('Real Title - Jane Doe.pdf', '.pdf', meta, '')
            self.assertEqual(m['authors'], ['Jane Doe'], bad)

    def test_filename_equal_title_rejected(self):
        meta = dict(META_BASE, title='0321839455.pdf', creator=None)
        m = merge_fields('Campbell Biology - 10th Edition.pdf', '.pdf', meta, '')
        self.assertEqual(m['title'], 'Campbell Biology')
        self.assertNotIn('embedded:title', m['source_parts'])

    def test_embedded_isbn_used(self):
        meta = dict(META_BASE, title='Behold the Spirit',
                    isbn='978-0-307-78433-9')
        m = merge_fields('Watts - Behold the Spirit.epub', '.epub', meta, '')
        self.assertEqual(m['isbn13'], '9780307784339')

    def test_raw_fallback_never_empty(self):
        m = merge_fields('x.pdf', '.pdf', None, '')
        self.assertTrue(m['title'])


if __name__ == '__main__':
    unittest.main()

"""Tests for the filename grammar engine, using real filenames from the library."""
from __future__ import annotations
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.parse_names import parse_filename  # noqa: E402


class TestISBNPrefixGrammar(unittest.TestCase):
    def test_cambridge_isbn10_valid(self):
        f = ("0521870445.Cambridge.University.Press."
             "An.Introduction.to.Continuum.Mechanics.Oct.2007.pdf")
        p = parse_filename(f)
        self.assertEqual(p.isbn, "0521870445")
        self.assertEqual(p.isbn10, "0521870445")
        self.assertEqual(p.publisher, "Cambridge University Press")
        self.assertEqual(p.year, 2007)
        self.assertEqual(p.title, "An Introduction to Continuum Mechanics")
        self.assertFalse(p.needs_review)
        self.assertGreaterEqual(p.confidence, 0.9)

    def test_invalid_isbn_not_captured(self):
        p = parse_filename("9999999998.Some.Title.Here.pdf")
        self.assertIsNone(p.isbn)
        self.assertIsNotNone(p.title)


class TestDashGrammar(unittest.TestCase):
    def test_author_minus_title(self):
        p = parse_filename("Frans De Waal - Evolved Morality [2014][A].epub")
        self.assertEqual(p.authors, ["Frans De Waal"])
        self.assertEqual(p.year, 2014)
        self.assertEqual(p.title, "Evolved Morality")
        self.assertTrue(p.needs_review)

    def test_title_minus_author_long_title(self):
        p = parse_filename(
            "And I Suppose We Didn't Go to the Moon Either - Jim Fetzer.epub")
        self.assertEqual(p.authors, ["Jim Fetzer"])
        self.assertIn("And I Suppose", p.title)
        self.assertIsNone(p.year)
        self.assertTrue(p.needs_review)


class TestMagazinePattern(unittest.TestCase):
    def test_new_scientist_issue(self):
        p = parse_filename("New Scientist - 22.12.2012-04.01.2013.pdf")
        self.assertEqual(p.kind, "magazine")
        self.assertEqual(p.publisher, "New Scientist")
        self.assertEqual(p.year, 2012)
        self.assertTrue(p.title)


class TestPublisherTokenFallback(unittest.TestCase):
    def test_blackwell_companion(self):
        p = parse_filename("The Blackwell Companion to Eastern Christianity.pdf")
        self.assertIn("Blackwell", p.publisher or "")
        self.assertIn("Eastern Christianity", p.title)

    def test_cambridge_companion(self):
        p = parse_filename("The Cambridge Companion to Spinoza.epub")
        self.assertEqual(p.title, "The Cambridge Companion to Spinoza")
        self.assertTrue(p.publisher)


class TestPlainTitles(unittest.TestCase):
    def test_plain_title_no_publisher(self):
        p = parse_filename("Complete Idiot's Guide to MBA Basics.pdf")
        self.assertIsNone(p.publisher)
        self.assertIn("MBA Basics", p.title)
        self.assertTrue(p.needs_review)

    def test_series_number_stripped(self):
        p = parse_filename("Some Book (3).pdf")
        self.assertEqual(p.series_number, "3")
        self.assertIn("Some Book", p.title)

    def test_edition_detected(self):
        p = parse_filename("Some Book 3rd ed.pdf")
        self.assertEqual(p.edition, "3 ed")


class TestKindByExtension(unittest.TestCase):
    def test_extensions(self):
        self.assertEqual(parse_filename("x.epub").kind, "book")
        self.assertEqual(parse_filename("x.pdf").kind, "book")
        self.assertEqual(parse_filename("x.mp3").kind, "audiovisual")
        self.assertEqual(parse_filename("x.rar").kind, "archive")
        self.assertEqual(parse_filename("x.htm").kind, "web")


if __name__ == "__main__":
    unittest.main()

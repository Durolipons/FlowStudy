"""Tests for the Harvard citation formatter (Cite Them Right style)."""
from __future__ import annotations
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib.cite import Author, Work, HarvardBuilder  # noqa: E402


class TestBook(unittest.TestCase):
    def test_single_author_book(self):
        w = Work(title="The information: a history, a theory, a flood",
                 authors=[Author.parse("James Gleick")],
                 year=2011, place="London", publisher="Fourth Estate")
        ref = HarvardBuilder().reference(w)
        s = ref.rendered_list
        self.assertIn("Gleick, J. (2011)", s)
        self.assertIn("The information: a history, a theory, a flood", s)
        self.assertIn("London: Fourth Estate", s)
        self.assertEqual(ref.rendered_in, "(Gleick, 2011)")
        self.assertNotIn("Anon", s)


class TestEditedBook(unittest.TestCase):
    def test_edited_book(self):
        w = Work(title="The Cambridge Companion to Spinoza",
                 authors=[Author.parse("Don Garrett")],
                 editors=[Author.parse("Don Garrett")],
                 year=2005, place="Cambridge",
                 publisher="Cambridge University Press")
        ref = HarvardBuilder().reference(w)
        self.assertIn("Garrett, D.", ref.rendered_list)
        self.assertIn("Cambridge: Cambridge University Press", ref.rendered_list)


class TestChapter(unittest.TestCase):
    def test_chapter(self):
        w = Work(title="Dust and the formation of chondrules",
                 authors=[Author.parse("Peter Bell")],
                 editors=[Author.parse("Peter Bell")],
                 collection_title="The Origin of the Moon",
                 year=2009, place="London", publisher="IGCP", pages="15-36")
        ref = HarvardBuilder().reference(w, 'chapter')
        s = ref.rendered_list
        self.assertIn("Bell, P. (2009) 'Dust and the formation of chondrules'", s)
        self.assertIn("in Bell, P. (ed.) *The Origin", s)
        self.assertIn("London: IGCP", s)
        self.assertIn("pp. 15-36", s)
        self.assertIn("Bell", ref.rendered_in)
        self.assertIn("2009", ref.rendered_in)


class TestJournal(unittest.TestCase):
    def test_journal(self):
        w = Work(title="Climate change and the penguin",
                 authors=[Author.parse("Jane Smith")], year=2020,
                 volume="42", number="3", pages="120-135",
                 series="Journal of Polar Biology")
        ref = HarvardBuilder().reference(w, 'journal')
        s = ref.rendered_list
        self.assertIn("Smith, J. (2020) 'Climate change and the penguin'", s)
        self.assertIn("*Journal of Polar Biology*", s)
        self.assertIn("42(3)", s)
        self.assertIn("120-135", s)
        self.assertEqual(ref.rendered_in, "(Smith, 2020, p. 120-135)")


class TestWeb(unittest.TestCase):
    def test_web_accessed(self):
        w = Work(title="DeepMind", authors=[Author.parse("Sarah Jones")],
                 year=2023, url="https://www.deepmind.com/article",
                 website_name="DeepMind", accessed="2023-01-15",
                 work_type='web')
        ref = HarvardBuilder().reference(w)
        s = ref.rendered_list
        self.assertIn("Available at: https://www.deepmind.com/article", s)
        self.assertIn("(Accessed: 15 January 2023)", s)
        self.assertEqual(ref.rendered_in, "(Jones, 2023)")


class TestInTextRules(unittest.TestCase):
    def test_three_authors_et_al(self):
        w = Work(title="X", authors=[Author.parse("A One"),
                                      Author.parse("B Two"),
                                      Author.parse("C Three")], year=2020)
        ref = HarvardBuilder().reference(w)
        self.assertEqual(ref.rendered_in, "(One et al., 2020)")

    def test_two_authors(self):
        w = Work(title="X", authors=[Author.parse("A One"),
                                     Author.parse("B Two")], year=2020)
        ref = HarvardBuilder().reference(w)
        self.assertEqual(ref.rendered_in, "(One and Two, 2020)")

    def test_anonymous(self):
        w = Work(title="X", authors=[], year=2020)
        ref = HarvardBuilder().reference(w)
        self.assertEqual(ref.rendered_in, "(Anon, 2020)")


class TestBibliography(unittest.TestCase):
    def test_sort_and_dedup(self):
        a = Work(title="Zebra", authors=[Author.parse("Zoe Appleby")],
                 year=2019, place="London", publisher="P")
        b = Work(title="Apple", authors=[Author.parse("Alan Brown")],
                 year=2021, place="London", publisher="P")
        c = Work(title="Apple", authors=[Author.parse("Alan Brown")],
                 year=2021, place="London", publisher="P")  # true duplicate of b
        out = HarvardBuilder().bibliography([a, b, c])
        lines = out.strip().split("\n")
        self.assertEqual(len(lines), 2)  # dedup
        self.assertIn("Appleby", lines[0])  # sorted by surname


class TestValidation(unittest.TestCase):
    def test_missing_fields_flagged(self):
        w = Work(title="No Publisher Book", authors=[Author.parse("A B")],
                 year=2000)
        ref = HarvardBuilder().reference(w)
        self.assertTrue(any("publisher" in i for i in ref.issues))


if __name__ == "__main__":
    unittest.main()

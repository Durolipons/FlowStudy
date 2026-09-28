"""Tests for lib/crossrefs.py — validation, add/remove idempotency, queries."""
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib import crossrefs as x

DDL = """
CREATE TABLE subjects (
    id INTEGER PRIMARY KEY, term TEXT NOT NULL, slug TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'subject', parent_id INTEGER,
    description TEXT, source TEXT NOT NULL DEFAULT 'taxonomy',
    created TEXT, updated TEXT);
CREATE TABLE crossrefs (
    from_id INTEGER NOT NULL REFERENCES subjects(id),
    to_id INTEGER NOT NULL REFERENCES subjects(id),
    relation TEXT NOT NULL, note TEXT,
    PRIMARY KEY (from_id, to_id, relation));
"""


class CrossrefBase(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(DDL)
        for i, (term, slug) in enumerate(
                [('Alchemy', 'alchemy'), ('Esoteric Occult', 'esoteric-occult'),
                 ('Symbolism', 'symbolism'), ('Tarot', 'tarot')], start=1):
            self.conn.execute(
                "INSERT INTO subjects(id, term, slug) VALUES(?,?,?)",
                (i, term, slug))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()


class TestValidate(CrossrefBase):
    def test_valid_relation(self):
        self.assertEqual(x.validate(1, 2, 'see_also'), '')

    def test_invalid_relation_lists_choices(self):
        err = x.validate(1, 2, 'friends')
        self.assertIn('friends', err)
        self.assertIn('see_also', err)

    def test_self_link_rejected(self):
        self.assertIn('itself', x.validate(2, 2, 'related'))

    def test_non_int_rejected(self):
        self.assertIn('required', x.validate(None, 2, 'related'))


class TestAddRemove(CrossrefBase):
    def test_add_and_query_outgoing(self):
        changed, msg = x.add(self.conn, 1, 2, 'broader', 'umbrella term')
        self.assertTrue(changed)
        self.assertIn('saved', msg)
        rows = x.outgoing(self.conn, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['term'], 'Esoteric Occult')
        self.assertEqual(rows[0]['relation'], 'broader')

    def test_add_is_idempotent(self):
        x.add(self.conn, 1, 2, 'see_also')
        changed, msg = x.add(self.conn, 1, 2, 'see_also')
        self.assertFalse(changed)
        self.assertIn('already exists', msg)
        self.assertEqual(len(x.outgoing(self.conn, 1)), 1)

    def test_same_pair_different_relations_allowed(self):
        x.add(self.conn, 1, 2, 'see_also')
        x.add(self.conn, 1, 2, 'related')
        self.assertEqual(len(x.outgoing(self.conn, 1)), 2)

    def test_direction_is_independent(self):
        x.add(self.conn, 1, 2, 'see_also')
        self.assertEqual(x.outgoing(self.conn, 2), [])
        self.assertEqual(len(x.incoming(self.conn, 2)), 1)

    def test_add_self_link_fails(self):
        changed, msg = x.add(self.conn, 1, 1, 'same_as')
        self.assertFalse(changed)
        self.assertIn('itself', msg)

    def test_add_unknown_subject_fails(self):
        changed, msg = x.add(self.conn, 1, 99, 'see_also')
        self.assertFalse(changed)
        self.assertIn('does not exist', msg)

    def test_remove(self):
        x.add(self.conn, 1, 3, 'related')
        changed, _ = x.remove(self.conn, 1, 3, 'related')
        self.assertTrue(changed)
        self.assertEqual(x.outgoing(self.conn, 1), [])
        changed, msg = x.remove(self.conn, 1, 3, 'related')
        self.assertFalse(changed)
        self.assertIn('not found', msg)

    def test_incoming_ordering_and_note(self):
        x.add(self.conn, 2, 1, 'narrower', 'child topic')
        rows = x.incoming(self.conn, 1)
        self.assertEqual(rows[0]['term'], 'Esoteric Occult')
        self.assertEqual(rows[0]['note'], 'child topic')

    def test_opposite_covers_all_relations(self):
        for r in x.VALID_RELATIONS:
            self.assertIn(x.OPPOSITE[r], x.VALID_RELATIONS)


if __name__ == '__main__':
    unittest.main()

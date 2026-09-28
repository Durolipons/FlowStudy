"""Cross-reference graph between subjects (the encyclopedia see-also layer).

Writes only to the `crossrefs` table; validation lives here so the routes in
app.py stay thin and this logic is unit-testable. Relations mirror the
comment in schema.sql:

    see_also | broader | narrower | related | disputed_with | cited_by | same_as

Rows are directional (from_id -> to_id) with a composite primary key of
(from_id, to_id, relation), so adding is idempotent and the reverse link is a
separate row if you want it both ways.
"""
from __future__ import annotations

import os
import sqlite3
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

VALID_RELATIONS = ('see_also', 'broader', 'narrower', 'related',
                   'disputed_with', 'cited_by', 'same_as')

# how a relation reads on the inbound side (for UI labels)
OPPOSITE = {'broader': 'narrower', 'narrower': 'broader', 'see_also': 'see_also',
            'related': 'related', 'disputed_with': 'disputed_with',
            'cited_by': 'cited_by', 'same_as': 'same_as'}


def validate(from_id: int, to_id: int, relation: str) -> str:
    """Return '' when valid, else a human-readable reason."""
    if relation not in VALID_RELATIONS:
        return ('Unknown relation %r — choose one of: %s'
                % (relation, ', '.join(VALID_RELATIONS)))
    try:
        from_id, to_id = int(from_id), int(to_id)
    except (TypeError, ValueError):
        return 'Both subjects are required'
    if from_id == to_id:
        return 'A subject cannot be cross-referenced to itself'
    return ''


def add(conn: sqlite3.Connection, from_id: int, to_id: int,
        relation: str, note: Optional[str] = None) -> Tuple[bool, str]:
    """Insert a crossref. Returns (changed, message)."""
    err = validate(from_id, to_id, relation)
    if err:
        return False, err
    for sid in (from_id, to_id):
        if not conn.execute("SELECT 1 FROM subjects WHERE id=?",
                            (sid,)).fetchone():
            return False, 'Subject #%s does not exist' % sid
    cur = conn.execute(
        "INSERT OR IGNORE INTO crossrefs(from_id,to_id,relation,note)"
        " VALUES(?,?,?,?)", (from_id, to_id, relation, note or None))
    conn.commit()
    if cur.rowcount:
        return True, 'Cross-reference saved'
    return False, 'That cross-reference already exists'


def remove(conn: sqlite3.Connection, from_id: int, to_id: int,
           relation: str) -> Tuple[bool, str]:
    """Delete a crossref. Returns (changed, message)."""
    cur = conn.execute(
        "DELETE FROM crossrefs WHERE from_id=? AND to_id=? AND relation=?",
        (from_id, to_id, relation))
    conn.commit()
    if cur.rowcount:
        return True, 'Cross-reference removed'
    return False, 'Cross-reference not found'


def outgoing(conn: sqlite3.Connection, subject_id: int) -> List[Dict]:
    """Links this subject points to."""
    return [dict(r) for r in conn.execute(
        "SELECT x.from_id, x.to_id, x.relation, x.note,"
        " s.term, s.slug FROM crossrefs x"
        " JOIN subjects s ON s.id=x.to_id WHERE x.from_id=?"
        " ORDER BY x.relation, s.term", (subject_id,))]


def incoming(conn: sqlite3.Connection, subject_id: int) -> List[Dict]:
    """Links pointing at this subject (with the relation relabelled from the
    other side, e.g. a 'broader' link in means this subject is 'narrower')."""
    return [dict(r) for r in conn.execute(
        "SELECT x.from_id, x.to_id, x.relation, x.note,"
        " s.term, s.slug FROM crossrefs x"
        " JOIN subjects s ON s.id=x.from_id WHERE x.to_id=?"
        " ORDER BY x.relation, s.term", (subject_id,))]

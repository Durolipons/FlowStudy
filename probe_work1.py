"""Inspect work #1's citation-relevant fields (types + values)."""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "library.db")
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
r = c.execute(
    "SELECT id, title, subtitle, year, work_type, pages, isbn13, doi,"
    " place, publisher_id"
    " FROM works WHERE id=1"
).fetchone()
if r:
    print("WORK 1:")
    for k in r.keys():
        print("  %-12s = %r  (%s)" % (k, r[k], type(r[k]).__name__))
else:
    print("WORK 1 NOT FOUND")
c.close()

"""Print a few real ids for smoke-testing the web app."""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "library.db")
c = sqlite3.connect(DB)
wid = c.execute("SELECT work_id FROM work_authors WHERE role='author'"
                " LIMIT 1").fetchone()[0]
slug = c.execute("SELECT slug FROM subjects LIMIT 1").fetchone()[0]
aid = c.execute("SELECT id FROM authors LIMIT 1").fetchone()[0]
cid = c.execute("SELECT id FROM collections WHERE depth=1 LIMIT 1"
                ).fetchone()[0]
print(f"WORK={wid}")
print(f"SUBJECT={slug}")
print(f"AUTHOR={aid}")
print(f"SHELF={cid}")
c.close()

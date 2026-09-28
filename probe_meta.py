"""Show a sample of file_meta rows for a smoke test."""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "library.db")
c = sqlite3.connect(DB)
rows = c.execute("select file_id, title, creator, pages, has_text,"
                 " substr(coalesce(error,''),1,120), substr(coalesce(snippet,''),1,100)"
                 " from file_meta order by file_id limit 60").fetchall()
for r in rows:
    print(r[0], "| title:", r[1], "| creator:", r[2], "| pages:", r[3],
          "| has_text:", r[4], "| err:", r[5], "| snip:", r[6])
print("total file_meta rows:", c.execute("select count(*) from file_meta").fetchone()[0])
from collections import Counter
print(Counter(r[0] for r in c.execute("select error from file_meta")))

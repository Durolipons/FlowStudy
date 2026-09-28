"""Countpending extraction leaders by ext; and show biggest unsupported exts."""
import os
import sqlite3
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "library.db")
c = sqlite3.connect(DB)
rows = c.execute("select f.ext, count(*) from files f left join file_meta m"
                 " on m.file_id=f.id where f.state='active'"
                 " and f.is_duplicate_of is null and m.file_id is null"
                 " group by f.ext order by 2 desc").fetchall()
tot = sum(n for _, n in rows)
print("pending leaders:", tot)
for ext, n in rows[:25]:
    print(f"  {ext or '(noext)':10s} {n}")

"""Break down file_meta errors/snippets to see yield quality."""
import os
import sqlite3
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "library.db")
c = sqlite3.connect(DB)
err = Counter(r[0] or "(no error)" for r in c.execute("select error from file_meta"))
print("error values:")
for k, n in err.most_common():
    print(f"  {n:5d}  {k[:90]}")
print("rows:", c.execute("select count(*) from file_meta").fetchone()[0])
print("has_text=1:", c.execute("select count(*) from file_meta where has_text=1").fetchone()[0])
print("titles:", c.execute("select count(*) from file_meta where title is not null").fetchone()[0])
print("isbns:", c.execute("select count(*) from file_meta where isbn is not null").fetchone()[0])
for r in c.execute("select file_id, title, creator, pages, has_text,"
                   " substr(coalesce(error,''),1,100) from file_meta"
                   " where error is null or error not in"
                   " ('image_no_text','media_no_text','sidecar_only',"
                   " 'unsupported_format','djvu_embed_text_unsupported')"
                   " order by file_id limit 15"):
    print(r)

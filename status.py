"""Quick project status: DB row counts, latest ingest runs, project files."""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "library.db")

print("db_exists", os.path.exists(DB))
if os.path.exists(DB):
    c = sqlite3.connect(DB)
    try:
        print("files", c.execute("select count(*) from files").fetchone()[0])
        print("active", c.execute("select count(*) from files where state='active'").fetchone()[0])
        print("dup_rows", c.execute("select count(*) from files where is_duplicate_of is not null").fetchone()[0])
        print("collections", c.execute("select count(*) from collections").fetchone()[0])
        print("works", c.execute("select count(*) from works").fetchone()[0])
        sub = c.execute("select count(*) from subjects").fetchone()[0]
        filled = c.execute("select count(*) from subjects where description is not null and trim(description) <> ''").fetchone()[0]
        print("subjects", sub, "| with encyclopedia entry", filled)
        real = c.execute("select count(*) from subjects where source='encyclopedia'").fetchone()[0]
        print("  of which real definitions (from books):", real)
        notes = c.execute("select count(*) from notes").fetchone()[0]
        by_kind = {r[0]: r[1] for r in c.execute("select kind, count(*) from notes group by kind")}
        quoted = c.execute("select count(distinct work_id) from notes where kind='quote'").fetchone()[0]
        print("notes", notes, by_kind, "| works with a quote:", quoted)
        for r in c.execute("select run_id, started, finished, files_seen, files_added, errors from ingest_log order by started desc limit 5"):
            print("run", r[0], "| started", r[1], "| finished", r[2], "| seen", r[3], "| added", r[4], "| errors", r[5])
    except Exception as e:
        print("STATUS_ERROR", type(e).__name__, e)
    finally:
        c.close()
for sub in ("reports", "data", "logs"):
    p = os.path.join(BASE, sub)
    print(sub, sorted(os.listdir(p)) if os.path.isdir(p) else "MISSING")

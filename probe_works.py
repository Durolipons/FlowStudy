"""Spot-check normalised works against the real files before full rollout."""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "library.db")
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
rows = c.execute(
    "SELECT w.id, w.title, w.subtitle, w.year, w.confidence, w.source,"
    " p.name AS pub, f.filename, f.rel_path"
    " FROM works w JOIN work_files wf ON wf.work_id=w.id"
    " JOIN files f ON f.id=wf.file_id AND f.is_duplicate_of IS NULL"
    " LEFT JOIN publishers p ON p.id=w.publisher_id"
    " ORDER BY w.id LIMIT 25").fetchall()
for r in rows:
    aus = [a['display_name'] for a in c.execute(
        "SELECT a.display_name FROM work_authors wa JOIN authors a"
        " ON a.id=wa.author_id WHERE wa.work_id=? ORDER BY wa.seq", (r['id'],))]
    subs = [s['term'] for s in c.execute(
        "SELECT s.term FROM work_subjects ws JOIN subjects s ON s.id=ws.subject_id"
        " WHERE ws.work_id=? LIMIT 4", (r['id'],))]
    print(f"[{r['id']}] {r['title']!r} sub={r['subtitle']!r} yr={r['year']}"
          f" conf={r['confidence']} src={r['source']} pub={r['pub']}")
    print(f"   authors={aus} subjects={subs}")
    print(f"   file={r['filename'][:100]}")
nfiles = c.execute("SELECT COUNT(*) FROM files f JOIN work_files wf"
                   " ON wf.file_id=f.id").fetchone()[0]
print("files attached to works:", nfiles)

"""Probe: list pending leader files (no file_meta row yet)."""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "library.db")
c = sqlite3.connect(DB)
rows = c.execute(
    "SELECT f.id, f.path, f.ext, f.size_bytes FROM files f "
    "LEFT JOIN file_meta m ON m.file_id = f.id "
    "WHERE f.state = 'active' AND f.is_duplicate_of IS NULL "
    "AND m.file_id IS NULL ORDER BY f.size_bytes DESC").fetchall()
print("pending leaders:", len(rows))
for fid, path, ext, size in rows:
    exists = os.path.exists(path)
    actual = os.path.getsize(path) if exists else -1
    print(f"  id={fid} ext={ext} meta_size={size} on_disk={actual} exists={exists}")
    print(f"     {path[:150]}")
c.close()

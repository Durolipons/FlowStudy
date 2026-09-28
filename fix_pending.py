"""Process the 14 remaining pending files directly, with explicit error handling."""
from __future__ import annotations
import os
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import db
from lib.extract import extract_one


def main() -> None:
    conn = db.get_conn()
    db.init_db(conn)
    root = db.load_config()['library_root']
    rows = list(conn.execute(
        '''SELECT f.id, f.filename, f.ext, f.path FROM files f
           LEFT JOIN file_meta m ON m.file_id = f.id
           WHERE f.state = 'active'
             AND f.is_duplicate_of IS NULL
             AND m.file_id IS NULL
           ORDER BY f.id''').fetchall())
    print(f'Processing {len(rows)} remaining pending files...')
    for fid, fn, ext, path in rows:
        size = os.path.getsize(path) if os.path.exists(path) else None
        print(f'  id={fid} ext={ext!r:6s} size={size if size is not None else "??":>12}  {fn[:50]!r}')
        try:
            _, res = extract_one((fid, path, ext))
            conn.execute(
                '''INSERT OR REPLACE INTO file_meta
                   (file_id, title, creator, publisher, pub_date, language, isbn,
                    pages, has_text, snippet, identifiers, description, archive_count,
                    archive_members, raw_json, error, extracted_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))''',
                (fid, res.get('title'), res.get('creator'), res.get('publisher'),
                 res.get('pub_date'), res.get('language'), res.get('isbn'),
                 res.get('pages'), res.get('has_text'), res.get('snippet'),
                 res.get('identifiers'), res.get('description'),
                 res.get('archive_count'), res.get('archive_members'),
                 res.get('raw_json'), res.get('error')))
            status = res.get('error') or (res.get('title') or 'OK')
            print(f'      -> {status[:70]}')
        except Exception as e:
            conn.execute(
                '''INSERT OR REPLACE INTO file_meta
                   (file_id, error, extracted_at) VALUES (?, ?, datetime('now'))''',
                (fid, str(e)[:200]))
            print(f'      -> ERROR: {e}')
        conn.commit()
    conn.close()
    print('Done.')


if __name__ == '__main__':
    main()

"""Repair a broken `doc_fts` index.

`doc_fts` was originally created as an FTS5 *external content* table over
`works` (`content='works', content_rowid='id'`) while declaring columns
`authors` and `publisher`, neither of which exists as a column on `works`. In
external-content mode FTS5 resolves a `'delete'` command by re-reading the
content table, so the `trg_works_au` trigger ended up asking for
`works.authors` - and *every* UPDATE on `works` then failed with
"database disk image is malformed". `PRAGMA integrity_check` still reported
"ok" and the index reported itself healthy, because the damage is to the
write path, not to the stored pages.

The fix (now in schema.sql) is a standalone fts5 table whose triggers use a
plain `DELETE ... WHERE rowid = OLD.id`, which never consults `works`. This
module applies that shape to an already-populated database and repopulates the
index from `works`.

Safe to re-run: a no-op when the index is already healthy.

    python fix_fts.py [--dry-run]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lib.db as db  # noqa: E402

REBUILD = """
DROP TRIGGER IF EXISTS trg_works_ai;
DROP TRIGGER IF EXISTS trg_works_au;
DROP TRIGGER IF EXISTS trg_works_ad;
DROP TABLE IF EXISTS doc_fts;
CREATE VIRTUAL TABLE doc_fts USING fts5(
    title, subtitle, authors, publisher, description,
    tokenize='unicode61'
);
CREATE TRIGGER trg_works_ai AFTER INSERT ON works BEGIN
    INSERT INTO doc_fts(rowid, title, subtitle, authors, publisher, description)
    VALUES (NEW.id, NEW.title, NEW.subtitle,
            (SELECT group_concat(a.display_name, '; ') FROM work_authors wa
              JOIN authors a ON a.id=wa.author_id WHERE wa.work_id=NEW.id
              AND wa.role IN ('author','editor')),
            (SELECT name FROM publishers p WHERE p.id=NEW.publisher_id),
            NEW.description);
END;
CREATE TRIGGER trg_works_au AFTER UPDATE ON works BEGIN
    DELETE FROM doc_fts WHERE rowid = OLD.id;
    INSERT INTO doc_fts(rowid, title, subtitle, authors, publisher, description)
    VALUES (NEW.id, NEW.title, NEW.subtitle,
            (SELECT group_concat(a.display_name, '; ') FROM work_authors wa
              JOIN authors a ON a.id=wa.author_id WHERE wa.work_id=NEW.id
               AND wa.role IN ('author','editor')),
            (SELECT name FROM publishers p WHERE p.id=NEW.publisher_id),
            NEW.description);
END;
CREATE TRIGGER trg_works_ad AFTER DELETE ON works BEGIN
    DELETE FROM doc_fts WHERE rowid = OLD.id;
END;
"""

POPULATE = """
INSERT INTO doc_fts(rowid, title, subtitle, authors, publisher, description)
SELECT w.id, w.title, w.subtitle,
       (SELECT group_concat(a.display_name, '; ') FROM work_authors wa
         JOIN authors a ON a.id=wa.author_id WHERE wa.work_id=w.id
         AND wa.role IN ('author','editor')),
       (SELECT name FROM publishers p WHERE p.id=w.publisher_id),
       w.description
FROM works w
"""


def is_broken(conn) -> bool:
    """True when a plain UPDATE on `works` fails, i.e. the shape is broken."""
    try:
        conn.execute("UPDATE works SET updated=updated WHERE id="
                     "(SELECT MIN(id) FROM works)")
        conn.rollback()
        return False
    except Exception:  # noqa: BLE001 - any failure means the trigger is broken
        conn.rollback()
        return True


def fix(dry_run: bool = False, config=None) -> int:
    cfg = config or db.load_config()
    conn = db.get_conn(cfg)

    print("db            :", cfg["db_path"])
    print("integrity     :", conn.execute("PRAGMA integrity_check").fetchone()[0])
    if not is_broken(conn):
        print("doc_fts       : already healthy, nothing to do")
        conn.close()
        return 0

    print("doc_fts       : BROKEN (UPDATE works fails) - rebuilding")
    if dry_run:
        print("dry run: no changes written")
        conn.close()
        return 0

    conn.executescript(REBUILD)
    conn.execute(POPULATE)
    conn.commit()
    print("doc_fts rows  :",
          conn.execute("SELECT COUNT(*) FROM doc_fts").fetchone()[0])
    print("works rows    :",
          conn.execute("SELECT COUNT(*) FROM works").fetchone()[0])
    print("integrity     :", conn.execute("PRAGMA integrity_check").fetchone()[0])
    print("update works  :", "OK" if not is_broken(conn) else "STILL BROKEN")
    conn.close()
    return 0


def main(argv=None) -> int:
    argv = list(argv or sys.argv[1:])
    return fix(dry_run="--dry-run" in argv)


if __name__ == "__main__":
    raise SystemExit(main())
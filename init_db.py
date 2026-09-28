"""Initialise the schema, or repair the doc_fts index on an existing database.

    python init_db.py             # create the schema if it is not there yet
    python init_db.py --fix-fts   # rebuild a broken doc_fts index (safe to repeat)
    python init_db.py --reset     # drop the database and re-create it (destructive)
"""
import os
import sys

import lib.db as db
from fix_fts import fix


def main(argv=None) -> int:
    argv = list(argv or sys.argv[1:])
    config = db.load_config()
    if "--reset" in argv:
        path = config["db_path"]
        for ext in ("", "-wal", "-shm"):
            p = path + ext
            if os.path.exists(p):
                os.remove(p)
                print("removed", p)
    conn = db.get_conn(config)
    db.init_db(conn)
    tables = [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print("schema_version:", db.schema_version(conn))
    print("tables (%d):" % len(tables), ", ".join(tables))
    conn.close()

    if "--fix-fts" in argv:
        return fix(dry_run="--dry-run" in argv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

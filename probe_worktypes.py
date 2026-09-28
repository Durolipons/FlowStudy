"""Distinct work_types and how many have pages set."""
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "data", "library.db")
c = sqlite3.connect(DB)
print('work_type                 | pages_set | count')
print('-' * 52)
for r in c.execute(
    "SELECT work_type,"
    " CASE WHEN pages IS NOT NULL THEN 'set' ELSE 'none' END,"
    " COUNT(*)"
    " FROM works GROUP BY work_type, pages IS NOT NULL"
    " ORDER BY work_type, pages IS NOT NULL"):
    print('  %-22s | %-8s | %d' % (r[0], r[1], r[2]))
print()
for label, sql in [
    ('books pages set', "SELECT COUNT(*) FROM works WHERE work_type='book' AND pages IS NOT NULL"),
    ('ebooks pages set', "SELECT COUNT(*) FROM works WHERE work_type IN ('ebook','eBook') AND pages IS NOT NULL"),
    ('chapters pages set', "SELECT COUNT(*) FROM works WHERE work_type='chapter' AND pages IS NOT NULL"),
    ('journals pages set', "SELECT COUNT(*) FROM works WHERE work_type IN ('journal','journal_article') AND pages IS NOT NULL"),
    ('magazines pages set', "SELECT COUNT(*) FROM works WHERE work_type='magazine' AND pages IS NOT NULL"),
    ('reports pages set', "SELECT COUNT(*) FROM works WHERE work_type='report' AND pages IS NOT NULL"),
    ('thesis pages set', "SELECT COUNT(*) FROM works WHERE work_type='thesis' AND pages IS NOT NULL"),
    ('web pages set', "SELECT COUNT(*) FROM works WHERE work_type='web' AND pages IS NOT NULL"),
]:
    print('%s: %s' % (label, c.execute(sql).fetchone()[0]))
c.close()

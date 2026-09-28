"""Debug: inspect merge_fields / parse_filename behaviour on failing cases."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.parse_names import parse_filename
from lib.normalise import merge_fields

print("=== parse_filename('Campbell Biology - 10th Edition.pdf') ===")
print(parse_filename('Campbell Biology - 10th Edition.pdf').to_dict())

print("\n=== merge_fields with junk title ===")
meta = {'title': '0321839455.pdf', 'creator': None, 'publisher': None,
        'isbn': None, 'pages': None, 'language': None, 'description': None,
        'pub_date': None}
m = merge_fields('Campbell Biology - 10th Edition.pdf', '.pdf', meta, '')
print({k: m[k] for k in ('title', 'subtitle', 'authors', 'source_parts')})

print("\n=== merge_fields with ISBN ===")
meta2 = {'title': 'Behold the Spirit', 'creator': None, 'publisher': None,
         'isbn': '978-0-307-78433-9', 'pages': None, 'language': None,
         'description': None, 'pub_date': None}
m2 = merge_fields('Watts - Behold the Spirit.epub', '.epub', meta2, '')
print({k: m2[k] for k in ('title', 'isbn10', 'isbn13', 'source_parts')})

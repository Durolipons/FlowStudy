"""Extract term -> definition entries from an A-Z PDF reference (pilot).

Built for Van Nostrand's Scientific Encyclopedia (A-F / G-P / Q-Z). Each entry
is an ALL-CAPS term followed by a period and its definition on the same line,
e.g. 'ANTIKNOCK AGENTS. Any of a number of organic compounds ...'. Pages carry
a '/CR' running header and a '288 ANTIKNOCK AGENTS' page-number running header;
both are stripped. 'See also ...' / 'Additional Reading' end an entry.

This module is the extraction half of the encyclopedia-definition pipeline; the
matching + writing half lives in lib/encyclopedia.py. Read-only: it reads PDFs
and returns entries, it never touches the database.

Usage (pilot):
    python lib/pdftoc.py <path-to.pdf> [--pages 0:400] [--limit N] [--json]
"""
from __future__ import annotations

import json
import os
import re
import sys
from typing import Dict, Iterator, List, Optional

# PDF text-extraction ligatures and stray unicode punctuation.
LIGATURES = {
    'ﬀ': 'ff', 'ﬁ': 'fi', 'ﬂ': 'fl', 'ﬃ': 'ffi', 'ﬄ': 'ffl',
    'ﬅ': 'st', 'ﬆ': 'st',
    '‘': "'", '’': "'", '“': '"', '”': '"',
    '–': '-', '—': '-', '−': '-', ' ': ' ',
}
_LIG_RE = re.compile('[' + ''.join(LIGATURES) + ']')

# A running header like '288 ANTIKNOCK AGENTS' or '1234 CHAPTER TITLE'.
_RUNNING_RE = re.compile(r'^\s*\d{1,5}\s+[A-Z][A-Z0-9 &/()\-]{2,}\s*$')
_CR_RE = re.compile(r'^\s*/CR\s*$')

# Entry heading: ALL-CAPS term (letters/numbers/spaces/punct) then a period,
# followed by whitespace + definition on the same line.
_HEADING_RE = re.compile(
    r'^(?P<term>[A-Z][A-Z0-9][A-Z0-9 ,\'&\-()/]*?)\.\s+(?P<rest>\S.*)$')

# Lines that terminate an entry's definition.
_TERMINATORS = ('see also', 'see also ', 'additional reading', 'bibliography',
                'literature cited', 'see references', 'further reading')


def clean_text(t: str) -> str:
    """Fix ligatures/unicode punctuation; collapse per-line whitespace.

    Keeps newlines (we need line structure to find entry boundaries) but
    removes intra-line runs of spaces.
    """
    t = _LIG_RE.sub(lambda m: LIGATURES[m.group(0)], t)
    out = []
    for line in t.splitlines():
        line = line.replace('\t', ' ')
        line = re.sub(r'[ ]{2,}', ' ', line).strip()
        out.append(line)
    return '\n'.join(out)


def _lp(path: str) -> str:
    if path.startswith('\\\\?\\'):
        return path
    return '\\\\?\\' + os.path.abspath(path)


def _is_heading(term: str) -> bool:
    """True when a candidate term looks like an A-Z dictionary heading."""
    t = term.strip()
    if not (2 <= len(t) <= 60):
        return False
    if t.upper().startswith(('TABLE', 'FIGURE', 'FIG.', 'PLATE', 'CHART',
                             'BOX', 'MAP', 'APPENDIX', 'FRONTISPIECE',
                             'PREFACE', 'CONTENTS', 'INDEX', 'BIBLIOGRAPHY',
                             'GLOSSARY', 'ACKNOWLEDG')):
        return False
    words = t.split()
    if not (1 <= len(words) <= 9):
        return False
    letters = [c for c in t if c.isalpha()]
    if len(letters) < 2 or not words[0].isupper():
        return False
    up = sum(1 for c in letters if c.isupper())
    return up / len(letters) >= 0.5


def extract_entries(pdf_path: str, first_page: int = 0,
                    last_page: Optional[int] = None,
                    max_def_chars: int = 600) -> List[Dict[str, str]]:
    """Return [{'term','definition'}, ...] for a PDF reference, in order."""
    from pypdf import PdfReader
    r = PdfReader(_lp(pdf_path))
    n = len(r.pages)
    last = n if last_page is None else min(last_page, n)
    entries: List[Dict[str, str]] = []
    seen: set = set()
    cur_term: Optional[str] = None
    cur_def: List[str] = []
    skipping = False

    def flush():
        nonlocal cur_term, cur_def
        if cur_term and cur_def:
            body = re.sub(r'\s+', ' ', ' '.join(cur_def)).strip()
            key = cur_term.lower()
            if body and key not in seen and len(body) >= 40:
                seen.add(key)
                if len(body) > max_def_chars:
                    body = body[:max_def_chars].rsplit(' ', 1)[0] + '...'
                entries.append({'term': cur_term, 'definition': body})
        cur_term = None
        cur_def = []

    for i in range(first_page, last):
        try:
            raw = r.pages[i].extract_text() or ''
        except Exception:
            continue
        for line in clean_text(raw).splitlines():
            if not line or _CR_RE.match(line) or _RUNNING_RE.match(line):
                continue
            m = _HEADING_RE.match(line)
            if m and _is_heading(m.group('term')):
                flush()
                cur_term = m.group('term').strip()
                cur_def = [m.group('rest')]
                skipping = False
                continue
            if skipping:
                continue
            low = line.lower()
            if any(low.startswith(t) for t in _TERMINATORS):
                flush()
                skipping = True
                continue
            if cur_term is not None:
                cur_def.append(line)
    flush()
    return entries


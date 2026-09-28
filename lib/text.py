r"""Full-text extraction + quote selection for the Notes & quotes panel.

Given a book file, this pulls its full body text (PDF via pypdf, EPUB via
ebooklib, TXT/HTML), chooses 1-3 genuinely quotable sentences, and hands them
back. The driver (lib/notes.py quotes) writes them as kind='quote' notes and
discards the text, so library.db stays small. Reads only; never modifies the
library.
"""
from __future__ import annotations

import html as _html
import logging
import os
import re
import sys
from typing import List, Optional, Tuple

# Damaged PDFs make pypdf emit thousands of "Object N 0 not defined" lines;
# silence it so console logs stay readable (same guard as lib/extract.py).
for _lg in ('pypdf', 'PyPDF2', 'pdfminer'):
    logging.getLogger(_lg).setLevel(logging.CRITICAL)

TEXT_EXTS = {'.pdf', '.epub', '.txt', '.html', '.htm'}


def _lp(path: str) -> str:
    if path.startswith('\\\\?\\'):
        return path
    return '\\\\?\\' + os.path.abspath(path)


def extract_text(path: str, ext: str, max_chars: int = 400000,
                 max_pages: int = 50) -> Tuple[str, Optional[str]]:
    """Return (text, error). error is None on success. Text is truncated to
    max_chars and PDFs to max_pages (we only need enough for a few quotable
    sentences, and this bounds time on huge/damaged books)."""
    ext = (ext or os.path.splitext(path)[1]).lower()
    try:
        if ext == '.pdf':
            # Prefer pymupdf: fast (~0.1-0.5s/book) and robust on damaged
            # files. Fall back to pypdf only if pymupdf isn't installed.
            try:
                import pymupdf
            except ImportError:
                pass
            else:
                doc = pymupdf.open(_lp(path))
                try:
                    parts = [p.get_text() for p in doc[:max_pages]]
                finally:
                    doc.close()
                return ('\n'.join(parts)[:max_chars], None)
            from pypdf import PdfReader
            r = PdfReader(_lp(path), strict=False)
            parts = []
            for pg in r.pages[:max_pages]:
                try:
                    parts.append(pg.extract_text() or '')
                except Exception:
                    pass
            return ('\n'.join(parts)[:max_chars], None)
        if ext == '.epub':
            from ebooklib import epub, ITEM_DOCUMENT
            from bs4 import BeautifulSoup
            book = epub.read_epub(_lp(path))
            parts = []
            for item in book.get_items_of_type(ITEM_DOCUMENT):
                try:
                    raw = item.get_content().decode('utf-8', 'replace')
                    # EPUB items are XHTML: strip tags/entities so quotes are
                    # plain prose, never "</p>&#13;<p class=...>" fragments.
                    parts.append(BeautifulSoup(raw, 'html.parser').get_text(' '))
                except Exception:
                    pass
            return ('\n'.join(parts)[:max_chars], None)
        if ext in ('.html', '.htm'):
            from bs4 import BeautifulSoup
            with open(_lp(path), encoding='utf-8', errors='replace') as fh:
                return (BeautifulSoup(fh.read(), 'html.parser').get_text(' ')[:max_chars], None)
        # txt and anything else text-like
        with open(_lp(path), encoding='utf-8', errors='replace') as fh:
            return (fh.read(max_chars), None)
    except Exception as e:
        return ('', '%s: %s' % (type(e).__name__, str(e)[:120]))


_SENT_SPLIT = re.compile(r'(?<=[.!?])\s+')
_TAG_RE = re.compile(r'<[^>]+>')
_JUNK_START = re.compile(
    r'^\s*(see also|fig\.|figure|table|plate|chapter|appendix|vol\.|volume|'
    r'pp?\.|et al|isbn|www\.|http|copyright|all rights|contents|'
    r'preface|foreword|acknowledg|index|bibliograph|references|notes\b|'
    r'\d)', re.I)
_FOOTNOTEISH = re.compile(r'(\[\d+\]|\bet al\b|\bdoi\b|\bpp?\.\s*\d+)')


def _clean_sentence(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip(' -–— "“”')


def pick_quotes(text: str, max_quotes: int = 3) -> List[str]:
    """Choose up to max_quotes genuinely quotable sentences from full text.

    Heuristic: skip front/back matter, split into sentences, and keep ones that
    read as real prose (right length, enough lowercase words, sentence
    punctuation, not a heading/caption/citation). Selection is deterministic and
    spaced across the body so quotes are not all from one spot.
    """
    t = _TAG_RE.sub(' ', text or '')
    t = _html.unescape(t)
    t = re.sub(r'\s+', ' ', t).strip()
    if len(t) < 200:
        return []
    lo = int(len(t) * 0.10)
    hi = int(len(t) * 0.95)
    body = t[lo:hi]
    sents = [_clean_sentence(s) for s in _SENT_SPLIT.split(body)]
    # the lo/hi cuts can slice through a sentence; drop the partial edges
    if len(sents) > 2:
        sents = sents[1:-1]
    cands = []
    for s in sents:
        if not (60 <= len(s) <= 280):
            continue
        if _JUNK_START.match(s) or _FOOTNOTEISH.search(s):
            continue
        letters = [c for c in s if c.isalpha()]
        if len(letters) < 20:
            continue
        # mostly-lowercase prose; reject ALL-CAPS headings
        up = sum(1 for c in letters if c.isupper())
        if up / max(len(letters), 1) > 0.35:
            continue
        if len(re.findall(r'\b[a-z]{3,}\b', s)) < 6:
            continue
        if not re.search(r'[.!?"”]$', s):
            continue
        cands.append(s)
    if not cands:
        return []
    # space the picks across the body
    out = []
    n = len(cands)
    for i in range(max_quotes):
        idx = int((i + 0.5) * n / max_quotes)
        idx = min(idx, n - 1)
        if cands[idx] not in out:
            out.append(cands[idx])
    return out


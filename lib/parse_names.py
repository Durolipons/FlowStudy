"""Filename grammar engine.

Turns messy library filenames into structured bibliographic fields:
  title / subtitle, authors, year, publisher, isbn10/isbn13, series,
  series_number, edition, kind, and a confidence score (0..1).

Heuristics are deliberately conservative and emit a confidence + list of
signals so low-certainty parses land in the review queue for triage.

Grammar classes observed in the collection:
  * ISBN-prefix publisher: 0521870445.Cambridge.University.Press.Title.Mon.2007
  * 'Author - Title [Year][A]': Frans De Waal - Evolved Morality [2014][A]
  * 'Title - Author'        : And I Suppose We Didn't Go to the Moon Either - Jim Fetzer
  * magazines               : New Scientist - 22.12.2012-04.01.2013.pdf
"""
from __future__ import annotations
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import List, Optional

MONTHS = {
    'jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'sept',
    'oct', 'nov', 'dec',
}
PUB_TOKENS: List[str] = [
    'cambridge university press', 'cambridge up', 'cambridge',
    'oxford university press', 'oxford up',
    'princeton university press', 'harvard university press',
    'yale university press', 'stanford university press',
    'university of chicago press', 'university of california press',
    'routledge', 'taylor & francis', 'taylor', 'crc press',
    'mit press', 'world scientific', 'imperial college press',
    'edinburgh university press', 'manchester university press',
    'penguin', 'penguin books', 'vintage', 'verso', 'rebellion',
    'bloomsbury', 'hodder', 'hodder stoughton', 'macmillan',
    'springer', 'springernature', 'wiley', 'wiley-blackwell',
    'blackwell', 'palgrave macmillan', 'elsevier',
    'butterworth-heinemann', 'mcgraw-hill', 'mcgraw hill',
    'ww norton', 'norton', 'academic press',
]
JUNK_TAG_RE = re.compile(
    r'\s*\[(?:StormRG|TKRG|TNRG|TPB|Team Nanban|gnv64|True PDF|PDF|EPUB|'
    r'MOBI|PRC|LIT|SCREAM|SCRAM|Book-)\]', re.I)
BRACKET_YEAR_RE = re.compile(r'\[(1[89]\d{2}|20\d{2})\]|\((1[89]\d{2}|20\d{2})\)')
YEAR_RE = re.compile(r'(1[89]\d{2}|20\d{2})')
YEAR_SCAN_RE = re.compile(r'(?<!\d)(1[89]\d{2}|20\d{2})(?!\d)')
MONTHYEAR_RE = re.compile(
    r'(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\s*(1[89]\d{2}|20\d{2})',
    re.I)
ISBN13_RE = re.compile(r'\b(?:97[89])(\d{9})\d\b')
ISBN10_RE = re.compile(r'\b((?:97[89])?\d{9}[0-9Xx])\b')
EDITION_RE = re.compile(r'\b(\d{1,2})(?:st|nd|rd|th)?\s*ed(?:ition)?\b', re.I)
SERIES_NO_RE = re.compile(r'\s*[\[(](\d{1,3})[\])]\s*$')
NAMELIKE_RE = re.compile(r'^([A-Z][a-z]+(?:\s+[A-Z][a-z\-]+){0,3})$')
ISSUE_DATE_RE = re.compile(r'\d{1,2}[\./-]\d{1,2}[\./-](19|20)\d{2}')
# Right-hand sides of "Title - X" that are structural, never a title or author:
# "Campbell Biology - 10th Edition", "Book - Volume 2", "X - Revised".
STRUCTURAL_SIDE_RE = re.compile(
    r'^(?:vol(?:ume)?\.?|part|book|set|edition|ed\.?|rev(?:ised)?|'
    r'annotated|illustrated|abridged|unabridged|complete|student|'
    r'international|paperback|hardback|\d+)\b', re.I)
# --- isbn helpers -----------------------------------------------------------
def _isbn10_valid(raw: str) -> Optional[str]:
    d = re.sub(r'[^0-9Xx]', '', raw.upper())
    if len(d) != 10:
        return None
    s = 0
    for i, ch in enumerate(d):
        if i == 9:
            val = 10 if ch == 'X' else (int(ch) if ch.isdigit() else -1)
        elif not ch.isdigit():
            return None
        else:
            val = int(ch)
        if val < 0:
            return None
        s += (i + 1) * val
    return raw if (s % 11 == 0) else None


def _isbn13_valid(raw: str) -> Optional[str]:
    d = re.sub(r'\D', '', raw)
    if len(d) != 13 or not d.startswith(('978', '979')) or not d.isdigit():
        return None
    s = sum(int(x) * (1 if i % 2 == 0 else 3) for i, x in enumerate(d[:-1]))
    check = (10 - (s % 10)) % 10
    return raw if (check == int(d[-1])) else None


def _canon_isbn(text: str) -> Optional[str]:
    m13 = ISBN13_RE.search(text)
    if m13:
        v = _isbn13_valid(m13.group(0))
        if v:
            return m13.group(0)
    for m in ISBN10_RE.finditer(text):
        v = _isbn10_valid(m.group(0))
        if v:
            return m.group(0)
    return None


@dataclass
class ParsedName:
    title: Optional[str] = None
    subtitle: Optional[str] = None
    authors: List[str] = field(default_factory=list)
    year: Optional[int] = None
    publisher: Optional[str] = None
    isbn: Optional[str] = None
    isbn13: Optional[str] = None
    isbn10: Optional[str] = None
    series: Optional[str] = None
    series_number: Optional[str] = None
    edition: Optional[str] = None
    language: Optional[str] = None
    work_type: str = 'book'
    kind: Optional[str] = None
    confidence: float = 0.0
    signals: List[str] = field(default_factory=list)
    needs_review: bool = True
    raw: str = ""

    def to_dict(self) -> dict:
        return {
            'title': self.title, 'subtitle': self.subtitle,
            'authors': self.authors, 'year': self.year,
            'publisher': self.publisher, 'isbn': self.isbn,
            'isbn13': self.isbn13, 'isbn10': self.isbn10,
            'series': self.series, 'series_number': self.series_number,
            'edition': self.edition, 'language': self.language,
            'work_type': self.work_type, 'kind': self.kind,
            'confidence': round(self.confidence, 3),
            'signals': self.signals, 'needs_review': self.needs_review,
        }


# --- normalization helpers --------------------------------------------------
def _clean_sep(text: str) -> str:
    text = text.replace('_', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def _strip_dangling_sep(text: str) -> str:
    """Remove separators left behind when a phrase was cut out.

    'Campbell Biology - ' -> 'Campbell Biology'
    """
    return re.sub(r"[\s\-–—:;,]+$", '', text or '').strip()


def _title_case(s: str) -> str:
    if not s:
        return s
    small = {'a', 'an', 'the', 'and', 'of', 'in', 'on', 'to', 'for',
             'with', 'or', 'by', 'from', 'at', 'into', 'than', 'via'}
    out = []
    for i, w in enumerate(s.split()):
        lw = w.lower()
        if i == 0 or lw not in small:
            out.append(w if w[:1].isupper() else w.capitalize())
        else:
            out.append(lw)
    return ' '.join(out)


def _split_title_subtitle(title: str) -> tuple:
    m = re.split(r'\s[-–—]\s', title, maxsplit=1)
    if len(m) == 2 and m[1].strip():
        second = m[1].strip()
        if 2 <= len(second.split()) <= 14 and second[0].isupper():
            return m[0].strip(), second
    if ':' in title:
        idx = title.find(':')
        sub = title[idx + 1:].strip()
        if sub:
            return title[:idx].strip(), sub
    return title.strip(), None


def _looks_like_name(tok: str) -> bool:
    if not NAMELIKE_RE.match(tok):
        return False
    if tok.lower() in ('the', 'a', 'an'):
        return False
    return True


def _year_from(text: str) -> Optional[int]:
    m = YEAR_SCAN_RE.search(text)
    if m:
        y = int(m.group(1))
        if 1400 <= y <= 2100:
            return y
    return None


def _publisher_from(tokens: List[str]) -> Optional[str]:
    for plen in (4, 3, 2, 1):
        for i in range(len(tokens) - plen + 1):
            cand = ' '.join(tokens[i:i + plen]).lower()
            if cand in PUB_TOKENS:
                return _title_case(' '.join(tokens[i:i + plen]))
    for t in tokens:
        if t.lower().endswith('press'):
            return _title_case(t)
    return None
# --- main parser ------------------------------------------------------------
def parse_filename(filename: str) -> ParsedName:
    pn = ParsedName(raw=filename)
    base0 = os.path.splitext(filename)[0]
    ext = os.path.splitext(filename)[1].lower()
    pn.kind = _kind_from_ext(ext, base0)

    # Capture structured year / edition / series-number BEFORE junk stripping
    # so bracketed years like [2014] are not lost.
    ym = MONTHYEAR_RE.search(base0)
    if ym:
        pn.year = int(ym.group(2))
        pn.signals.append('monthyear')
    if pn.year is None:
        y = _year_from(base0)
        if y:
            pn.year = y
            pn.signals.append('year_in_name')
    em = EDITION_RE.search(base0)
    if em:
        pn.edition = f"{em.group(1)} ed"
        pn.signals.append('edition')
    sm = SERIES_NO_RE.search(base0)
    if sm and not re.search(r'\b(volume|vol\.?|set|part|edition|ed\.?)\b', base0, re.I):
        # guard: "10 Volume Set" is not a series number; "(No.12)" handled below
        pn.series_number = sm.group(1)
        pn.signals.append('series_number')

    # Magazine / issue filename patterns: "Pub - 22.12.2012-04.01.2013".
    if ISSUE_DATE_RE.search(base0) and ' - ' in base0:
        parts = [p.strip() for p in base0.split(' - ') if p.strip()]
        if len(parts) == 2 and _looks_like_name(parts[0]):
            pn.work_type = 'magazine'; pn.kind = 'magazine'
            pn.publisher = parts[0]
            pn.title = parts[1]
            pn.confidence = 0.6
            pn.needs_review = True
            pn.signals.append('issue_magazine')
            return pn

    # Clean: strip release-group / junk brackets (year already captured).
    base = JUNK_TAG_RE.sub(' ', base0)
    base = re.sub(r'\s*\([^)]*\)', ' ', base)
    base = re.sub(r'\s*\[[^\]]+\]', ' ', base)
    # Drop the edition phrase now that pn.edition holds it, so strings like
    # "Campbell Biology - 10th Edition" cannot leave "10th Edition" as a title.
    if pn.edition:
        base = EDITION_RE.sub(' ', base)
    base = _clean_sep(base)
    base = _strip_dangling_sep(base)

    # ISBN prefix grammar -> dotted publisher (0521870445.Cambridge.UP.Title...)
    isbn = _canon_isbn(base)
    tokens = base.replace('.', ' ').split()
    if isbn and len(re.sub(r'\D', '', isbn)) >= 10:
        pn.isbn = isbn
        if len(re.sub(r'\D', '', isbn)) == 13:
            pn.isbn13 = isbn
        else:
            pn.isbn10 = isbn
        pn.signals.append('isbn_valid')
        idx = next((i for i, t in enumerate(tokens)
                    if t == isbn or t.startswith(isbn[:8])), 0)
        remainder = tokens[idx + 1:]
        pn.year = pn.year or _year_from(' '.join(remainder))
        pub = _publisher_from(remainder)
        if pub:
            pn.publisher = pub
            pub_tokens = pub.lower().split()
            rest = list(remainder)
            while rest and ' '.join(rest[:len(pub_tokens)]).lower() == pub.lower():
                rest = rest[len(pub_tokens):]
        else:
            rest = remainder
        rest = [t for t in rest if not re.fullmatch(
            r'(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)', t, re.I)]
        rest = [t for t in rest if not YEAR_RE.fullmatch(t)]
        title = ' '.join(rest).strip('.,; ')
        t, sub = _split_title_subtitle(title)
        pn.title, pn.subtitle = t, sub
        pn.work_type = 'book'
        pn.kind = 'book'
        pn.confidence = 0.92
        pn.needs_review = False
        pn.signals.append('isbn_prefix_dotted')
        return pn

    # dash-separated 'Author - Title' / 'Title - Author'
    if ' - ' in base:
        parts = [p for p in base.split(' - ') if p.strip()]
        if len(parts) == 2:
            left, right = parts[0].strip(), parts[1].strip()
            left_n = _looks_like_name(left)
            right_n = _looks_like_name(right)
            author, title = None, base
            if left_n and right_n and (
                    STRUCTURAL_SIDE_RE.match(right)
                    or STRUCTURAL_SIDE_RE.match(left)):
                # "X - 10th Edition" / "X - Volume 2": neither side is a person
                author, title = None, base
                pn.signals.append('dash_structural_side')
                pn.needs_review = True
            elif left_n and not right_n and len(left.split()) <= 4:
                author, title = left, right
                pn.signals.append('author_left')
            elif right_n and not left_n and len(right.split()) <= 4:
                author, title = right, left
                pn.signals.append('author_right')
            elif left_n and right_n:
                author, title = (left, right) if len(left) <= len(right) else (right, left)
                pn.signals.append('dash_ambiguous')
                pn.needs_review = True
            else:
                pn.signals.append('dash_no_name')
            if author:
                pn.authors = [author]
            if title != base:
                title = _strip_trailing_year(title)
                t, sub = _split_title_subtitle(title)
                pn.title, pn.subtitle = t, sub
            pub = _publisher_from(base.split())
            if pub:
                pn.publisher = pub
                pn.signals.append('publisher_found')
            if pn.publisher:
                pn.confidence = max(pn.confidence, 0.62)
            if pn.year:
                pn.confidence = max(pn.confidence, 0.68 if pn.needs_review else 0.72)
            else:
                pn.confidence = max(pn.confidence, 0.45)
            pn.work_type = _kind_from_ext(ext, pn.title)
            pn.kind = pn.work_type
            return pn

    # fallback: whole string is the title
    title = _strip_trailing_year(base)
    t, sub = _split_title_subtitle(title)
    pn.title, pn.subtitle = t, sub
    pub = _publisher_from(base.split())
    if pub:
        pn.publisher = pub
        pn.signals.append('publisher_found')
        pn.confidence = max(pn.confidence, 0.6)
    if pn.year:
        pn.confidence = max(pn.confidence, 0.55)
    pn.needs_review = pn.confidence < 0.8
    return pn
# --- misc helpers -----------------------------------------------------------
def _strip_trailing_year(text: str) -> str:
    text = re.sub(
        r'\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\s+(1[89]\d{2}|20\d{2})\s*$',
        ' ', text, flags=re.I)
    text = re.sub(r'\s*(1[89]\d{2}|20\d{2})\s*$', ' ', text)
    return _clean_sep(text)


def _kind_from_ext(ext: str, title: Optional[str]) -> str:
    t = (title or '').lower()
    if ext == '.pdf':
        if any(k in t for k in ('issue', 'review', 'quarterly', 'annual')):
            return 'journal'
        if any(k in t for k in ('new scientist', 'economist', 'monthly')):
            return 'magazine'
        return 'book'
    if ext in ('.htm', '.html'):
        return 'web'
    if ext == '.txt':
        return 'book'
    if ext in ('.epub', '.mobi', '.prc', '.lit', '.azw', '.azw3', '.djvu',
               '.pdb', '.rtf'):
        return 'book'
    if ext in ('.mp3', '.wma', '.m4a', '.wav', '.flac', '.aac'):
        return 'audiovisual'
    if ext in ('.mp4', '.avi', '.mov', '.mkv'):
        return 'audiovisual'
    if ext in ('.jpg', '.jpeg', '.png', '.bmp', '.gif', '.tif', '.tiff'):
        return 'image'
    if ext in ('.rar', '.zip', '.7z', '.r00'):
        return 'archive'
    return 'other'


if __name__ == '__main__':
    import sys
    for f in sys.argv[1:]:
        print(parse_filename(f).to_dict())

# PARSE_END




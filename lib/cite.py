"""Harvard (Cite Them Right style) reference and in-text citation formatter.

Public API:
    Author(display='Alan W. Watts')            # given/family split handled best-effort
    work  = Work(title='...', authors=[Author,...], year=2013, publisher=...,
                 place='London', edition=None, pages='42-55',
                 url='https://...', doi=None, isbn=None, accessed='...', ...)
    builder = HarvardBuilder()
    ref = builder.reference(work, 'book')      # -> Reference (rendered_list + rendered_in)
    builder.bibliography([work, ...])          # sorted, deduped, plain text

Italics are emitted with *...* markers (plain text) and converted to <i>..</i>
in render_html.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional


@dataclass
class Author:
    display: str = ""
    given: Optional[str] = None
    family: Optional[str] = None
    orcid: Optional[str] = None

    @classmethod
    def parse(cls, raw: str) -> "Author":
        raw = (raw or "").strip()
        if ',' in raw:
            fam, giv = raw.split(',', 1)
            fam, giv = fam.strip(), giv.strip()
        else:
            parts = raw.split()
            if len(parts) == 1:
                fam, giv = parts[0], None
            else:
                fam, giv = parts[-1], ' '.join(parts[:-1])
        return cls(display=raw, given=giv, family=fam)

    @property
    def surname(self) -> str:
        return (self.family or self.display).strip()

    @property
    def initials(self) -> str:
        g = (self.given or "").strip()
        if not g:
            return ""
        out = []
        for tok in g.split():
            if not tok:
                continue
            out.append(tok[0].upper() + ('.' if len(tok) > 1 else ''))
        return ' '.join(out).replace(' .', '.')

    @property
    def ref(self) -> str:
        """Reference-list form: 'Watts, A.'"""
        i = self.initials
        return f"{self.surname}, {i}" if i else self.surname

    @property
    def cite(self) -> str:
        return self.surname


def _join_authors(authors: List[Author], max_list: int = 6) -> str:
    """Reference-list author join per Cite Them Right."""
    if not authors:
        return "Anon"
    if len(authors) > max_list:
        return authors[0].ref + " et al."
    refs = [a.ref for a in authors]
    if len(refs) == 1:
        return refs[0]
    if len(refs) == 2:
        return f"{refs[0]} and {refs[1]}"
    return ", ".join(refs[:-1]) + " and " + refs[-1]


def _authors_in_text(authors: List[Author]) -> str:
    if not authors:
        return "Anon"
    if len(authors) == 1:
        return authors[0].cite
    if len(authors) == 2:
        return f"{authors[0].cite} and {authors[1].cite}"
    return authors[0].cite + " et al."
# --- Work -------------------------------------------------------------------
@dataclass
class Work:
    title: str = ""
    subtitle: Optional[str] = None
    authors: List[Author] = field(default_factory=list)
    editors: List[Author] = field(default_factory=list)
    year: Optional[int] = None
    year_raw: Optional[str] = None       # e.g. 'n.d.', '2013?'
    edition: Optional[str] = None
    place: Optional[str] = None
    publisher: Optional[str] = None
    pages: Optional[str] = None
    volume: Optional[str] = None
    number: Optional[str] = None
    series: Optional[str] = None        # journal / magazine name
    url: Optional[str] = None
    doi: Optional[str] = None
    isbn10: Optional[str] = None
    isbn13: Optional[str] = None
    accessed: Optional[str] = None      # ISO date
    work_type: str = 'book'
    collection_title: Optional[str] = None   # for chapters
    report_no: Optional[str] = None
    thesis_inst: Optional[str] = None
    unpublished: bool = False
    website_name: Optional[str] = None
    page_title: Optional[str] = None

    @staticmethod
    def from_fields(f: dict) -> "Work":
        def alist(d):
            if not d:
                return []
            if isinstance(d, str):
                return [Author.parse(d)]
            return [Author.parse(x) if isinstance(x, str) else x for x in d]
        return Work(
            title=f.get('title', '') or '',
            subtitle=f.get('subtitle'),
            authors=alist(f.get('authors')), editors=alist(f.get('editors')),
            year=f.get('year'), year_raw=f.get('year_raw'),
            edition=f.get('edition'), place=f.get('place'),
            publisher=f.get('publisher'), pages=f.get('pages'),
            volume=f.get('volume'), number=f.get('number'),
            series=f.get('series'), url=f.get('url'), doi=f.get('doi'),
            isbn10=f.get('isbn10'), isbn13=f.get('isbn13'),
            accessed=f.get('accessed'), work_type=f.get('work_type', 'book'),
            collection_title=f.get('collection_title'),
            report_no=f.get('report_no'), thesis_inst=f.get('thesis_inst'),
            unpublished=f.get('unpublished', False),
            website_name=f.get('website_name'), page_title=f.get('page_title'),
        )

    def _year_str(self) -> str:
        if self.year is not None:
            return str(self.year)
        if self.year_raw:
            return self.year_raw
        return "n.d."

    def _edition_str(self) -> str:
        if not self.edition:
            return ""
        ed = self.edition.strip()
        if re.match(r'^[0-9]+(?:st|nd|rd|th)?$', ed, re.I):
            return f"{ed} ed"
        if ed.lower() in ('rev', 'revised'):
            return "rev ed"
        if 'ed' not in ed.lower():
            return f"{ed} ed"
        return ed

    def _full_title(self) -> str:
        t = (self.title or '').strip()
        if self.subtitle:
            t += f": {self.subtitle.strip()}"
        return t
# --- Harvard Cite Them Right builder ----------------------------------------
class Reference:
    """A rendered Harvard reference plus in-text form and issue list."""
    def __init__(self, rendered_list, rendered_in, work, issues=None):
        self.rendered_list = rendered_list
        self.rendered_in = rendered_in
        self.work = work
        self.issues = issues or []

    def __str__(self):
        return self.rendered_list


class HarvardBuilder:
    """Builds Harvard (Cite Them Right) references and in-text citations."""

    def italic(self, s: str) -> str:
        return f"*{s.strip()}*" if s and s.strip() else ""

    def reference(self, work: Work, kind: Optional[str] = None) -> Reference:
        kind = kind or work.work_type
        issues: List[str] = []
        self._validate(work, kind, issues)
        yr = work._year_str()
        if kind in ('book', 'ebook', 'eBook'):
            out = self._ref_book(work, yr)
        elif kind == 'chapter' or work.collection_title:
            out = self._ref_chapter(work, yr)
        elif kind in ('journal', 'journal_article'):
            out = self._ref_journal(work, yr)
        elif kind == 'magazine':
            out = self._ref_magazine(work, yr)
        elif kind == 'web':
            out = self._ref_web(work, yr)
        elif kind == 'thesis':
            out = self._ref_thesis(work, yr)
        elif kind == 'report':
            out = self._ref_report(work, yr)
        else:
            out = self._ref_book(work, yr)
        out = re.sub(r'  +', ' ', out).strip().rstrip('.') + '.'
        return Reference(out, self.in_text(work, kind), work, issues)

    def in_text(self, work: Work, kind: str = 'book') -> str:
        if len(work.authors) >= 3:
            a = f"{work.authors[0].cite} et al."
        elif len(work.authors) == 2:
            a = f"{work.authors[0].cite} and {work.authors[1].cite}"
        elif len(work.authors) == 1:
            a = work.authors[0].cite
        elif work.editors:
            a = f"{work.editors[0].cite} (ed.)"
        else:
            a = "Anon"
        base = f"({a}, {work._year_str()}"
        if work.pages and kind in ('chapter', 'journal',
                                   'journal_article', 'magazine'):
            base += f", p. {str(work.pages).split(':')[0].strip()}"
        return base + ")"

    def bibliography(self, works) -> str:
        b = HarvardBuilder()
        seen, refs = set(), []
        for w in works:
            w = Work.from_fields(w) if isinstance(w, dict) else w
            ref = b.reference(w)
            if ref.rendered_list in seen:
                continue
            seen.add(ref.rendered_list)
            refs.append(ref)

        def skey(r):
            au = r.work.authors[0].family if r.work.authors else 'zzz'
            return (au.upper(), r.work.year or 0, (r.work.title or '').upper())
        refs.sort(key=skey)
        return "\n".join(f"{i+1}. {_strip_italics(r.rendered_list)}"
                         for i, r in enumerate(refs))

    def render_html(self, ref: Reference) -> str:
        return re.sub(r'\*([^*]+)\*', r'<i>\1</i>', ref.rendered_list)
    # -- per-type templates --------------------------------------------------
    def _ref_book(self, w: Work, yr: str) -> str:
        s = f"{_join_authors(w.authors)} ({yr}) {self.italic(w._full_title())}"
        if (ed := w._edition_str()):
            s += f". {ed}"
        if w.place or w.publisher:
            loc = (w.place or "Place of publication unknown").rstrip(',')
            s += f". {loc}: {w.publisher or 'publisher unknown'}"
        if w.series:
            ser = w.series
            if w.series_number:
                ser += f", {w.series_number}"
            s += f". ({ser})"
        if w.doi:
            s += f" doi:{w.doi.lower()}."
        elif w.isbn13:
            s += f" isbn:{w.isbn13}."
        return s

    def _ref_chapter(self, w: Work, yr: str) -> str:
        eds = _join_authors(w.editors) if w.editors else 'Anon'
        s = (f"{_join_authors(w.authors)} ({yr}) '{w._full_title()}' in "
             f"{eds} (ed.) {self.italic(w.collection_title or '')}.")
        if w.place or w.publisher:
            loc = (w.place or "Place of publication unknown").rstrip(',')
            s += f" {loc}: {w.publisher or 'publisher unknown'}"
        if w.pages:
            s += f". pp. {w.pages}"
        return s

    def _ref_journal(self, w: Work, yr: str) -> str:
        vol = w.volume or ''
        if w.number:
            vol += f"({w.number})"
        jour = w.series or w.publisher or ''
        s = (f"{_join_authors(w.authors)} ({yr}) '{w._full_title()}' "
             f"{self.italic(jour)}, {vol}, {w.pages}.")
        return re.sub(r'  +', ' ', s).rstrip()

    def _ref_magazine(self, w: Work, yr: str) -> str:
        s = (f"{_join_authors(w.authors)} ({yr}) '{w._full_title()}' "
             f"{self.italic(w.series or '')}, {w.pages}.")
        return re.sub(r'  +', ' ', s).strip()

    def _ref_web(self, w: Work, yr: str) -> str:
        s = (f"{_join_authors(w.authors)} ({yr}) {self.italic(w._full_title())}. "
             f"{w.website_name or ''}. Available at: {w.url} "
             f"(Accessed: {self._accessed(w.accessed)}).")
        return re.sub(r'  +', ' ', s).replace(' .', '.')

    def _ref_thesis(self, w: Work, yr: str) -> str:
        s = f"{_join_authors(w.authors)} ({yr}) {self.italic(w._full_title())}."
        if w.unpublished:
            s += " Unpublished"
        s += f" PhD thesis, {w.thesis_inst or 'University'}."
        return s

    def _ref_report(self, w: Work, yr: str) -> str:
        s = f"{_join_authors(w.authors)} ({yr}) {self.italic(w._full_title())}."
        if w.report_no:
            s += f" {w.report_no}."
        if w.publisher:
            loc = (w.place or "").rstrip(',')
            s += f" {loc + ': ' if loc else ''}{w.publisher}."
        return s

    # -- support ------------------------------------------------------------
    def _accessed(self, iso_date) -> str:
        if not iso_date:
            return "n.d."
        months = ['', 'January', 'February', 'March', 'April', 'May', 'June',
                  'July', 'August', 'September', 'October', 'November',
                  'December']
        try:
            d = datetime.fromisoformat(str(iso_date).replace('Z', '+00:00'))
            return f"{d.day} {months[d.month]} {d.year}"
        except Exception:
            return str(iso_date)

    def _validate(self, work: Work, kind, issues: List[str]):
        if not work.title:
            issues.append("missing title")
        if kind in ('book', 'chapter') and not work.year and not work.year_raw:
            issues.append("missing year")
        if kind == 'book' and not work.publisher:
            issues.append("missing publisher")
        if kind in ('book', 'chapter') and not work.place:
            issues.append("missing place of publication")
        if kind == 'web' and not work.url:
            issues.append("missing url")


def _strip_italics(s: str) -> str:
    return s.replace('*', '')
# C3_END




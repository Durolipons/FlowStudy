"""Bibliography export: BibTeX, RIS, CSV, DOCX and PDF.

Builders consume lib.cite.Work objects, so an export reflects exactly what
the Harvard formatter sees. Entries are ordered like the bibliography
(first-author family, year, title) so numbered outputs stay meaningful.

Only BibTeX/RIS/CSV are dependency-free; DOCX needs python-docx and PDF
needs reportlab (see requirements.txt). Those imports are lazy so a missing
package only breaks its own format, not the module.
"""
from __future__ import annotations

import csv
import html as _html_mod
import io
import re
import unicodedata
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from lib.cite import HarvardBuilder, Work

_BIBTEX_TYPE = {
    'book': 'book', 'ebook': 'book', 'eBook': 'book',
    'chapter': 'incollection',
    'journal': 'article', 'journal_article': 'article', 'magazine': 'article',
    'thesis': 'phdthesis', 'report': 'techreport',
    'web': 'misc', 'course': 'misc', 'audiovisual': 'misc',
}

_RIS_TYPE = {
    'book': 'BOOK', 'ebook': 'EBOOK', 'eBook': 'EBOOK',
    'chapter': 'CHAP',
    'journal': 'JOUR', 'journal_article': 'JOUR', 'magazine': 'MGZN',
    'thesis': 'THES', 'report': 'RPRT',
    'web': 'ELEC', 'course': 'GEN', 'audiovisual': 'AV',
}

_CSV_COLS = ['work_id', 'title', 'subtitle', 'authors', 'editors', 'year',
             'edition', 'publisher', 'place', 'volume', 'number', 'series',
             'pages', 'url', 'doi', 'isbn10', 'isbn13',
             'work_type', 'harvard', 'in_text']


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------
def sort_key(w: Work) -> Tuple[str, int, str]:
    fam = (w.authors[0].family if w.authors else 'zzz') or 'zzz'
    return (fam.upper(), w.year or 0, (w.title or '').upper())


def sorted_works(works: List[Work]) -> List[Work]:
    return sorted(works, key=sort_key)


def _ascii(s: Optional[str]) -> str:
    return unicodedata.normalize('NFKD', s or '') \
        .encode('ascii', 'ignore').decode('ascii')


_TEX_ESC = {'\\': r'\textbackslash{}', '{': r'\{', '}': r'\}',
            '%': r'\%', '&': r'\&', '$': r'\$', '#': r'\#',
            '_': r'\_', '~': r'\~{}', '^': r'\^{}'}


def _tex(s: Optional[str]) -> str:
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    return ''.join(_TEX_ESC.get(ch, ch) for ch in s)


def _flat(s: Optional[str]) -> str:
    return re.sub(r'\s+', ' ', str(s or '')).strip()


def _person(a) -> str:
    fam = (a.family or '').strip()
    giv = (a.given or '').strip()
    if fam and giv:
        return f"{fam}, {giv}"
    if fam:
        return fam
    return (a.display or 'Anon').strip() or 'Anon'


def _people(w: Work) -> Tuple[List[str], List[str]]:
    """Split names into (authors, editors) without duplicating editor-only
    records across both fields."""
    eds = {_flat(a.display) for a in (w.editors or [])}
    aus = [a for a in (w.authors or []) if _flat(a.display) not in eds]
    if not aus and w.authors:
        # editor-only record: cite it under editor, not author
        return [], [_person(a) for a in w.authors]
    authors = [_person(a) for a in aus]
    editors = [_person(a) for a in (w.editors or [])]
    return authors, editors


def _split_pages(pages: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    p = (pages or '').strip()
    if not p:
        return None, None
    m = re.match(r'^(.+?)\s*[-:\u2013\u2014]\s*(.+)$', p)
    if m:
        return (m.group(1).strip() or None, m.group(2).strip() or None)
    return p, None


def bib_key(w: Work, seen: set) -> str:
    fam = re.sub(r'[^a-z]', '', _ascii(
        w.authors[0].family if w.authors else 'anon').lower()) or 'anon'
    yr = str(w.year) if w.year else 'nd'
    words = re.findall(r'[a-z0-9]+', _ascii(w.title).lower())
    stem = (words[0][:14] if words else 'untitled')
    cand, i = f"{fam}{yr}{stem}", 98  # 'b'
    while cand in seen:
        cand = f"{fam}{yr}{stem}{chr(i)}"
        i += 1
    seen.add(cand)
    return cand



# ---------------------------------------------------------------------------
# BibTeX
# ---------------------------------------------------------------------------
def _bib_pages(pages: Optional[str]) -> Optional[str]:
    p = (pages or '').strip()
    if p and '-' in p and '--' not in p:
        p = p.replace('-', '--')
    return p or None


def _bib_fields(w: Work) -> Tuple[str, Dict[str, str]]:
    kind = _BIBTEX_TYPE.get((w.work_type or 'book'), 'misc')
    authors, editors = _people(w)
    f: Dict[str, str] = {}
    if authors:
        f['author'] = ' and '.join(authors)
    elif not editors:
        f['author'] = 'Anon'  # mirrors the Harvard renderer + RIS
    if editors:
        f['editor'] = ' and '.join(editors)
    f['title'] = _tex(w.title + (': ' + w.subtitle
                                 if (w.subtitle or '').strip() else ''))
    f['year'] = str(w.year) if w.year else _tex(w.year_raw or 'n.d.')
    if kind == 'book':
        for k, v in (('publisher', w.publisher), ('address', w.place),
                     ('edition', w.edition), ('volume', w.volume),
                     ('series', w.series), ('isbn', w.isbn13 or w.isbn10),
                     ('doi', w.doi), ('url', w.url)):
            if v and str(v).strip():
                f[k] = _tex(v)
    elif kind == 'incollection':
        for k, v in (('booktitle', w.collection_title),
                     ('publisher', w.publisher), ('address', w.place),
                     ('pages', _bib_pages(w.pages)),
                     ('volume', w.volume), ('series', w.series),
                     ('isbn', w.isbn13 or w.isbn10), ('doi', w.doi),
                     ('url', w.url)):
            if v and str(v).strip():
                f[k] = _tex(v)
    elif kind == 'article':
        for k, v in (('journal', w.series or w.publisher),
                     ('volume', w.volume), ('number', w.number),
                     ('pages', _bib_pages(w.pages)),
                     ('doi', w.doi), ('url', w.url)):
            if v and str(v).strip():
                f[k] = _tex(v)
    f.update(_bib_tail(w, kind))
    return kind, f


# ---------------------------------------------------------------------------
# BibTeX tail (phdthesis / techreport / misc)
# ---------------------------------------------------------------------------
def _bib_tail(w: Work, kind: str) -> Dict[str, str]:
    f: Dict[str, str] = {}
    if kind == 'phdthesis':
        for k, v in (('school', w.thesis_inst or w.publisher),
                     ('address', w.place)):
            if v and str(v).strip():
                f[k] = _tex(v)
        if w.unpublished:
            f['note'] = 'Unpublished'
    elif kind == 'techreport':
        for k, v in (('institution', w.publisher),
                     ('number', w.report_no), ('address', w.place),
                     ('doi', w.doi), ('url', w.url)):
            if v and str(v).strip():
                f[k] = _tex(v)
    else:  # misc: web, course, audiovisual, unknown
        for k, v in (('howpublished', w.url), ('doi', w.doi),
                     ('isbn', w.isbn13 or w.isbn10)):
            if v and str(v).strip():
                f[k] = _tex(v)
        if w.accessed:
            f['note'] = 'Accessed: ' + _tex(
                HarvardBuilder()._accessed(w.accessed))
    return f


# ---------------------------------------------------------------------------
# RIS
# ---------------------------------------------------------------------------
def _ris_record(w: Work) -> List[str]:
    lines: List[str] = []

    def add(tag: str, val: Optional[str]) -> None:
        if val is not None and str(val).strip():
            lines.append('%s  - %s' % (tag, _flat(val)))

    add('TY', _RIS_TYPE.get((w.work_type or 'book'), 'GEN'))
    authors, editors = _people(w)
    for a in authors:
        add('AU', a)
    if not authors and not editors:
        add('AU', 'Anon')
    for a in editors:
        add('A2', a)
    add('TI', w._full_title())
    add('T2', w.collection_title or w.series)
    add('PY', str(w.year) if w.year else w.year_raw)
    sp, ep = _split_pages(w.pages)
    add('SP', sp)
    add('EP', ep)
    add('VL', w.volume)
    add('IS', w.number)
    add('ED', w.edition)
    if (w.work_type or '') == 'thesis':
        add('PB', w.thesis_inst or w.publisher)
    else:
        add('PB', w.publisher)
    add('CY', w.place)
    add('SN', w.isbn13 or w.isbn10)
    add('DO', w.doi)
    add('UR', w.url)
    add('Y2', w.accessed)
    if (w.work_type or '') == 'report':
        add('N1', w.report_no)
    lines.append('ER  - ')
    return lines


def ris(works: List[Work]) -> str:
    if not works:
        return ''
    lines: List[str] = []
    for w in sorted_works(works):
        lines.extend(_ris_record(w))
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def csv_text(works: List[Work], ids=()) -> str:
    ids = list(ids) + [None] * max(0, len(works) - len(list(ids)))
    pairs = sorted(zip(works, ids), key=lambda p: sort_key(p[0]))
    b = HarvardBuilder()
    buf = io.StringIO(newline='')
    wr = csv.writer(buf)
    wr.writerow(_CSV_COLS)
    for w, i in pairs:
        authors, editors = _people(w)
        ref = b.reference(w)
        wr.writerow([
            i, w.title or '', w.subtitle or '',
            '; '.join(authors), '; '.join(editors),
            w.year if w.year is not None else (w.year_raw or ''),
            w.edition or '', w.publisher or '', w.place or '',
            w.volume or '', w.number or '', w.series or '',
            w.pages or '', w.url or '',
            w.doi or '', w.isbn10 or '', w.isbn13 or '',
            w.work_type or 'book', ref.rendered_list, ref.rendered_in,
        ])
    out = buf.getvalue()
    assert out is not None
    return out


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------
def _stamp() -> str:
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


def _runs(par, text: str) -> None:
    """Add runs to a docx paragraph, honouring *italic* markers."""
    for i, part in enumerate(text.split('*')):
        if not part:
            continue
        run = par.add_run(part)
        if i % 2 == 1:
            run.italic = True


def docx_bytes(works: List[Work],
               title: str = 'Bibliotheca bibliography') -> bytes:
    try:
        from docx import Document
    except ImportError:
        raise ImportError('python-docx is required for DOCX export '
                          '(pip install python-docx)')
    doc = Document()
    doc.add_heading(title, level=1)
    doc.add_paragraph('Exported %s · %d reference(s)' % (_stamp(), len(works)))
    b = HarvardBuilder()
    doc.add_heading('Bibliography', level=2)
    for w in sorted_works(works):
        ref = b.reference(w)
        p = doc.add_paragraph(style='List Number')
        _runs(p, ref.rendered_list)
    doc.add_heading('In-text citations', level=2)
    for w in sorted_works(works):
        ref = b.reference(w)
        doc.add_paragraph(ref.rendered_in, style='List Bullet')
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PDF (reportlab)
# ---------------------------------------------------------------------------
def _pdf_escape(text: str) -> str:
    """Escape HTML specials but keep the <i> italics from render_html."""
    esc = _html_mod.escape(text)
    return esc.replace('&lt;i&gt;', '<i>').replace('&lt;/i&gt;', '</i>')


def _pdf_footer(canvas, doc) -> None:
    from reportlab.lib.units import mm
    canvas.saveState()
    canvas.setFont('Helvetica', 8)
    canvas.drawCentredString(doc.pagesize[0] / 2, 12 * mm,
                             'Page %d' % doc.page)
    canvas.restoreState()


def pdf_bytes(works: List[Work],
              title: str = 'Bibliotheca bibliography') -> bytes:
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer)
    except ImportError:
        raise ImportError('reportlab is required for PDF export '
                          '(pip install reportlab)')
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, title=title,
                            leftMargin=20 * mm, rightMargin=20 * mm,
                            topMargin=18 * mm, bottomMargin=18 * mm)
    styles = getSampleStyleSheet()
    h1 = ParagraphStyle('bibH1', parent=styles['Heading1'], fontSize=16)
    body = ParagraphStyle('bibBody', parent=styles['Normal'],
                          fontSize=10, leading=14)
    b = HarvardBuilder()
    story = [Paragraph(_pdf_escape(title), h1),
             Paragraph('Exported %s · %d reference(s)'
                       % (_stamp(), len(works)), body),
             Spacer(1, 6 * mm),
             Paragraph('Bibliography', h1)]
    for i, w in enumerate(sorted_works(works), 1):
        ref = b.reference(w)
        story.append(Paragraph('%d.&nbsp;&nbsp;%s'
                               % (i, _pdf_escape(b.render_html(ref))),
                               body))
        story.append(Spacer(1, 3 * mm))
    story.append(Paragraph('In-text citations', h1))
    for w in sorted_works(works):
        story.append(Paragraph('•&nbsp;&nbsp;%s'
                               % _pdf_escape(b.reference(w).rendered_in),
                               body))
    doc.build(story, onFirstPage=_pdf_footer, onLaterPages=_pdf_footer)
    return buf.getvalue()


def bibtex(works: List[Work]) -> str:
    seen: set = set()
    out = []
    for w in sorted_works(works):
        kind, f = _bib_fields(w)
        out.append('@%s{%s,' % (kind, bib_key(w, seen)))
        for k, v in f.items():
            out.append('  %s = {%s},' % (k, v))
        out.append('}')
        out.append('')
    return '\n'.join(out).rstrip() + '\n' if out else ''

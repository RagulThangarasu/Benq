"""Cropped, highlighted before/after screenshots for content-validation issues.

A "missing fragment" issue is an absence on the STAGE side, so there is nothing
on a STAGE page to box.  Instead each issue is rendered as a *pair* of crops:

    PROD   — the fragment located and highlighted in red (this is the content)
    STAGE  — the same neighbourhood, anchored on the words either side of the
             fragment that DID survive, with an amber gap marker drawn where
             the missing content should have been

Both crops are tight around the issue (not full pages) and come back as PNG
bytes plus a plain-English comment.  Shared by the Guide web view and the Q&A
Index PDF report so the two never drift.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import fitz

# Rendering
ZOOM = 2.0            # 144 dpi — legible when scaled into a letter-size report
PAD_X = 14.0          # points of page context kept around the issue band
PAD_Y = 16.0
MAX_BAND_H = 260.0    # cap a crop's height so one long fragment can't eat a page

# Colours (RGB 0-1)
RED = (0.72, 0.11, 0.11)
AMBER = (0.85, 0.47, 0.02)
RED_FILL = (1.0, 0.87, 0.87)

_PUNCT_RE = re.compile(r"[^\w]+", re.UNICODE)


def _norm(word: str) -> str:
    """Canonical form for matching: lowercase, punctuation-free."""
    return _PUNCT_RE.sub("", (word or "").lower())


@dataclass
class _PageWords:
    page_no: int                      # 1-based
    rects: list = field(default_factory=list)
    toks: list = field(default_factory=list)


def _page_words(doc, page_no: int) -> _PageWords:
    """Normalised token stream for one page, parallel to its word rects."""
    pw = _PageWords(page_no)
    for x0, y0, x1, y1, w, *_ in doc[page_no - 1].get_text("words"):
        t = _norm(w)
        if not t:
            continue
        pw.rects.append(fitz.Rect(x0, y0, x1, y1))
        pw.toks.append(t)
    return pw


def _find_run(toks: list, needle: list, min_len: int = 3):
    """Locate `needle` in `toks`, shrinking from the right until it matches.

    Returns (start, end_exclusive) or None.  Shrinking matters because a
    fragment reported by the validator often straddles a column or page break,
    so only its opening words are present on any single page.
    """
    if not needle:
        return None
    n = len(toks)
    for size in range(len(needle), min_len - 1, -1):
        probe = needle[:size]
        first = probe[0]
        for i in range(n - size + 1):
            if toks[i] == first and toks[i:i + size] == probe:
                return i, i + size
    return None


def _merge_lines(rects: list) -> list:
    """Collapse per-word rects into one box per text line.

    A box around every individual word reads as visual noise; one box per line
    is what a reviewer would draw by hand.
    """
    out = []
    for r in sorted(rects, key=lambda r: (round(r.y0, 1), r.x0)):
        prev = out[-1] if out else None
        # Same line when the vertical spans overlap by most of their height.
        if prev is not None and min(prev.y1, r.y1) - max(prev.y0, r.y0) > 0.6 * r.height:
            out[-1] = prev | r
        else:
            out.append(fitz.Rect(r))
    return [fitz.Rect(r.x0 - 1.5, r.y0 - 1.5, r.x1 + 1.5, r.y1 + 1.5) for r in out]


def _band(rects: list, page_rect) -> fitz.Rect:
    """Full-width band covering `rects`, padded and clipped to the page."""
    u = fitz.Rect(rects[0])
    for r in rects[1:]:
        u |= r
    band = fitz.Rect(page_rect.x0 + 2, u.y0 - PAD_Y, page_rect.x1 - 2, u.y1 + PAD_Y)
    if band.height > MAX_BAND_H:
        band.y1 = band.y0 + MAX_BAND_H
    return band & page_rect


def _render(page, band: fitz.Rect, boxes, colour, *, fill=None, gap_y=None) -> bytes:
    """Render `band` of `page` to PNG with `boxes` outlined in `colour`.

    Drawing happens on a scratch copy of the page so the source PDF on disk is
    never touched.
    """
    scratch = fitz.open()
    scratch.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
    sp = scratch[0]
    for b in boxes:
        if fill is not None:
            sp.draw_rect(b, color=None, fill=fill, fill_opacity=0.35, overlay=True)
        sp.draw_rect(b, color=colour, width=1.4, overlay=True)
    if gap_y is not None:
        sp.draw_line(fitz.Point(band.x0 + 6, gap_y), fitz.Point(band.x1 - 6, gap_y),
                     color=colour, width=1.8, dashes="[3 2] 0", overlay=True)
    png = sp.get_pixmap(matrix=fitz.Matrix(ZOOM, ZOOM), clip=band).tobytes("png")
    scratch.close()
    return png


def _locate(doc, needle: list, pages):
    """First (page_words, start, end) where `needle` matches across `pages`."""
    for pno in pages:
        if not 1 <= pno <= doc.page_count:
            continue
        pw = _page_words(doc, pno)
        hit = _find_run(pw.toks, needle)
        if hit:
            return pw, hit[0], hit[1]
    return None, None, None


def _stage_anchor(sdoc, before: list, after: list):
    """Find the surviving context around the gap on the STAGE side.

    Prefers the words that preceded the missing fragment in PROD; falls back to
    the words that followed it.  Returns (page_words, rects, gap_y, which).
    """
    for label, ctx, lead in (("before", before, False), ("after", after, True)):
        if len(ctx) < 3:
            continue
        probe = ctx[-8:] if not lead else ctx[:8]
        for pno in range(1, sdoc.page_count + 1):
            pw = _page_words(sdoc, pno)
            hit = _find_run(pw.toks, probe if not lead else probe)
            if not hit:
                continue
            rects = pw.rects[hit[0]:hit[1]]
            gap = (max(r.y1 for r in rects) + 6) if not lead else (min(r.y0 for r in rects) - 6)
            return pw, rects, gap, label
    return None, None, None, None


def build_issue_shot(prod_path: str, stage_path: str | None, fragment: str,
                     section_page: int, section_title: str = "",
                     search_span: int = 6) -> dict | None:
    """Build the PROD/STAGE crop pair + comment for one missing fragment.

    `section_page` is the 1-based PROD page the section starts on; the fragment
    is searched from there over `search_span` pages.  Returns None when the
    fragment cannot be located in PROD at all (nothing meaningful to show).
    """
    needle = [t for t in (_norm(w) for w in (fragment or "").split()) if t]
    if len(needle) < 3:
        return None

    pdoc = fitz.open(prod_path)
    try:
        pages = range(max(1, section_page), max(1, section_page) + search_span)
        pw, i, j = _locate(pdoc, needle, pages)
        if pw is None:                       # widen to the whole document once
            pw, i, j = _locate(pdoc, needle, range(1, pdoc.page_count + 1))
        if pw is None:
            return None

        hit_rects = pw.rects[i:j]
        ppage = pdoc[pw.page_no - 1]
        prod_png = _render(ppage, _band(hit_rects, ppage.rect),
                           _merge_lines(hit_rects), RED, fill=RED_FILL)
        before, after = pw.toks[max(0, i - 12):i], pw.toks[j:j + 12]
        matched, total = j - i, len(needle)
    finally:
        pdoc.close()

    shot = {
        "fragment": fragment,
        "prod_page": pw.page_no,
        "prod_png": prod_png,
        "stage_page": None,
        "stage_png": None,
        "comment": "",
    }

    partial = ("" if matched >= total else
               f" (first {matched} of {total} words shown — the fragment "
               f"continues past this page)")

    if not stage_path:
        shot["comment"] = (f"Present in PROD p.{pw.page_no}, highlighted in red."
                           f" No STAGE file to compare against.{partial}")
        return shot

    sdoc = fitz.open(stage_path)
    try:
        spw, srects, gap_y, which = _stage_anchor(sdoc, before, after)
        if spw is None:
            shot["comment"] = (
                f"Missing from STAGE. Highlighted in red on PROD p.{pw.page_no}."
                f" The surrounding text is absent from STAGE too, so the whole"
                f" passage — not just this fragment — appears to have been"
                f" dropped from “{section_title or 'this section'}”.{partial}")
            return shot

        spage = sdoc[spw.page_no - 1]
        band = _band(srects, spage.rect)
        band.y0 = min(band.y0, gap_y - PAD_Y)
        band.y1 = max(band.y1, gap_y + PAD_Y)
        band = band & spage.rect
        shot["stage_page"] = spw.page_no
        shot["stage_png"] = _render(spage, band, _merge_lines(srects), AMBER, gap_y=gap_y)
        where = ("directly after" if which == "before" else "directly before")
        shot["comment"] = (
            f"Missing from STAGE. The text is highlighted in red on PROD "
            f"p.{pw.page_no}; on STAGE p.{spw.page_no} the surviving context is "
            f"boxed in amber and the dashed line marks where the content should "
            f"appear — {where} it. STAGE jumps straight past it.{partial}")
    finally:
        sdoc.close()
    return shot


def build_section_shots(prod_path: str, stage_path: str | None, fragments,
                        section_page: int, section_title: str = "",
                        limit: int = 5) -> list:
    """Screenshot pairs for a section's missing fragments, longest first.

    Longest first because a long fragment is the substantive omission; short
    ones are usually the same gap seen through a different window.
    """
    ranked = sorted((f for f in fragments if f), key=lambda f: -len(f.split()))
    shots = []
    for frag in ranked:
        if len(shots) >= limit:
            break
        shot = build_issue_shot(prod_path, stage_path, frag,
                                section_page, section_title)
        if shot:
            shots.append(shot)
    return shots

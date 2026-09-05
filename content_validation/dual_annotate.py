"""Produce the report as the STAGE PDF itself, marked up.

Rather than a separate document describing what is wrong, this writes the
findings INTO a copy of the STAGE PDF:

    * each issue is boxed in red on the page where it belongs, with a popup
      comment carrying the full description — the same review workflow Acrobat
      gives a human reviewer;
    * an index is prepended listing every issue, and each row is a live link that
      jumps straight to the marked page.

Where a finding is text PROD has and STAGE does not, there is nothing in STAGE to
box. Those are anchored on the STAGE heading for the topic instead, so the note
lands where the missing text belongs.
"""
from __future__ import annotations

import os
import re

import fitz

from . import dual_extract as DX

RED    = (0.85, 0.10, 0.10)
ORANGE = (0.90, 0.45, 0.05)
BLUE   = (0.10, 0.35, 0.85)
GREY   = (0.42, 0.47, 0.50)

# Index page geometry (points, US Letter portrait)
_PAGE_W, _PAGE_H = 612, 792
_M = 46


def _is_contents_page(page) -> bool:
    """True for a page that lists headings against page numbers.

    Contents and Q&A index pages repeat the wording of real headings, so
    anchoring on them puts a mark next to a table-of-contents entry instead of
    the content it names. Detected from the layout — a page number sitting at
    the end of most lines — because such a page is not always in the outline.
    """
    text = page.get_text()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 6:
        return False
    # Two shapes of contents page: the number trails the heading on the same
    # line, or — as here — it is emitted as a line of its own next to it.
    trailing = sum(1 for l in lines if re.search(r"\s\d{1,3}$", l))
    bare = sum(1 for l in lines if re.fullmatch(r"\d{1,3}", l))
    if trailing >= len(lines) * 0.5:
        return True
    if bare >= max(4, len(lines) * 0.25):
        return True
    return len(re.findall(r"\.{4,}", text)) >= 5


def _locate(doc, needle: str, hint: int = 0, skip=None):
    """(page_index, [rects]) for `needle`, preferring pages near `hint`.

    `skip` holds the contents/index pages. A heading's first occurrence in a
    manual is its table-of-contents entry, and marking that would put every note
    on the contents page instead of beside the content it is about.
    """
    want = DX.norm_words(needle)
    if not want:
        return -1, []
    skip = {p - 1 for p in (skip or ())}
    for p in range(doc.page_count):
        if p not in skip and _is_contents_page(doc[p]):
            skip.add(p)
    order = [p for p in range(doc.page_count) if p not in skip]
    if hint and 1 <= hint <= doc.page_count:
        near = [p for p in order if abs(p + 1 - hint) <= 3]
        order = near + [p for p in order if p not in near]
    for pno in order:
        toks, rects = [], []
        for w in doc[pno].get_text("words"):
            for t in DX.norm_words(w[4]):
                toks.append(t)
                rects.append(fitz.Rect(w[0], w[1], w[2], w[3]))
        for i in (i for i, t in enumerate(toks) if t == want[0]):
            if toks[i:i + len(want)] == want:
                return pno, rects[i:i + len(want)]
    return -1, []


def _colour_for(kind: str):
    if kind.startswith("Content"):
        return RED
    if kind.startswith(("Table", "List", "Text alignment")):
        return ORANGE
    if kind.startswith(("Image", "Emphasis", "Italic")):
        return BLUE
    return GREY


def annotate(stage_path: str, findings, out_path: str, prod_name: str = "",
             stage_name: str = "", nav_pages=None) -> dict:
    """Write an annotated copy of STAGE. Returns a small summary dict."""
    doc = fitz.open(stage_path)
    n_pages_before = doc.page_count
    placed = []            # (finding, page_index_in_original, anchor_rect)

    for f in findings:
        hint = f.page if 0 < f.page <= n_pages_before else 0
        pno, rects = (-1, [])
        if f.evidence:
            pno, rects = _locate(doc, f.evidence, hint, nav_pages)
        if pno < 0:
            # Nothing to box — the text is PROD's. Put the note on the STAGE
            # heading for the topic, which is where the missing text belongs.
            topic = re.sub(r"^(Table|Figure|Diagram|Text|List item)s?\s+",
                           "", f.topic or "")
            topic = re.sub(r"[“”\"]", "", topic).strip(" —-")
            if topic:
                pno, rects = _locate(doc, topic, hint, nav_pages)
        if pno < 0:
            # Neither the text nor its heading could be found in STAGE. The page
            # number on the finding is PROD's, and using it here would drop the
            # mark on whatever STAGE happens to have at that sheet — which is how
            # notes ended up on contents pages. Better to leave it in the index
            # only than to point at the wrong place.
            continue

        page = doc[pno]
        colour = _colour_for(f.kind)
        if rects:
            box = fitz.Rect(rects[0])
            for r in rects[1:]:
                box |= fitz.Rect(r)
            box = fitz.Rect(box.x0 - 2, box.y0 - 2, box.x1 + 2, box.y1 + 2)
        else:
            # No anchor: mark the top of the page so the note is still findable.
            box = fitz.Rect(page.rect.x0 + 36, page.rect.y0 + 36,
                            page.rect.x1 - 36, page.rect.y0 + 62)

        body = f"{f.kind}\n\n{f.detail}"
        if prod_name:
            body += f"\n\nReference: PROD ({prod_name})"

        # The mark is drawn into the page, not left to an annotation's
        # appearance stream: viewers render annotation borders inconsistently and
        # some hide them entirely, which is why the boxes were not showing.
        # Drawn content is part of the page and always visible.
        page.draw_rect(box, color=colour, width=1.6)
        tag = fitz.Rect(box.x0 - 1, box.y0 - 13, box.x0 + 21, box.y0 - 1)
        page.draw_rect(tag, color=colour, fill=colour, width=0)
        page.insert_text((tag.x0 + 3, tag.y1 - 3), f"{len(placed) + 1}",
                         fontsize=8, fontname="hebo", color=(1, 1, 1))

        # ONE annotation carries the comment. Two overlapping annotations meant
        # closing a popup simply revealed the one underneath it.
        note = page.add_text_annot(fitz.Point(box.x1 + 4, box.y0 - 2), body,
                                   icon="Comment")
        note.set_info(title=f"{f.kind} ({f.lane})", content=body)
        note.set_colors(stroke=colour)
        note.set_flags(fitz.PDF_ANNOT_IS_PRINT)     # closed until clicked
        note.update()

        placed.append((f, pno, box))

    # ── index pages, prepended ────────────────────────────────────────────
    per_page = 30
    n_index = max(1, (len(placed) + per_page - 1) // per_page)
    for k in range(n_index):
        doc.new_page(k, width=_PAGE_W, height=_PAGE_H)

    def idx_page(i):
        return doc[i]

    y = _M
    page_i, row_on_page = 0, 0
    p = idx_page(0)
    p.insert_text((_M, y), "Validation issues", fontsize=17,
                  fontname="helv", color=(0.15, 0.18, 0.20))
    y += 20
    p.insert_text((_M, y), f"Reference (PROD): {prod_name or '—'}", fontsize=8.5,
                  fontname="helv", color=GREY)
    y += 12
    p.insert_text((_M, y), f"Under test (STAGE): {stage_name or os.path.basename(stage_path)}",
                  fontsize=8.5, fontname="helv", color=GREY)
    y += 14
    p.insert_text((_M, y), f"{len(placed)} issue(s) marked in this document. "
                           f"Click a row to jump to it; each mark carries a comment.",
                  fontsize=8.5, fontname="helv", color=GREY)
    y += 18

    for n, (f, pno, box) in enumerate(placed, 1):
        if row_on_page >= per_page:
            page_i += 1
            if page_i >= n_index:
                break
            p = idx_page(page_i)
            y, row_on_page = _M, 0
            p.insert_text((_M, y), "Validation issues (continued)", fontsize=13,
                          fontname="helv", color=(0.15, 0.18, 0.20))
            y += 20

        target = pno + n_index          # page index after the inserted pages
        colour = _colour_for(f.kind)
        row = fitz.Rect(_M, y - 9, _PAGE_W - _M, y + 6)

        p.draw_rect(fitz.Rect(_M, y - 6, _M + 7, y + 1), color=colour,
                    fill=colour, width=0)
        p.insert_text((_M + 13, y), f"{n}. {f.kind}", fontsize=9,
                      fontname="hebo", color=(0.15, 0.18, 0.20))
        p.insert_text((_M + 168, y), f"page {target + 1}", fontsize=8.5,
                      fontname="helv", color=BLUE)
        detail = re.sub(r"\s+", " ", f.detail)
        p.insert_text((_M + 216, y), detail[:96], fontsize=8,
                      fontname="helv", color=(0.30, 0.33, 0.35))
        p.insert_link({"kind": fitz.LINK_GOTO, "from": row, "page": target,
                       "to": fitz.Point(box.x0, max(0, box.y0 - 60))})
        y += 15
        row_on_page += 1

    doc.set_metadata({"title": "Validation — annotated STAGE",
                      "subject": f"{len(placed)} issues marked"})
    doc.save(out_path, garbage=3, deflate=True)
    doc.close()
    return {"marked": len(placed), "index_pages": n_index,
            "skipped": len(findings) - len(placed)}

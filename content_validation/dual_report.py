"""PDF report for the Content + Visual validation.

Metrics, then one row per finding, then a screenshot per finding taken from the
page that carries it. Nothing is searched for twice: every finding arrives with
the text to box, so a shot is either produced from a verified location or not at
all.
"""
from __future__ import annotations

import io
import os
import re

import fitz
from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, PageBreak, Image as RLImage)
from reportlab.platypus.flowables import HRFlowable

from . import dual_extract as DX

SHOT_ZOOM = 2.2
SHOT_PAD = 40
BOX = (0.85, 0.10, 0.10)


def _esc(t):
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _trunc(t, n=300):
    t = re.sub(r"\s+", " ", t or "").strip()
    return t if len(t) <= n else t[:n - 1] + "…"


def _locate(pdf_path, needle, hint=0):
    """(page, [rects]) where `needle` reads on the page, else (0, [])."""
    want = DX.norm_words(needle)
    if not want:
        return 0, []
    doc = fitz.open(pdf_path)
    order = list(range(doc.page_count))
    if hint and 1 <= hint <= doc.page_count:
        near = [p for p in order if abs(p + 1 - hint) <= 3]
        order = near + [p for p in order if p not in near]
    try:
        for pno in order:
            words = doc[pno].get_text("words")
            toks, rects = [], []
            for w in words:
                for t in DX.norm_words(w[4]):
                    toks.append(t)
                    rects.append(fitz.Rect(w[0], w[1], w[2], w[3]))
            for s in (i for i, t in enumerate(toks) if t == want[0]):
                if toks[s:s + len(want)] == want:
                    return pno + 1, rects[s:s + len(want)]
    finally:
        doc.close()
    return 0, []


def _shot(pdf_path, page_no, rects):
    if not page_no:
        return None
    try:
        doc = fitz.open(pdf_path)
        page = doc[page_no - 1]
        clip = None
        if rects:
            box = fitz.Rect(rects[0])
            for r in rects[1:]:
                box |= fitz.Rect(r)
            for r in rects:
                page.draw_rect(fitz.Rect(r), color=BOX, width=1.4)
            clip = fitz.Rect(page.rect.x0, box.y0 - SHOT_PAD,
                             page.rect.x1, box.y1 + SHOT_PAD) & page.rect
        png = page.get_pixmap(matrix=fitz.Matrix(SHOT_ZOOM, SHOT_ZOOM),
                              clip=clip).tobytes("png")
        doc.close()
        return png
    except Exception:
        return None


def _img(png, max_w=690.0, max_h=190.0):
    if not png:
        return None
    try:
        im = RLImage(io.BytesIO(png))
        sc = min(max_w / im.imageWidth, max_h / im.imageHeight, 1.0)
        im.drawWidth, im.drawHeight = im.imageWidth * sc, im.imageHeight * sc
        return im
    except Exception:
        return None


def build(prod_path, stage_path, findings, met, out_path, shots=True):
    st = getSampleStyleSheet()
    doc = SimpleDocTemplate(out_path, pagesize=landscape(letter),
                            leftMargin=0.4 * inch, rightMargin=0.4 * inch,
                            topMargin=0.4 * inch, bottomMargin=0.4 * inch,
                            title="Content + Visual Validation")
    title = ParagraphStyle("T", parent=st["Heading1"], fontSize=16, spaceAfter=4)
    sub = ParagraphStyle("S", parent=st["Normal"], fontSize=10,
                         textColor=colors.grey, spaceAfter=2)
    head = ParagraphStyle("H", parent=st["Heading2"], fontSize=13,
                          spaceBefore=10, spaceAfter=6)
    hdr = ParagraphStyle("Hd", parent=st["Normal"], fontSize=9, leading=12,
                         textColor=colors.white)
    cell = ParagraphStyle("C", parent=st["Normal"], fontSize=8, leading=11)
    topic = ParagraphStyle("Tp", parent=st["Normal"], fontSize=8, leading=11,
                           fontName="Helvetica-Bold")
    bad = ParagraphStyle("B", parent=st["Normal"], fontSize=8, leading=11,
                         textColor=colors.HexColor("#b71c1c"),
                         fontName="Helvetica-Bold")

    def grade(p):
        return "#2e7d32" if p >= 99 else "#e65100" if p >= 95 else "#b71c1c"

    story = [Paragraph("Content + Visual Validation", title),
             Paragraph(f"Production (reference): {os.path.basename(prod_path)}", sub),
             Paragraph(f"Staging (under test):   {os.path.basename(stage_path)}", sub),
             Spacer(1, 10), Paragraph("Metrics", head)]

    m = [[Paragraph(f"<b>{h}</b>", hdr) for h in
          ["Headings matched", "Topics with no differences", "Text reproduced",
           "Content issues", "Visual issues"]],
         [Paragraph(f"<font color='{grade(met['headings_matched'])}' size='15'>"
                    f"<b>{met['headings_matched']:.1f}%</b></font><br/>"
                    f"<font size='7'>{met['headings'][0]} of {met['headings'][1]}</font>", cell),
          Paragraph(f"<font color='{grade(met['topics_clean'])}' size='15'>"
                    f"<b>{met['topics_clean']:.1f}%</b></font><br/>"
                    f"<font size='7'>{met['topics'][0]} of {met['topics'][1]}</font>", cell),
          Paragraph(f"<font color='{grade(met['text_reproduced'])}' size='15'>"
                    f"<b>{met['text_reproduced']:.1f}%</b></font><br/>"
                    f"<font size='7'>of PROD's words</font>", cell),
          Paragraph(f"<font color='{'#b71c1c' if met['content_issues'] else '#2e7d32'}'"
                    f" size='15'><b>{met['content_issues']}</b></font>", cell),
          Paragraph(f"<font color='{'#b71c1c' if met['visual_issues'] else '#2e7d32'}'"
                    f" size='15'><b>{met['visual_issues']}</b></font>", cell)]]
    mt = Table(m, colWidths=[140, 165, 130, 110, 110])
    mt.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#37474f")),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#f5f7f8")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
    story += [mt, Spacer(1, 12)]

    if not findings:
        story.append(Paragraph("No differences found — STAGE matches PROD.",
                               ParagraphStyle("Ok", parent=st["Normal"],
                                              fontSize=11,
                                              textColor=colors.HexColor("#2e7d32"))))
        doc.build(story)
        return

    story.append(Paragraph("Issues", head))
    rows = [[Paragraph(f"<b>{h}</b>", hdr) for h in
             ["#", "Topic / Location", "Where", "Lane", "Issue", "Detail"]]]
    for n, f in enumerate(findings, 1):
        rows.append([Paragraph(str(n), cell), Paragraph(_esc(f.topic), topic),
                     Paragraph(f"{f.doc} p{f.page}", cell),
                     Paragraph(f.lane, cell), Paragraph(f.kind, bad),
                     Paragraph(_esc(_trunc(f.detail, 320)), cell)])
    it = Table(rows, colWidths=[18, 132, 62, 44, 96, 372], repeatRows=1)
    it.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#37474f")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1),
         [colors.white, colors.HexColor("#f7f7f7")])]))
    story.append(it)

    if shots:
        ev, cap = [], ParagraphStyle("Cp", parent=st["Normal"], fontSize=8.5,
                                     leading=11.5, spaceAfter=2)
        lab = ParagraphStyle("Lb", parent=st["Normal"], fontSize=7.5,
                             textColor=colors.HexColor("#37474f"))
        for n, f in enumerate(findings, 1):
            if not f.evidence:
                continue
            src = prod_path if f.doc == "PROD" else stage_path
            pg, rects = _locate(src, f.evidence, f.page)
            if not pg:
                # the words are PROD's; show them where they do exist
                src = prod_path
                pg, rects = _locate(prod_path, f.evidence, f.page)
            img = _img(_shot(src, pg, rects))
            if img is None:
                continue
            side = "PROD" if src == prod_path else "STAGE"
            ev += [Paragraph(f"<b>{n}. {f.kind}</b> — {_esc(_trunc(f.detail, 190))}", cap),
                   Paragraph(f"<b>{side}</b> page {pg} — boxed in red", lab),
                   img, Spacer(1, 6),
                   HRFlowable(width="100%", thickness=0.6,
                              color=colors.HexColor("#cfd8dc"),
                              spaceBefore=2, spaceAfter=10)]
        if ev:
            story += [PageBreak(), Paragraph("Evidence", head)] + ev

    doc.build(story)

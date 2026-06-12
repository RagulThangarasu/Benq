#!/usr/bin/env python3
"""Comprehensive STAGE-vs-PROD style validation.

PROD is the reference ("expected"); STAGE is what's checked ("actual").
Nine style dimensions are validated and collected into one clean PDF report:

  1. Image position        — left / centre / right alignment per image
  2. Space above image     — vertical gap between an image and the text above it
  3. Heading style         — heading colour & relative size
  4. Paragraph spacing     — gap above and below body paragraphs
  5. Text colour           — body-text colour (should stay PROD's colour)
  6. Info / notice colour  — colour of NOTE / TIP / IMPORTANT callouts
  7. Table layout breaking — tables that split across a page boundary
  8. Hyperlink issues      — missing / broken / re-targeted links
  9. Underline removal      — text underlines in STAGE that should be removed

Built on PyMuPDF (open-source). Run:

    python style_validation.py <prod.pdf> <stage.pdf> [out_report.pdf]

Default output: <project-root>/reports/style_validation_report.pdf
"""
import os
import re
import statistics
import sys

import fitz  # PyMuPDF

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)

from .validate_toc_content import (
    get_toc, _norm_key, _toc_ranges_by_key, _doc_body_size, _style_class_rel,
)

NOTICE_RE = re.compile(r"\b(NOTE|TIP|IMPORTANT|WARNING|CAUTION|INFO)\b", re.IGNORECASE)

# ── colour helpers ──────────────────────────────────────────────────────────
def _rgb(c):
    c = int(c or 0)
    return ((c >> 16) & 255, (c >> 8) & 255, c & 255)

def _hex(c):
    return "#%06x" % int(c or 0)

def _cdist(a, b):
    ra, rb = _rgb(a), _rgb(b)
    return sum((x - y) ** 2 for x, y in zip(ra, rb)) ** 0.5

COLOR_TOL = 28          # RGB euclidean distance below which two colours are "the same"
WHITE = 0xFFFFFF

def _dominant_color(weighted):
    """weighted: dict color->weight. Returns most-weighted non-white colour."""
    best, bw = None, 0
    for c, w in weighted.items():
        if c == WHITE:
            continue
        if w > bw:
            best, bw = c, w
    return best


# ── feature extraction ──────────────────────────────────────────────────────
# Each page is parsed exactly once (get_text / get_drawings / find_tables are
# the expensive calls) into a per-page feature record. Whole-document and
# per-section views are then assembled by slicing that cache, instead of
# re-parsing the same pages for every section.
_FEAT_KEYS = ("lines", "images", "links", "hstrokes", "tables")


def _extract_page(page, pno, body_size):
    """Parse one page's geometry, colour, link, drawing and table features."""
    pw, ph = page.rect.width, page.rect.height
    lines, images, links, hstrokes, tables, fills = [], [], [], [], [], []

    for b in page.get_text("dict").get("blocks", []):
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            spans = ln.get("spans", [])
            txt = "".join(s.get("text", "") for s in spans).strip()
            if not txt:
                continue
            mx = max((s.get("size", 0.0) for s in spans), default=0.0)
            cw = {}
            for s in spans:
                cw[s.get("color", 0)] = cw.get(s.get("color", 0), 0) + len(s.get("text", ""))
            color = max(cw.items(), key=lambda kv: kv[1])[0] if cw else 0
            lines.append({
                "page": pno, "text": txt, "size": mx, "color": color,
                "cls": _style_class_rel(txt, mx, body_size),
                "rect": fitz.Rect(ln["bbox"]), "pw": pw, "ph": ph,
            })
    for info in page.get_image_info():
        bb = info.get("bbox")
        if bb:
            images.append({"page": pno, "rect": fitz.Rect(bb), "pw": pw, "ph": ph})
    for l in page.get_links():
        links.append({"page": pno, "kind": l.get("kind"), "uri": l.get("uri"),
                      "to_page": (l.get("page", -1) + 1) if l.get("kind") == fitz.LINK_GOTO else None,
                      "rect": fitz.Rect(l.get("from"))})
    # single get_drawings() pass yields both underline strokes and bg fills
    for dr in page.get_drawings():
        fc, r = _fill_to_int(dr.get("fill")), dr.get("rect")
        if fc is not None and r is not None and r.width > 100 and r.height > 14:
            fills.append((r, fc))
        for it in dr.get("items", []):
            if it[0] == "l":
                a, b2 = it[1], it[2]
                if abs(a.y - b2.y) < 0.7 and abs(b2.x - a.x) > 12:
                    hstrokes.append({"page": pno, "y": (a.y + b2.y) / 2,
                                     "x0": min(a.x, b2.x), "x1": max(a.x, b2.x)})
            elif it[0] == "re":
                r2 = it[1]
                if r2.height < 1.3 and r2.width > 12:
                    hstrokes.append({"page": pno, "y": r2.y0 + r2.height / 2,
                                     "x0": r2.x0, "x1": r2.x1})
    try:
        for t in page.find_tables().tables:
            cells = [fitz.Rect(c) for c in (t.cells or []) if c]
            tables.append({"page": pno, "rect": fitz.Rect(t.bbox),
                           "cols": len(t.header.cells) if t.header else len(t.cols),
                           "cells": cells, "pw": pw, "ph": ph})
    except Exception:
        pass
    return {"lines": lines, "images": images, "links": links, "hstrokes": hstrokes,
            "tables": tables, "fills": fills, "pw": pw, "ph": ph}


def _build_cache(doc, body_size):
    """Parse every page once → {page_no: feature record}."""
    return {pno: _extract_page(doc[pno - 1], pno, body_size)
            for pno in range(1, doc.page_count + 1)}


def _assemble(cache, start, end):
    """Merge cached per-page features for a page range into one feature dict."""
    out = {k: [] for k in _FEAT_KEYS}
    for pno in range(start, end + 1):
        pc = cache.get(pno)
        if not pc:
            continue
        for k in _FEAT_KEYS:
            out[k].extend(pc[k])
    return out


def _align(rect, pw):
    cx = (rect.x0 + rect.x1) / 2 / pw
    if cx < 0.42:
        return "left"
    if cx > 0.58:
        return "right"
    return "center"


def _median(vals):
    return round(float(statistics.median(vals)), 1) if vals else None


def _space_above_images(feat):
    """List of (image_rect, gap_above) using nearest text line above with x-overlap."""
    out = []
    for im in feat["images"]:
        best = None
        for ln in feat["lines"]:
            if ln["page"] != im["page"]:
                continue
            r = ln["rect"]
            if r.y1 <= im["rect"].y0 and not (r.x1 < im["rect"].x0 or r.x0 > im["rect"].x1):
                if best is None or r.y1 > best:
                    best = r.y1
        if best is not None:
            out.append((im, round(im["rect"].y0 - best, 1)))
    return out


def _paragraph_gaps(feat):
    """Vertical gaps between consecutive body lines that exceed normal leading
    (i.e. paragraph breaks)."""
    body = sorted([l for l in feat["lines"] if l["cls"] == "body"],
                  key=lambda l: (l["page"], l["rect"].y0))
    raw = []
    for i in range(1, len(body)):
        a, b = body[i - 1], body[i]
        if a["page"] != b["page"]:
            continue
        raw.append(max(0.0, b["rect"].y0 - a["rect"].y1))
    if not raw:
        return []
    line_lead = statistics.median(raw)
    return [g for g in raw if g > line_lead * 1.6]


def _wrapped_cell_padding_stats(feat):
    """Measure alignment of wrapped (2+ line) table-cell text.

    Reports two robust metrics per section:
      • left_pad      — median gap from the cell's left edge to where the text
                        starts (only for cells whose text is clearly left-anchored,
                        so mis-detected/centred cells are ignored).
      • indent_drift  — median (max-min) of the wrapped lines' left edges within a
                        cell. This is intrinsic to the cell (the cell's own x0
                        cancels out), so it is unaffected by how find_tables places
                        the cell border — the reliable "do wrapped lines align" signal.

    Right-edge padding is deliberately NOT measured: left-aligned text is ragged
    on the right, so that distance reflects line length, not a style property.
    """
    left_pads, indent_drifts = [], []
    bad_left_align = 0
    lines_by_page = {}
    for l in feat["lines"]:
        lines_by_page.setdefault(l["page"], []).append(l)

    for tbl in feat["tables"]:
        pg = tbl["page"]
        for cell in tbl.get("cells") or []:
            if (cell.x1 - cell.x0) < 20:
                continue
            cell_lines = []
            for ln in lines_by_page.get(pg, []):
                r = ln["rect"]
                cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
                if cell.x0 <= cx <= cell.x1 and cell.y0 <= cy <= cell.y1 and len(ln["text"]) >= 2:
                    cell_lines.append(ln)
            if len(cell_lines) < 2:
                continue
            lp = [max(0.0, round(ln["rect"].x0 - cell.x0, 1)) for ln in cell_lines]
            # skip cells whose text is not left-anchored (centred / mis-detected):
            # those produce meaningless 50-150pt "padding".
            if min(lp) > 30:
                continue
            base_left = round(min(lp), 1)
            drift = round(max(lp) - min(lp), 1)
            left_pads.append(base_left)                  # the cell's base left padding
            indent_drifts.append(drift)
            # A wrapped cell is bad only when its own wrapped lines stop sharing
            # the same left start position.
            if drift > 3.0:
                bad_left_align += 1

    return {
        "wrapped_cells": len(indent_drifts),
        "left_pad": _median(left_pads),
        "indent_drift": _median(indent_drifts),
        "bad_left_align": bad_left_align,
    }


# ── checks (each returns a list of finding dicts) ───────────────────────────
def _f(cat, sev, topic, pages, expected, actual, issue, fix):
    return {"category": cat, "severity": sev, "topic": topic, "pages": pages,
            "expected": expected, "actual": actual, "issue": issue, "fix": fix}


def check_heading_color(p_all, s_all, findings):
    pw = {}
    for l in p_all["lines"]:
        if l["cls"] == "heading":
            pw[l["color"]] = pw.get(l["color"], 0) + len(l["text"])
    sw = {}
    for l in s_all["lines"]:
        if l["cls"] == "heading":
            sw[l["color"]] = sw.get(l["color"], 0) + len(l["text"])
    pc, sc = _dominant_color(pw), _dominant_color(sw)
    if pc is not None and sc is not None and _cdist(pc, sc) > COLOR_TOL:
        pages = ", ".join(str(p) for p in sorted({l["page"] for l in s_all["lines"]
                          if l["cls"] == "heading" and _cdist(l["color"], sc) <= COLOR_TOL})[:8])
        findings.append(_f(
            "Heading style", "High", "All headings", pages,
            f"Heading colour {_hex(pc)}", f"Heading colour {_hex(sc)}",
            f"Section headings use {_hex(sc)} in STAGE but {_hex(pc)} in PROD.",
            f"Recolour headings to {_hex(pc)} to match PROD brand colour."))


def check_text_color(p_all, s_all, sections, findings):
    # document-level body colour
    def dom_body(feat):
        w = {}
        for l in feat["lines"]:
            if l["cls"] == "body":
                w[l["color"]] = w.get(l["color"], 0) + len(l["text"])
        return _dominant_color(w)
    pc, sc = dom_body(p_all), dom_body(s_all)
    if pc is not None and sc is not None and _cdist(pc, sc) > COLOR_TOL:
        findings.append(_f(
            "Text colour", "High", "Body text (document-wide)", "—",
            f"Body text {_hex(pc)}", f"Body text {_hex(sc)}",
            f"Body copy is {_hex(sc)} in STAGE vs {_hex(pc)} in PROD.",
            f"Set body text to {_hex(pc)}."))
    # per-section: body lines whose colour is far from PROD body colour.
    # Lines that sit under a hyperlink are intentionally coloured (cross-refs /
    # URLs) — they are not a body-text-colour defect, so skip them.
    ref = pc if pc is not None else 0
    for title, _p, s_feat in sections:
        link_rects = {}
        for lk in s_feat["links"]:
            link_rects.setdefault(lk["page"], []).append(lk["rect"])
        bad = {}
        for l in s_feat["lines"]:
            if l["cls"] == "body" and l["color"] != WHITE and _cdist(l["color"], ref) > COLOR_TOL:
                if any(r.intersects(l["rect"]) for r in link_rects.get(l["page"], [])):
                    continue
                bad.setdefault(l["color"], []).append(l["page"])
        for col, pgs in bad.items():
            if len(pgs) >= 2:   # ignore one-off coloured words
                findings.append(_f(
                    "Text colour", "Medium", title,
                    ", ".join(str(p) for p in sorted(set(pgs))[:6]),
                    f"Body text {_hex(ref)}", f"Body text {_hex(col)}",
                    f"{len(pgs)} body line(s) are {_hex(col)} instead of {_hex(ref)}.",
                    f"Recolour to {_hex(ref)}."))


_NOTICE_TYPES = [
    (re.compile(r"\bIMPORTANT\b", re.I), "IMPORTANT"),
    (re.compile(r"\bWARNING\b", re.I),   "WARNING"),
    (re.compile(r"\bCAUTION\b", re.I),   "CAUTION"),
    (re.compile(r"\bNOTE\b", re.I),      "NOTE"),
    (re.compile(r"\bTIP\b", re.I),       "TIP"),
    (re.compile(r"\bINFO\b", re.I),      "INFO"),
]


def _notice_type(t):
    for rx, name in _NOTICE_TYPES:
        if rx.search(t):
            return name
    return None


def _is_chromatic(c, thresh=25):
    """True if a colour is actually coloured (not black/white/grey)."""
    if c is None or c < 0:
        return False
    r, g, b = _rgb(c)
    return (max(r, g, b) - min(r, g, b)) > thresh


def _fill_to_int(f):
    if f is None:
        return None
    if isinstance(f, int):
        return f
    try:
        r, g, b = f[:3]
        return (int(r * 255) << 16) | (int(g * 255) << 8) | int(b * 255)
    except Exception:
        return None


def _mode(counter):
    return max(counter.items(), key=lambda kv: kv[1])[0] if counter else None


def _region_colored(page, rect, frac=0.05, sat=45):
    """Render a small region and report whether it contains coloured pixels
    (used to tell a coloured info icon from a black/greyscale one)."""
    rect = rect & page.rect
    if rect.is_empty or rect.width < 2 or rect.height < 2:
        return False
    try:
        pix = page.get_pixmap(clip=rect, matrix=fitz.Matrix(2, 2), alpha=False)
    except Exception:
        return False
    data, comp = pix.samples, pix.n
    if comp < 3 or not data:
        return False
    npx = len(data) // comp
    step = max(1, npx // 500)
    colored = total = 0
    for i in range(0, len(data) - comp + 1, comp * step):
        r, g, b = data[i], data[i + 1], data[i + 2]
        total += 1
        if (max(r, g, b) - min(r, g, b)) > sat:
            colored += 1
    return total > 0 and (colored / total) > frac


def analyze_info_callouts(doc, cache):
    """Per callout type, gather text colour, themed background and icon colour.

    Reuses the per-page feature cache (lines / fills / images); only the icon
    pixmap render still needs the live page. Returns {TYPE: {text:Counter,
    bg:Counter(-1=none), icon_color/icon_gray/icon_missing counts, pages, n}}.
    """
    from collections import Counter
    out = {}
    for pno, pc in cache.items():
        labels = [(_notice_type(ln["text"]), ln["rect"], ln["color"])
                  for ln in pc["lines"]
                  if len(ln["text"]) <= 40 and _notice_type(ln["text"])]
        if not labels:
            continue
        fills = pc["fills"]
        imgs = [im["rect"] for im in pc["images"]]
        page = None
        for typ, lr, col in labels:
            rec = out.setdefault(typ, {"text": Counter(), "bg": Counter(),
                                       "icon_color": 0, "icon_gray": 0,
                                       "icon_missing": 0, "pages": set(), "n": 0})
            rec["n"] += 1
            rec["pages"].add(pno)
            rec["text"][col] += 1
            # themed background: a tinted fill enclosing the label
            bg = -1
            for r, fc in fills:
                if (r.x0 - 4 <= lr.x0 and r.x1 + 4 >= lr.x1
                        and r.y0 - 6 <= lr.y0 and r.y1 + 2 >= lr.y0
                        and _cdist(fc, WHITE) > 10):
                    bg = fc
                    break
            rec["bg"][bg] += 1
            # icon: image to the left of the label
            icon = [ir for ir in imgs if ir.x1 <= lr.x0 + 10 and lr.x0 - ir.x1 < 55
                    and abs((ir.y0 + ir.y1) / 2 - (lr.y0 + lr.y1) / 2) < 22]
            if not icon:
                rec["icon_missing"] += 1
            else:
                if page is None:           # render only when a page has icons
                    page = doc[pno - 1]
                if _region_colored(page, icon[0]):
                    rec["icon_color"] += 1
                else:
                    rec["icon_gray"] += 1
    return out


def check_info_callouts(p_info, s_info, findings):
    """Validate info-callout styling: coloured icon, coloured text, themed bg."""
    for typ in sorted(set(s_info)):
        s = s_info[typ]
        p = p_info.get(typ)
        pages = ", ".join(str(x) for x in sorted(s["pages"])[:8])

        s_text = _mode(s["text"])
        s_bg = _mode(s["bg"])
        icon_tot = s["icon_color"] + s["icon_gray"] + s["icon_missing"]
        icon_colored = icon_tot and s["icon_color"] >= max(s["icon_gray"], s["icon_missing"])
        icon_missing = icon_tot and s["icon_missing"] >= max(s["icon_color"], s["icon_gray"])
        themed_bg = s_bg is not None and s_bg != -1
        text_colored = _is_chromatic(s_text)

        styled = icon_colored or themed_bg or text_colored
        if not styled:
            findings.append(_f(
                "Info / notice colour", "Medium", f"{typ} callout", pages,
                "Coloured icon + coloured text (theme background optional)",
                "No colour: plain icon, plain text, no background",
                f"{typ} callouts have no colour at all — icon, text and background are all plain.",
                "Add a coloured info icon and colour the label text (and a themed background)."))
            continue

        # icon should be coloured
        if icon_missing:
            findings.append(_f(
                "Info / notice colour", "Medium", f"{typ} icon", pages,
                "Coloured info icon", "No icon",
                f"{typ} callouts have no info icon.",
                "Add a coloured info icon matching the theme."))
        elif icon_tot and s["icon_gray"] > s["icon_color"]:
            findings.append(_f(
                "Info / notice colour", "Low", f"{typ} icon", pages,
                "Coloured info icon", "Greyscale / uncoloured icon",
                f"{typ} icon is not coloured.",
                "Colour the info icon to match the theme."))

        # text should be coloured (theme colour for the callout type)
        p_text = _mode(p["text"]) if p else None
        if not text_colored:
            findings.append(_f(
                "Info / notice colour", "Low", f"{typ} text", pages,
                "Coloured label text" + (f" (PROD {_hex(p_text)})" if _is_chromatic(p_text) else ""),
                f"Label text {_hex(s_text)} (not coloured)",
                f"{typ} label text is {_hex(s_text)}, which is a grey/black, not a theme colour.",
                "Colour the label text to the callout's theme colour."))
        elif p and _is_chromatic(p_text) and _cdist(p_text, s_text) > COLOR_TOL:
            findings.append(_f(
                "Info / notice colour", "Low", f"{typ} text", pages,
                f"Text {_hex(p_text)}", f"Text {_hex(s_text)}",
                f"{typ} label text colour differs from PROD.",
                f"Set {typ} text toward {_hex(p_text)} (or the theme colour)."))

        # themed background (informational — theme dependent)
        p_bg = _mode(p["bg"]) if p else -1
        if themed_bg and p and p_bg not in (None, -1) and _cdist(s_bg, p_bg) > 18:
            findings.append(_f(
                "Info / notice colour", "Low", f"{typ} background", pages,
                f"Background {_hex(p_bg)}", f"Background {_hex(s_bg)}",
                f"{typ} themed background colour differs from PROD.",
                "Confirm the themed background colour is intended."))
        elif not themed_bg and p and p_bg not in (None, -1):
            findings.append(_f(
                "Info / notice colour", "Low", f"{typ} background", pages,
                f"Background {_hex(p_bg)}", "No background",
                f"{typ} has a themed background in PROD but none in STAGE.",
                "Add the themed background colour if the theme calls for it."))


def check_heading_size(sections, findings):
    for title, p_feat, s_feat in sections:
        ph = [l["size"] for l in p_feat["lines"] if l["cls"] == "heading"]
        sh = [l["size"] for l in s_feat["lines"] if l["cls"] == "heading"]
        if ph and sh:
            pm, sm = _median(ph), _median(sh)
            if pm and sm and abs(pm - sm) > 2.5:
                findings.append(_f(
                    "Heading style", "Medium", title,
                    ", ".join(str(p) for p in sorted({l["page"] for l in s_feat["lines"] if l["cls"] == "heading"})[:4]),
                    f"Heading size ~{pm}pt", f"Heading size ~{sm}pt",
                    f"Heading font size differs by {abs(round(sm-pm,1))}pt.",
                    f"Resize headings to ~{pm}pt."))


def check_paragraph_spacing(sections, findings):
    for title, p_feat, s_feat in sections:
        pg, sg = _paragraph_gaps(p_feat), _paragraph_gaps(s_feat)
        if len(pg) < 4 or len(sg) < 4:   # too few paragraphs to judge spacing
            continue
        pm, sm = _median(pg), _median(sg)
        if pm and sm and abs(pm - sm) > 7.0:
            more = "more" if sm > pm else "less"
            findings.append(_f(
                "Paragraph spacing", "Low", title,
                ", ".join(str(p) for p in sorted({l["page"] for l in s_feat["lines"]})[:4]),
                f"~{pm}pt between paragraphs", f"~{sm}pt between paragraphs",
                f"Paragraphs have {more} vertical space than PROD (Δ{abs(round(sm-pm,1))}pt).",
                f"Adjust paragraph spacing toward ~{pm}pt."))


def check_space_above_image(sections, findings):
    for title, p_feat, s_feat in sections:
        pv = [g for _i, g in _space_above_images(p_feat) if 0 <= g < 200]
        sv = [g for _i, g in _space_above_images(s_feat) if 0 <= g < 200]
        pm, sm = _median(pv), _median(sv)
        if pm is not None and sm is not None and abs(pm - sm) > 8.0:
            findings.append(_f(
                "Space above image", "Low", title,
                ", ".join(str(im["page"]) for im in s_feat["images"][:4]),
                f"~{pm}pt above images", f"~{sm}pt above images",
                f"Spacing above images differs by {abs(round(sm-pm,1))}pt.",
                f"Set space above images toward ~{pm}pt."))


def check_image_position(sections, findings):
    for title, p_feat, s_feat in sections:
        pim = sorted(p_feat["images"], key=lambda im: (im["page"], im["rect"].y0))
        sim = sorted(s_feat["images"], key=lambda im: (im["page"], im["rect"].y0))
        # only compare when both sides expose the same number of images
        if not pim or not sim or len(pim) != len(sim):
            continue
        mism = []
        for i, (pi, si) in enumerate(zip(pim, sim)):
            pa, sa = _align(pi["rect"], pi["pw"]), _align(si["rect"], si["pw"])
            pcx = (pi["rect"].x0 + pi["rect"].x1) / 2 / pi["pw"]
            scx = (si["rect"].x0 + si["rect"].x1) / 2 / si["pw"]
            # require a real horizontal shift, not a borderline rounding
            if pa != sa and abs(pcx - scx) > 0.12:
                mism.append((i, pa, sa))
        if mism:
            findings.append(_f(
                "Image position", "Medium", title,
                ", ".join(str(sim[i]["page"]) for i, _a, _b in mism[:4]),
                ", ".join(f"#{i+1}:{a}" for i, a, _b in mism[:4]),
                ", ".join(f"#{i+1}:{b}" for i, _a, b in mism[:4]),
                f"{len(mism)} image(s) have different horizontal alignment.",
                "Re-align image(s) to match PROD."))


def _pctl(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, int(q * (len(s) - 1))))]


def check_table_layout(s_all, findings):
    """STAGE table/layout defects (a table continuing onto the next page is fine):

      • text overlapping other text / a table  (incl. cell-to-cell bleed)
      • cell content spilling into the adjacent cell
      • a table or text running past the right margin
    """
    lines_by_page, pw_by_page = {}, {}
    for l in s_all["lines"]:
        lines_by_page.setdefault(l["page"], []).append(l)
        pw_by_page[l["page"]] = l["pw"]

    # The document's established right text edge (where body text normally ends).
    # Use a high percentile of body-line right edges so the odd long line doesn't
    # define the margin. Content must exceed this AND sit clearly toward the page
    # edge to count as a real margin break — that keeps normal full-width text
    # from being flagged on every page.
    body_x1 = [l["rect"].x1 for l in s_all["lines"] if l["cls"] == "body"]
    doc_right = _pctl(body_x1, 0.93) if body_x1 else 0
    OVER = 10.0   # pt past the established edge before it's a break

    # ── A. right-margin overflow (text, then tables) ──
    for pg, lns in sorted(lines_by_page.items()):
        limit = max(doc_right, pw_by_page[pg] - 14)  # also allow up to near page edge
        worst = None
        for l in lns:
            if l["rect"].x1 > limit + OVER and len(l["text"]) >= 2:
                if worst is None or l["rect"].x1 > worst["rect"].x1:
                    worst = l
        if worst:
            findings.append(_f(
                "Table layout breaking", "Medium", "Right margin", str(pg),
                f"Text ends by ~{round(doc_right)}pt",
                f"Runs to {round(worst['rect'].x1)}pt",
                f"Text runs past the right margin: “{worst['text'][:45]}”.",
                "Reflow the content to stay inside the right margin."))
    for t in s_all["tables"]:
        if t["rect"].x1 > max(doc_right, t.get("pw", 0) - 14) + OVER:
            findings.append(_f(
                "Table layout breaking", "High", "Table width", str(t["page"]),
                f"Text ends by ~{round(doc_right)}pt",
                f"Table extends to {round(t['rect'].x1)}pt",
                "A table runs past the right margin.",
                "Narrow the table or its columns to fit the right margin."))

    # ── B. overlapping text (text-on-text, cell-to-cell visual overlap) ──
    seen = set()
    overlaps = []
    for pg, lns in lines_by_page.items():
        arr = sorted(lns, key=lambda l: l["rect"].y0)
        for i in range(len(arr)):
            a = arr[i]
            ar = a["rect"]
            for j in range(i + 1, len(arr)):
                b = arr[j]
                br = b["rect"]
                if br.y0 > ar.y1:
                    break
                ix = min(ar.x1, br.x1) - max(ar.x0, br.x0)
                iy = min(ar.y1, br.y1) - max(ar.y0, br.y0)
                if ix <= 0 or iy <= 0:
                    continue
                aw, bw = ar.x1 - ar.x0, br.x1 - br.x0
                ah, bh = ar.y1 - ar.y0, br.y1 - br.y0
                if (ix > 0.30 * min(aw, bw) and iy > 0.35 * min(ah, bh)
                        and len(a["text"]) >= 2 and len(b["text"]) >= 2):
                    key = (pg, a["text"][:30], b["text"][:30])
                    if key in seen:
                        continue
                    seen.add(key)
                    overlaps.append((pg, a["text"], b["text"]))
    for pg, t1, t2 in overlaps[:25]:
        findings.append(_f(
            "Table layout breaking", "High", "Overlapping text", str(pg),
            "Text laid out without overlap",
            f"“{t1[:30]}” overlaps “{t2[:30]}”",
            "Two pieces of text overlap (text-on-text or cell-to-cell bleed).",
            "Separate the overlapping text / fix the cell sizing."))
    if len(overlaps) > 25:
        findings.append(_f(
            "Table layout breaking", "High", "Overlapping text (more)", "various",
            "No overlap", f"{len(overlaps) - 25} further overlapping text runs",
            f"{len(overlaps) - 25} more overlapping text runs were found.",
            "Resolve the remaining overlaps."))

    # ── C. cell content spilling into the next cell (needs detected cells) ──
    for t in s_all["tables"]:
        cells = t.get("cells") or []
        if len(cells) < 2:
            continue
        for c in cells:
            has_right = any(o.x0 >= c.x1 - 1 and min(o.y1, c.y1) - max(o.y0, c.y0) > 2
                            for o in cells if o is not c)
            if not has_right:
                continue
            for l in lines_by_page.get(t["page"], []):
                r = l["rect"]
                cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
                if c.x0 - 1 <= cx <= c.x1 + 1 and c.y0 - 1 <= cy <= c.y1 + 1 and r.x1 > c.x1 + 2:
                    findings.append(_f(
                        "Table layout breaking", "High", "Cell overflow", str(t["page"]),
                        "Cell text fits its column",
                        f"“{l['text'][:30]}” spills past its cell",
                        "Cell content extends past its column into the adjacent cell.",
                        "Widen the column or shorten/wrap the cell text."))
                    break


def check_wrapped_text_padding(sections, findings):
    """Flag only true wrapped-text left-margin misalignment in STAGE tables.

    Skip ordinary wrapping and page-flow differences. Report only when wrapped
    lines inside the same table cell do not stay aligned to the same left start.
    """
    for title, p_feat, s_feat in sections:
        s = _wrapped_cell_padding_stats(s_feat)
        if not s["wrapped_cells"]:
            continue

        stage_pages = sorted({t["page"] for t in s_feat["tables"]})
        pages = ", ".join(str(pn) for pn in stage_pages[:6]) or "—"

        if s["bad_left_align"] > 0 and s["indent_drift"] is not None:
            findings.append(_f(
                "Wrapped text padding", "Low", title, pages,
                "Wrapped lines keep the same left margin inside each cell",
                f"Continuation-line indent drift ~{s['indent_drift']}pt",
                f"{s['bad_left_align']} wrapped table cell(s) keep shifting away from the left margin while wrapping.",
                "Align wrapped lines within each table cell to the same left margin; ignore page-to-page continuation when the left edge stays consistent."
            ))


def check_hyperlinks(sections, p_all, s_all, findings):
    # document-level URI comparison
    p_uris = {l["uri"] for l in p_all["links"] if l["kind"] == fitz.LINK_URI and l["uri"]}
    s_uris = {l["uri"] for l in s_all["links"] if l["kind"] == fitz.LINK_URI and l["uri"]}
    missing = p_uris - s_uris
    if missing:
        findings.append(_f(
            "Hyperlink issue", "High", "External links (document-wide)", "—",
            f"{len(p_uris)} external URL link(s)", f"{len(s_uris)} external URL link(s)",
            f"{len(missing)} external URL(s) in PROD are absent from STAGE: "
            + "; ".join(sorted(missing)[:3]) + (" …" if len(missing) > 3 else ""),
            "Restore the missing hyperlink targets."))
    # broken internal GOTO links (target page out of range)
    broken = [l for l in s_all["links"]
              if l["kind"] == fitz.LINK_GOTO and l["to_page"] is not None
              and (l["to_page"] < 1 or l["to_page"] > 10_000)]
    if broken:
        findings.append(_f(
            "Hyperlink issue", "High", "Internal links", ", ".join(str(l["page"]) for l in broken[:6]),
            "All internal links resolve", f"{len(broken)} link(s) point nowhere",
            f"{len(broken)} internal link(s) have an invalid destination.",
            "Re-point the internal links to valid pages."))
    # per-section: link count drop
    for title, p_feat, s_feat in sections:
        pn, sn = len(p_feat["links"]), len(s_feat["links"])
        if pn >= 3 and sn < pn * 0.5:
            findings.append(_f(
                "Hyperlink issue", "Medium", title,
                ", ".join(str(l["page"]) for l in s_feat["links"][:4]) or "—",
                f"{pn} link(s)", f"{sn} link(s)",
                f"This section has {pn-sn} fewer clickable links than PROD.",
                "Check that cross-references and URLs are still linked."))


def _text_at(lines_by_page, pg, rect):
    """Visible text overlapping a rect (for naming an underlined link)."""
    best, bo = "", 0
    for ln in lines_by_page.get(pg, []):
        r = ln["rect"]
        ox = min(r.x1, rect.x1) - max(r.x0, rect.x0)
        oy = min(r.y1, rect.y1) - max(r.y0, rect.y0)
        if ox > 0 and oy > 0 and ox * oy > bo:
            best, bo = ln["text"], ox * oy
    return best


def check_underline(s_all, findings):
    """Find underlined text/links in STAGE that should be removed.

    Two sources: (a) a thin horizontal stroke sitting under a hyperlink rect
    (underlined links) and (b) a stroke directly under a short text line that
    isn't inside a table (manually underlined emphasis).
    """
    lines_by_page, tbl_by_page = {}, {}
    for l in s_all["lines"]:
        lines_by_page.setdefault(l["page"], []).append(l)
    for t in s_all["tables"]:
        tbl_by_page.setdefault(t["page"], []).append(t["rect"])
    strokes_by_page = {}
    for st in s_all["hstrokes"]:
        strokes_by_page.setdefault(st["page"], []).append(st)

    def in_table(pg, y, x0, x1):
        return any(r.y0 - 2 <= y <= r.y1 + 2 and r.x0 - 3 <= x0 and x1 <= r.x1 + 3
                   for r in tbl_by_page.get(pg, []))

    hits = []   # (page, text, is_link)

    # (a) underlined links
    for l in s_all["links"]:
        pg, lr = l["page"], l["rect"]
        for st in strokes_by_page.get(pg, []):
            if lr.y0 - 1 <= st["y"] <= lr.y1 + 5 and min(st["x1"], lr.x1) - max(st["x0"], lr.x0) > 3:
                if not in_table(pg, st["y"], st["x0"], st["x1"]):
                    hits.append((pg, _text_at(lines_by_page, pg, lr)[:60] or "(link)", True))
                break

    # (b) underlined non-link text
    link_by_page = {}
    for l in s_all["links"]:
        link_by_page.setdefault(l["page"], []).append(l["rect"])
    for pg, strokes in strokes_by_page.items():
        for st in strokes:
            sw = st["x1"] - st["x0"]
            if sw <= 0 or in_table(pg, st["y"], st["x0"], st["x1"]):
                continue
            for ln in lines_by_page.get(pg, []):
                r = ln["rect"]
                if not (r.y1 - 1.0 <= st["y"] <= r.y1 + 3.5):
                    continue
                ox = min(st["x1"], r.x1) - max(st["x0"], r.x0)
                lw = r.x1 - r.x0
                if ox <= 0 or ox / sw < 0.6 or sw > lw * 1.4 or len(ln["text"]) > 80:
                    continue
                if any(lk.intersects(r) for lk in link_by_page.get(pg, [])):
                    continue   # already counted as a link
                hits.append((pg, ln["text"][:60], False))
                break

    # dedupe by (page, text)
    seen, uniq = set(), []
    for h in hits:
        k = (h[0], h[1])
        if k not in seen:
            seen.add(k)
            uniq.append(h)
    if not uniq:
        return
    n_link = sum(1 for _p, _t, isl in uniq if isl)
    for pg, txt, is_link in uniq[:25]:
        kind = "Underlined link" if is_link else "Underlined text"
        findings.append(_f(
            "Underline to remove", "Medium",
            "Underlined link" if is_link else "Underlined text", str(pg),
            "No underline", f"{kind}: “{txt}”",
            f"This {'hyperlink' if is_link else 'text'} is underlined.",
            "Remove the underline."))
    if len(uniq) > 25:
        findings.append(_f(
            "Underline to remove", "Medium", "Underlined text (more)", "various",
            "No underline", f"{len(uniq)-25} further underlined runs "
            f"({n_link} of {len(uniq)} total are links)",
            f"{len(uniq)-25} additional underlined runs were found beyond the first 25.",
            "Remove the underlines throughout."))


# ── orchestration ───────────────────────────────────────────────────────────
def validate_style(prod_path, stage_path):
    prod_toc = get_toc(prod_path)
    stage_toc = get_toc(stage_path)
    prod_doc = fitz.open(prod_path)
    stage_doc = fitz.open(stage_path)
    p_body = _doc_body_size(prod_doc)
    s_body = _doc_body_size(stage_doc)
    p_ranges = _toc_ranges_by_key(prod_toc, prod_doc.page_count)
    s_ranges = _toc_ranges_by_key(stage_toc, stage_doc.page_count)

    # Parse every page once, then assemble whole-document and per-section views
    # from the cache (avoids re-parsing pages for every section + info pass).
    p_cache = _build_cache(prod_doc, p_body)
    print("  extracted PROD features")
    s_cache = _build_cache(stage_doc, s_body)
    print("  extracted STAGE features")
    p_all = _assemble(p_cache, 1, prod_doc.page_count)
    s_all = _assemble(s_cache, 1, stage_doc.page_count)

    # matched sections (per-section checks). A section is "comparable" for
    # geometric checks only when PROD and STAGE map it to similarly-sized
    # ranges; otherwise STAGE re-paginates the topic and geometry comparison
    # is noise (same guard used by the TOC content/style validator).
    stage_keys = {_norm_key(t) for _, t, _ in stage_toc}
    sections, geo = [], []
    for _lvl, title, _pg in prod_toc:
        k = _norm_key(title)
        if k not in stage_keys:
            continue
        pr, sr = p_ranges.get(k), s_ranges.get(k)
        if not pr or not sr:
            continue
        p_feat = _assemble(p_cache, pr[0], pr[1])
        s_feat = _assemble(s_cache, sr[0], sr[1])
        sections.append((title, p_feat, s_feat))
        p_n = sum(1 for l in p_feat["lines"] if l["cls"] != "other")
        s_n = sum(1 for l in s_feat["lines"] if l["cls"] != "other")
        if max(p_n, s_n) >= 6 and 0.45 <= (s_n / max(p_n, 1)) <= 2.2:
            geo.append((title, p_feat, s_feat))
    print(f"  matched {len(sections)} sections ({len(geo)} comparable for geometry)")

    findings = []
    check_heading_color(p_all, s_all, findings)
    check_heading_size(geo, findings)
    check_text_color(p_all, s_all, sections, findings)
    p_info = analyze_info_callouts(prod_doc, p_cache)
    s_info = analyze_info_callouts(stage_doc, s_cache)
    check_info_callouts(p_info, s_info, findings)
    check_paragraph_spacing(geo, findings)
    check_space_above_image(geo, findings)
    check_image_position(geo, findings)
    check_table_layout(s_all, findings)
    check_wrapped_text_padding(geo, findings)
    check_hyperlinks(geo, p_all, s_all, findings)
    check_underline(s_all, findings)

    prod_doc.close()
    stage_doc.close()
    return findings


# ── report ──────────────────────────────────────────────────────────────────
CATEGORY_ORDER = [
    "Image position", "Space above image", "Heading style", "Paragraph spacing",
    "Text colour", "Info / notice colour", "Table layout breaking", "Wrapped text padding",
    "Hyperlink issue", "Underline to remove",
]
SEV_COLOR = {"High": colors.HexColor("#c62828"),
             "Medium": colors.HexColor("#e65100"),
             "Low": colors.HexColor("#1565c0")}


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_report(prod_path, stage_path, findings, out_path):
    doc = SimpleDocTemplate(out_path, pagesize=landscape(letter),
                            leftMargin=0.4*inch, rightMargin=0.4*inch,
                            topMargin=0.4*inch, bottomMargin=0.4*inch)
    ss = getSampleStyleSheet()
    title_s = ParagraphStyle("T", parent=ss["Heading1"], fontSize=16, spaceAfter=4)
    sub_s = ParagraphStyle("Sub", parent=ss["Normal"], fontSize=10, textColor=colors.grey, spaceAfter=2)
    head_s = ParagraphStyle("H", parent=ss["Heading2"], fontSize=13, spaceBefore=10, spaceAfter=6)
    hdr_s = ParagraphStyle("Hdr", parent=ss["Normal"], fontSize=9, leading=12,
                           textColor=colors.whitesmoke, fontName="Helvetica-Bold")
    cell_s = ParagraphStyle("Cell", parent=ss["Normal"], fontSize=8, leading=10)
    topic_s = ParagraphStyle("Topic", parent=ss["Normal"], fontSize=8, leading=10, fontName="Helvetica-Bold")
    fix_s = ParagraphStyle("Fix", parent=ss["Normal"], fontSize=8, leading=10, textColor=colors.HexColor("#2e7d32"))

    story = [Paragraph("Style Validation Report", title_s),
             Paragraph(f"Production (expected): {os.path.basename(prod_path)}", sub_s),
             Paragraph(f"Staging (actual):    {os.path.basename(stage_path)}", sub_s),
             Spacer(1, 10)]

    # summary by category
    by_cat = {c: [f for f in findings if f["category"] == c] for c in CATEGORY_ORDER}
    sum_rows = [[Paragraph("<b>Style check</b>", hdr_s), Paragraph("<b>Issues</b>", hdr_s),
                 Paragraph("<b>Highest severity</b>", hdr_s)]]
    for c in CATEGORY_ORDER:
        items = by_cat[c]
        if items:
            sev = next((s for s in ("High", "Medium", "Low") if any(i["severity"] == s for i in items)), "—")
        else:
            sev = "clean"
        sev_para = Paragraph(f"<b>{sev}</b>", ParagraphStyle("sv", parent=cell_s,
                              textColor=SEV_COLOR.get(sev, colors.HexColor("#2e7d32"))))
        sum_rows.append([Paragraph(c, topic_s),
                         Paragraph(str(len(items)) if items else "0", cell_s), sev_para])
    sumt = Table(sum_rows, colWidths=[230, 70, 140], repeatRows=1)
    sumt.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#37474f")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    total = len(findings)
    story.append(Paragraph(f"<b>{total}</b> style issue(s) found across "
                           f"{sum(1 for c in CATEGORY_ORDER if by_cat[c])} of {len(CATEGORY_ORDER)} checks. "
                           "PROD is the reference; fix STAGE to match.", sub_s))
    story.append(Spacer(1, 6))
    story.append(sumt)

    # per-category detail
    for c in CATEGORY_ORDER:
        items = by_cat[c]
        story.append(PageBreak())
        story.append(Paragraph(f"{c}", head_s))
        if not items:
            story.append(Paragraph("No issues — STAGE matches PROD for this check.",
                                   ParagraphStyle("ok", parent=ss["Normal"], fontSize=9,
                                                  textColor=colors.HexColor("#2e7d32"))))
            continue
        rows = [[Paragraph(f"<b>{h}</b>", hdr_s) for h in
                 ["#", "Topic", "Page(s)", "Sev", "Expected (PROD)", "Actual (STAGE)", "Issue & fix"]]]
        for i, f in enumerate(items, 1):
            sev_p = Paragraph(f"<b>{f['severity']}</b>", ParagraphStyle("s", parent=cell_s,
                              textColor=SEV_COLOR.get(f["severity"], colors.grey)))
            issue_fix = (f"{_esc(f['issue'])}<br/><font color='#2e7d32'><b>Fix:</b> {_esc(f['fix'])}</font>")
            rows.append([Paragraph(str(i), cell_s), Paragraph(_esc(f["topic"]), topic_s),
                         Paragraph(_esc(f["pages"]), cell_s), sev_p,
                         Paragraph(_esc(f["expected"]), cell_s),
                         Paragraph(_esc(f["actual"]), cell_s),
                         Paragraph(issue_fix, cell_s)])
        t = Table(rows, colWidths=[20, 130, 60, 45, 150, 150, 195], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#37474f")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#fff8f3")]),
            ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        story.append(t)

    doc.build(story)
    print(f"Report saved: {out_path}")


def main(prod_path, stage_path, out_path):
    print("Validating style (PROD = expected, STAGE = actual)...")
    findings = validate_style(prod_path, stage_path)
    by_cat = {}
    for f in findings:
        by_cat.setdefault(f["category"], 0)
        by_cat[f["category"]] += 1
    print(f"Total style issues: {len(findings)}")
    for c in CATEGORY_ORDER:
        print(f"  {c:24} {by_cat.get(c, 0)}")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    build_report(prod_path, stage_path, findings, out_path)
    return findings


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python style_validation.py <prod.pdf> <stage.pdf> [out.pdf]")
        sys.exit(1)
    prod = sys.argv[1]
    stage = sys.argv[2]
    out = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "reports", "style_validation_report.pdf")
    main(prod, stage, out)

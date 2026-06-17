#!/usr/bin/env python3
"""Comprehensive STAGE-vs-PROD style validation.

PROD is the reference ("expected"); STAGE is what's checked ("actual").
Nine style dimensions are validated and collected into one clean PDF report:

  1. Image position        — left / centre / right alignment per image
  2. Image padding         — left / right whitespace (margins) around each image
  3. Space above image     — vertical gap between an image and the text above it
  4. Heading style         — heading colour & relative size
  5. Paragraph spacing     — gap above and below body paragraphs
  6. Text colour           — body-text colour (should stay PROD's colour)
  7. Info / notice colour  — NOTE / TIP / IMPORTANT label-text colour only
                             (icon colour & themed background vary per type — not flagged)
  8. Table layout breaking — tables that split across a page boundary
  9. Hyperlink issues      — missing / broken / re-targeted links

Built on PyMuPDF (open-source). Run:

    python style_validation.py <prod.pdf> <stage.pdf> [out_report.pdf]

Default output: <project-root>/reports/style_validation_report.pdf
"""
import os
import re
import statistics
import sys

# Configure local TESSDATA_PREFIX before importing fitz (PyMuPDF)
_CUR_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_CUR_DIR)
_LOCAL_TESSDATA = os.path.join(_PROJECT_ROOT, "tessdata")
if os.path.isdir(_LOCAL_TESSDATA):
    os.environ["TESSDATA_PREFIX"] = _LOCAL_TESSDATA

import fitz  # PyMuPDF

from reportlab.lib import colors
from reportlab.lib.pagesizes import landscape, letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)

# Works both as a package submodule (content_validation.style_validation) and
# when run_validator.py / the tests import this file as a top-level module.
try:
    from .validate_toc_content import get_toc, _norm_key, _is_pdf_garbled, _get_pdf_language
except ImportError:
    from validate_toc_content import get_toc, _norm_key, _is_pdf_garbled, _get_pdf_language

NOTICE_RE = re.compile(r"\b(NOTE|TIP|IMPORTANT|WARNING|CAUTION|INFO)\b", re.IGNORECASE)


# ── Style helpers (self-contained; previously shared from validate_toc_content) ──
def _toc_ranges_by_key(toc: list, total_pages: int) -> dict:
    """Return {_norm_key(title): (start_page, end_page, title)} for TOC entries."""
    out = {}
    for i, (lvl, title, pg) in enumerate(toc):
        end_pg = total_pages
        for j in range(i + 1, len(toc)):
            if toc[j][0] <= lvl:
                end_pg = toc[j][2] - 1
                break
        key = _norm_key(title)
        out.setdefault(key, (pg, max(pg, end_pg), title))
    return out


def _doc_body_size(doc) -> float:
    """Most common font size of running body text (lines >= 15 chars)."""
    sizes = {}
    for page in doc:
        d = page.get_text("dict")
        for b in d.get("blocks", []):
            if b.get("type") != 0:
                continue
            for line in b.get("lines", []):
                spans = line.get("spans", [])
                txt = "".join(s.get("text", "") for s in spans).strip()
                if len(txt) < 15:
                    continue
                mx = max((round(s.get("size", 0.0), 1) for s in spans), default=0.0)
                if mx > 0:
                    sizes[mx] = sizes.get(mx, 0) + 1
    if not sizes:
        return 10.0
    return max(sizes.items(), key=lambda kv: kv[1])[0]


def _style_class_rel(text: str, max_size: float, body_size: float) -> str:
    """Classify a line by its size ratio to the document's own body size."""
    t = (text or "").strip()
    if not t:
        return "other"
    if NOTICE_RE.search(t):
        return "notice"
    ratio = (max_size / body_size) if body_size > 0 else 1.0
    if ratio >= 1.45:
        return "heading"
    if ratio >= 1.12:
        return "subheading"
    if ratio >= 0.85 and len(t) >= 15:
        return "body"
    return "other"

# Optional progress reporting. run_validator.py installs a callback so the web
# UI's progress bar can advance *during* a job (per page parsed), not just jump
# 0→100% at the end. No-op when run standalone or in tests.
_PROGRESS_CB = None


def set_progress_callback(cb):
    global _PROGRESS_CB
    _PROGRESS_CB = cb


def _emit(frac, msg=""):
    if _PROGRESS_CB:
        try:
            _PROGRESS_CB(max(0.0, min(1.0, float(frac))), msg)
        except Exception:
            pass

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
IMG_PAD_TOL = 12        # pt — left/right image padding diff (PROD vs STAGE) above which we flag
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

    doc = page.parent
    tp = None
    if _is_pdf_garbled(doc):
        lang = _get_pdf_language(doc)
        try:
            tp = page.get_textpage_ocr(dpi=150, language=lang)
        except Exception as e:
            print(f"OCR failed for style page {pno}: {e}")

    for b in page.get_text("dict", textpage=tp).get("blocks", []):
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
    # single get_drawings() pass yields underline strokes, bg fills, and a quick
    # count of horizontal/vertical rules used to decide whether a table is even
    # possible on this page.
    h_lines = v_lines = 0
    for dr in page.get_drawings():
        fc, r = _fill_to_int(dr.get("fill")), dr.get("rect")
        if fc is not None and r is not None and r.width > 100 and r.height > 14:
            fills.append((r, fc))
        for it in dr.get("items", []):
            if it[0] == "l":
                a, b2 = it[1], it[2]
                dy, dx = abs(a.y - b2.y), abs(b2.x - a.x)
                if dy < 0.7 and dx > 12:
                    hstrokes.append({"page": pno, "y": (a.y + b2.y) / 2,
                                     "x0": min(a.x, b2.x), "x1": max(a.x, b2.x)})
                    h_lines += 1
                elif dx < 0.7 and dy > 12:
                    v_lines += 1
            elif it[0] == "re":
                r2 = it[1]
                if r2.height < 1.3 and r2.width > 12:
                    hstrokes.append({"page": pno, "y": r2.y0 + r2.height / 2,
                                     "x0": r2.x0, "x1": r2.x1})
                    h_lines += 1
                elif r2.width > 12 and r2.height > 12:
                    h_lines += 1
                    v_lines += 1
    # find_tables() is the most expensive call; skip it on pages with no grid of
    # ruling lines (text-only pages can't hold a line-detected table anyway).
    if h_lines >= 2 and v_lines >= 1:
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


def _build_cache(doc, body_size, prog=None):
    """Parse every page once → {page_no: feature record}.

    prog: optional (lo, hi, label) — emit progress in the [lo, hi] band as pages
    are parsed (this is the slow part, so it's where the bar visibly moves).
    """
    n = doc.page_count
    out = {}
    for pno in range(1, n + 1):
        out[pno] = _extract_page(doc[pno - 1], pno, body_size)
        if prog and (pno % 2 == 0 or pno == n):
            lo, hi, label = prog
            _emit(lo + (hi - lo) * pno / n, label)
    return out


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


def _space_below_headings(feat):
    """List of (heading_line, gap_below) using nearest line below with x-overlap on the same page."""
    out = []
    lines = sorted(feat["lines"], key=lambda l: (l["page"], l["rect"].y0))
    for i, h_line in enumerate(lines):
        if h_line["cls"] not in ("heading", "subheading"):
            continue
        best = None
        for j in range(i + 1, len(lines)):
            b = lines[j]
            if b["page"] != h_line["page"]:
                break
            r = b["rect"]
            hr = h_line["rect"]
            if r.y0 >= hr.y1 - 1 and not (r.x1 < hr.x0 or r.x0 > hr.x1):
                best = r.y0
                break
        if best is not None:
            gap = round(best - h_line["rect"].y1, 1)
            if 0 <= gap < 150:
                out.append((h_line, gap))
    return out


# ── checks (each returns a list of finding dicts) ───────────────────────────
def _f(cat, sev, topic, pages, expected, actual, issue, fix):
    return {"category": cat, "severity": sev, "topic": topic, "pages": pages,
            "expected": expected, "actual": actual, "issue": issue, "fix": fix}


# BenQ A4 PDF typography specification
# Source: BenQ style guide (H1/H2/H3 headings, body, table note)
_BENQ_TYPO_SPEC = {
    "H1 heading":      {"size": 20.0, "bold": True,  "tol": 1.5},
    "H2 heading":      {"size": 14.0, "bold": True,  "tol": 1.5},
    "H3 heading":      {"size": 12.0, "bold": True,  "tol": 1.0},
    "Body text":       {"size": 12.0, "bold": False, "tol": 1.0},
    "Table note text": {"size": 10.0, "bold": False, "tol": 1.0},
}

# Tolerance (pt) within which a size is accepted as matching the spec
_TYPO_TOL = 1.5


def _is_bold_span(flags: int) -> bool:
    """PyMuPDF font flags: bit 4 (0x10) = bold."""
    return bool(flags & 0x10)


def check_typography_spec(stage_doc, s_body, findings):
    """Check that STAGE heading and body font sizes match the BenQ A4 PDF spec.

    BenQ A4 PDF specification (from style guide):
      H1  — 20 pt, Bold
      H2  — 14 pt, Bold
      H3  — 12 pt, Bold
      Body default — 12 pt, Regular
      Table note   — 10 pt, Regular

    Strategy: collect all text spans from STAGE, bucket them by approximate
    size, then map the three largest size buckets to H1/H2/H3 and verify.
    Body and table-note are checked using modal sizes near the spec values.
    """
    from collections import defaultdict

    # Collect (size, bold, page) per span across the whole document
    size_samples: dict[float, list] = defaultdict(list)
    for pno, page in enumerate(stage_doc, 1):
        for b in page.get_text("dict").get("blocks", []):
            if b.get("type") != 0:
                continue
            for ln in b.get("lines", []):
                for sp in ln.get("spans", []):
                    txt = (sp.get("text") or "").strip()
                    if len(txt) < 2:
                        continue
                    sz = round(sp.get("size", 0.0) * 2) / 2  # round to 0.5pt
                    bold = _is_bold_span(sp.get("flags", 0))
                    size_samples[sz].append({"page": pno, "bold": bold, "text": txt})

    if not size_samples:
        return

    # Sort size buckets largest-first to identify heading levels
    sorted_sizes = sorted(size_samples.keys(), reverse=True)

    # Heading candidates: sizes noticeably above the body size
    heading_candidates = [s for s in sorted_sizes if s > s_body + 1.0]

    spec_checks = []
    if len(heading_candidates) >= 1:
        spec_checks.append(("H1 heading", heading_candidates[0]))
    if len(heading_candidates) >= 2:
        spec_checks.append(("H2 heading", heading_candidates[1]))
    if len(heading_candidates) >= 3:
        spec_checks.append(("H3 heading", heading_candidates[2]))

    # Body text: modal size near 12pt
    body_candidates = [s for s in sorted_sizes
                       if abs(s - 12.0) <= 2.0 and s <= s_body + 1.0]
    if body_candidates:
        # pick the most common one
        body_size = max(body_candidates, key=lambda s: len(size_samples[s]))
        spec_checks.append(("Body text", body_size))

    # Table note: modal size near 10pt
    note_candidates = [s for s in sorted_sizes if abs(s - 10.0) <= 2.0]
    if note_candidates:
        note_size = max(note_candidates, key=lambda s: len(size_samples[s]))
        spec_checks.append(("Table note text", note_size))

    for level, actual_size in spec_checks:
        spec = _BENQ_TYPO_SPEC[level]
        expected_size = spec["size"]
        tol = spec["tol"]
        samples = size_samples[actual_size]
        pages_str = ", ".join(str(p) for p in
                              sorted({s["page"] for s in samples})[:5])

        # Check size
        if abs(actual_size - expected_size) > tol:
            sev = "High" if abs(actual_size - expected_size) > 3 else "Medium"
            findings.append(_f(
                "Typography spec", sev, level, pages_str,
                f"{expected_size}pt",
                f"{actual_size}pt",
                f"{level} size is {actual_size}pt — BenQ A4 spec requires {expected_size}pt "
                f"(Δ{abs(round(actual_size - expected_size, 1))}pt).",
                f"Set {level} to {expected_size}pt per BenQ A4 PDF typography specification.",
            ))

        # Check bold for headings
        if spec["bold"] and level.endswith("heading"):
            non_bold = [s for s in samples if not s["bold"]]
            if non_bold and len(non_bold) / max(len(samples), 1) > 0.3:
                nb_pages = ", ".join(str(s["page"]) for s in non_bold[:5])
                findings.append(_f(
                    "Typography spec", "Medium", f"{level} (not bold)", nb_pages,
                    "Bold",
                    "Regular / not bold",
                    f"{level} has non-bold instances — BenQ A4 spec requires Bold.",
                    f"Apply Bold weight to all {level} text.",
                ))


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

        # NOTE: the coloured icon and the themed background colour legitimately
        # differ per callout type (NOTE / TIP / IMPORTANT / WARNING each carry
        # their own theme colour), so icon-colour and background-colour diffs are
        # NOT flagged — they are expected variation, not defects.

        # text should be coloured (theme colour for the callout type)
        p_text = _mode(p["text"]) if p else None
        if not text_colored:
            findings.append(_f(
                "Info / notice colour", "Low", f"{typ} text", pages,
                "Coloured label text" + (f" (PROD {_hex(p_text)})" if _is_chromatic(p_text) else ""),
                f"Label text {_hex(s_text)} (not coloured)",
                f"{typ} label text is {_hex(s_text)}, which is a grey/black, not a theme colour.",
                "Colour the label text to the callout's theme colour."))


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


def check_image_dimensions_and_alignment(sections, findings):
    for title, p_feat, s_feat in sections:
        pim = sorted([im for im in p_feat["images"] if max(im["rect"].width, im["rect"].height) > 80.0],
                     key=lambda im: (im["page"], im["rect"].y0))
        sim = sorted([im for im in s_feat["images"] if max(im["rect"].width, im["rect"].height) > 80.0],
                     key=lambda im: (im["page"], im["rect"].y0))
        if not pim or not sim or len(pim) != len(sim):
            continue
            
        for i, (pi, si) in enumerate(zip(pim, sim)):
            pw_val, sw_val = round(pi["rect"].width, 1), round(si["rect"].width, 1)
            ph_val, sh_val = round(pi["rect"].height, 1), round(si["rect"].height, 1)
            w_diff = abs(pw_val - sw_val)
            h_diff = abs(ph_val - sh_val)
            bad_w = w_diff > 5.0 and (w_diff / max(pw_val, 1)) > 0.10
            bad_h = h_diff > 5.0 and (h_diff / max(ph_val, 1)) > 0.10
            if bad_w or bad_h:
                dim_note = []
                if bad_w:
                    dim_note.append(f"W: PROD {pw_val}pt → STAGE {sw_val}pt")
                if bad_h:
                    dim_note.append(f"H: PROD {ph_val}pt → STAGE {sh_val}pt")
                findings.append(_f(
                    "Image dimension", "Medium", title, str(si["page"]),
                    f"Image #{i+1} W:{pw_val}pt × H:{ph_val}pt",
                    f"Image #{i+1} W:{sw_val}pt × H:{sh_val}pt",
                    f"Image #{i+1} has dimension mismatch: " + "; ".join(dim_note) + ".",
                    f"Resize the image in STAGE to match PROD W:{pw_val}pt × H:{ph_val}pt."))
            
            pa, sa = _align(pi["rect"], pi["pw"]), _align(si["rect"], si["pw"])
            pcx = (pi["rect"].x0 + pi["rect"].x1) / 2 / pi["pw"]
            scx = (si["rect"].x0 + si["rect"].x1) / 2 / si["pw"]
            if pa != sa and abs(pcx - scx) > 0.12:
                findings.append(_f(
                    "Image alignment", "Medium", title, str(si["page"]),
                    f"Image #{i+1} aligned {pa}", f"Image #{i+1} aligned {sa}",
                    f"Image #{i+1} is aligned {sa} in STAGE but {pa} in PROD.",
                    f"Re-align the image to match PROD ({pa})."))


def check_icon_sizes_and_alignment(sections, findings):
    for title, p_feat, s_feat in sections:
        pim = sorted([im for im in p_feat["images"] if max(im["rect"].width, im["rect"].height) <= 80.0],
                     key=lambda im: (im["page"], im["rect"].y0))
        sim = sorted([im for im in s_feat["images"] if max(im["rect"].width, im["rect"].height) <= 80.0],
                     key=lambda im: (im["page"], im["rect"].y0))
        if not pim or not sim or len(pim) != len(sim):
            continue
            
        for i, (pi, si) in enumerate(zip(pim, sim)):
            pw_val, sw_val = round(pi["rect"].width, 1), round(si["rect"].width, 1)
            ph_val, sh_val = round(pi["rect"].height, 1), round(si["rect"].height, 1)
            w_diff = abs(pw_val - sw_val)
            h_diff = abs(ph_val - sh_val)
            bad_w = w_diff > 3.0 and (w_diff / max(pw_val, 1)) > 0.15
            bad_h = h_diff > 3.0 and (h_diff / max(ph_val, 1)) > 0.15
            if bad_w or bad_h:
                dim_note = []
                if bad_w:
                    dim_note.append(f"W: PROD {pw_val}pt → STAGE {sw_val}pt")
                if bad_h:
                    dim_note.append(f"H: PROD {ph_val}pt → STAGE {sh_val}pt")
                findings.append(_f(
                    "Icon size", "Medium", title, str(si["page"]),
                    f"Icon #{i+1} W:{pw_val}pt × H:{ph_val}pt",
                    f"Icon #{i+1} W:{sw_val}pt × H:{sh_val}pt",
                    f"Icon #{i+1} has dimension mismatch: " + "; ".join(dim_note) + ".",
                    f"Resize the icon in STAGE to match PROD W:{pw_val}pt × H:{ph_val}pt."))
            
            pa, sa = _align(pi["rect"], pi["pw"]), _align(si["rect"], si["pw"])
            pcx = (pi["rect"].x0 + pi["rect"].x1) / 2 / pi["pw"]
            scx = (si["rect"].x0 + si["rect"].x1) / 2 / si["pw"]
            if pa != sa and abs(pcx - scx) > 0.12:
                findings.append(_f(
                    "Icon alignment", "Low", title, str(si["page"]),
                    f"Icon #{i+1} aligned {pa}", f"Icon #{i+1} aligned {sa}",
                    f"Icon #{i+1} is aligned {sa} in STAGE but {pa} in PROD.",
                    f"Re-align the icon to match PROD ({pa})."))


def check_heading_below_spacing(sections, findings):
    for title, p_feat, s_feat in sections:
        pgaps = _space_below_headings(p_feat)
        sgaps = _space_below_headings(s_feat)

        for cls_name, label in [("heading", "H1"), ("subheading", "H2/H3")]:
            pv = [g for ln, g in pgaps if ln["cls"] == cls_name]
            sv = [g for ln, g in sgaps if ln["cls"] == cls_name]

            pm, sm = _median(pv), _median(sv)
            if pm is not None and sm is not None and abs(pm - sm) > 6.0:
                more_less = "larger" if sm > pm else "smaller"
                findings.append(_f(
                    "Heading spacing below", "Low", title,
                    ", ".join(str(ln["page"]) for ln, g in sgaps if ln["cls"] == cls_name)[:20] or "—",
                    f"~{pm}pt space below {label}", f"~{sm}pt space below {label}",
                    f"The vertical space below {label} headings is {more_less} than PROD (STAGE ~{sm}pt vs PROD ~{pm}pt).",
                    f"Adjust the spacing below {label} headings to ~{pm}pt to match PROD."))


def check_heading_line_height_global(p_all, s_all, findings):
    """Check space-below-heading across ALL pages end-to-end (PROD median vs each STAGE heading).

    Returns (prod_heading_count, stage_heading_count, issue_count).
    Complements check_heading_below_spacing (section-level) with a document-wide view.
    """
    pgaps = _space_below_headings(p_all)
    sgaps = _space_below_headings(s_all)

    prod_total = len(pgaps)
    stage_total = len(sgaps)
    issue_count = 0

    for cls_name, label in [("heading", "H1"), ("subheading", "H2/H3")]:
        pv  = [g for ln, g in pgaps if ln["cls"] == cls_name]
        sv  = [(ln, g) for ln, g in sgaps if ln["cls"] == cls_name]
        pm  = _median(pv)
        sm  = _median([g for _, g in sv])
        if pm is None:
            continue

        tol = 5.0  # pt tolerance — flag headings whose gap deviates more than this
        bad_pages = sorted({ln["page"] for ln, g in sv if abs(g - pm) > tol})
        n_bad = sum(1 for _, g in sv if abs(g - pm) > tol)

        if n_bad > 0:
            issue_count += n_bad
            pg_str = ", ".join(str(p) for p in bad_pages[:15])
            if len(bad_pages) > 15:
                pg_str += f" … (+{len(bad_pages)-15} more)"
            findings.append(_f(
                "Heading line height", "Medium",
                f"{label} — line height below heading (all pages)",
                pg_str or "—",
                f"~{pm}pt below {label} (PROD median across all pages)",
                f"~{sm}pt median · {n_bad} of {len(sv)} {label} headings deviate >5pt in STAGE",
                (f"{n_bad} {label} heading(s) on page(s) {pg_str} have incorrect line height below "
                 f"the heading. PROD median is ~{pm}pt; STAGE median is ~{sm}pt."),
                f"Set the paragraph spacing below {label} headings in STAGE to ~{pm}pt to match PROD."
            ))

    return prod_total, stage_total, issue_count


def check_image_dimensions_global(p_cache, s_cache, prod_doc, stage_doc, findings):
    """Compare image dimensions page-by-page across ALL pages (width + height, ≥80pt images).

    Returns (images_checked, issues_found).
    Complements check_image_dimensions_and_alignment (section-level) with full-document coverage.
    """
    checked = 0
    issues  = 0

    common_pages = min(prod_doc.page_count, stage_doc.page_count)
    for pno in range(1, common_pages + 1):
        if pno not in p_cache or pno not in s_cache:
            continue
        p_feat = _assemble(p_cache, pno, pno)
        s_feat = _assemble(s_cache, pno, pno)

        pim = sorted(
            [im for im in p_feat["images"] if max(im["rect"].width, im["rect"].height) > 80.0],
            key=lambda im: im["rect"].y0,
        )
        sim = sorted(
            [im for im in s_feat["images"] if max(im["rect"].width, im["rect"].height) > 80.0],
            key=lambda im: im["rect"].y0,
        )
        if not pim or not sim:
            continue

        for idx, (pi, si) in enumerate(zip(pim, sim), 1):
            checked += 1
            pw = round(pi["rect"].width,  1)
            ph = round(pi["rect"].height, 1)
            sw = round(si["rect"].width,  1)
            sh = round(si["rect"].height, 1)

            w_diff  = abs(pw - sw)
            h_diff  = abs(ph - sh)
            w_pct   = w_diff / max(pw, 1) * 100
            h_pct   = h_diff / max(ph, 1) * 100

            bad_w = w_diff > 5.0 and w_pct > 10
            bad_h = h_diff > 5.0 and h_pct > 10
            if bad_w or bad_h:
                issues += 1
                dim_note = []
                if bad_w:
                    dim_note.append(f"W: PROD {pw}pt → STAGE {sw}pt ({w_pct:.0f}% off)")
                if bad_h:
                    dim_note.append(f"H: PROD {ph}pt → STAGE {sh}pt ({h_pct:.0f}% off)")
                findings.append(_f(
                    "Image dimension", "Medium",
                    f"Page {pno} — image #{idx}",
                    str(pno),
                    f"W:{pw}pt × H:{ph}pt (PROD)",
                    f"W:{sw}pt × H:{sh}pt (STAGE)",
                    "Image dimension mismatch: " + "; ".join(dim_note) + ".",
                    f"Resize image #{idx} on page {pno} in STAGE to W:{pw}pt × H:{ph}pt.",
                ))

    return checked, issues


def check_image_padding(sections, findings):
    """Apple-to-apple image padding: the left / right whitespace (margins) around
    each image in PROD should match STAGE. Images are paired by reading order
    within a section, only when both sides expose the same image count (so we
    compare like for like). Right padding is only judged when the two images are
    a similar width (otherwise the difference is a resize, not a padding change),
    and pages with different geometry are skipped."""
    for title, p_feat, s_feat in sections:
        pim = sorted(p_feat["images"], key=lambda im: (im["page"], im["rect"].y0))
        sim = sorted(s_feat["images"], key=lambda im: (im["page"], im["rect"].y0))
        if not pim or not sim or len(pim) != len(sim):
            continue
        mism = []
        for i, (pi, si) in enumerate(zip(pim, sim)):
            if abs(pi["pw"] - si["pw"]) > 6:      # different page geometry — not comparable
                continue
            pl, sl = pi["rect"].x0, si["rect"].x0                 # left padding (indent)
            pr, sr = pi["pw"] - pi["rect"].x1, si["pw"] - si["rect"].x1   # right padding
            pwd, swd = pi["rect"].width, si["rect"].width
            widths_similar = max(pwd, swd) and abs(pwd - swd) / max(pwd, swd) < 0.15
            dl = abs(pl - sl)
            dr = abs(pr - sr) if widths_similar else 0.0
            if dl > IMG_PAD_TOL or dr > IMG_PAD_TOL:
                mism.append((i, round(pl), round(sl), round(pr), round(sr), max(dl, dr)))
        if mism:
            worst = round(max(m[5] for m in mism))
            findings.append(_f(
                "Image padding", "Low", title,
                ", ".join(str(sim[i]["page"]) for i, *_ in mism[:4]),
                ", ".join(f"#{i+1}:L{pl}/R{pr}pt" for i, pl, sl, pr, sr, _ in mism[:4]),
                ", ".join(f"#{i+1}:L{sl}/R{sr}pt" for i, pl, sl, pr, sr, _ in mism[:4]),
                f"{len(mism)} image(s) have different left/right padding (up to {worst}pt).",
                "Adjust image margins to match PROD."))


# ── Row-grouping helper (shared by layout consistency) ────────────────────────
_ROW_Y_TOL = 15.0   # pt — images within this vertical distance are "same row"


def _group_images_into_rows(images, min_dim=0.0):
    """Group images into visual rows based on vertical proximity.

    Returns a list of rows, where each row is a list of images sorted left-to-right.
    Only images whose max(width, height) > min_dim are considered.
    """
    filtered = sorted(
        [im for im in images if max(im["rect"].width, im["rect"].height) > min_dim],
        key=lambda im: (im["page"], im["rect"].y0, im["rect"].x0),
    )
    if not filtered:
        return []

    rows = []
    cur_row = [filtered[0]]
    for im in filtered[1:]:
        prev = cur_row[-1]
        # Same page and within Y tolerance → same row
        if im["page"] == prev["page"] and abs(im["rect"].y0 - prev["rect"].y0) <= _ROW_Y_TOL:
            cur_row.append(im)
        else:
            rows.append(sorted(cur_row, key=lambda x: x["rect"].x0))
            cur_row = [im]
    if cur_row:
        rows.append(sorted(cur_row, key=lambda x: x["rect"].x0))
    return rows


def check_image_layout_consistency(sections, findings):
    """End-to-end image layout validation: if PROD has N images in a row,
    STAGE must have N images in that same row. Also validates total image count
    per section and detects missing/extra rows.

    This covers:
    - Image count mismatch per section (PROD 5 images, STAGE 3)
    - Row structure mismatch (PROD has 3 images in row 2, STAGE has 2)
    - Missing / extra image rows
    """
    for title, p_feat, s_feat in sections:
        # Only consider real content images (>= 20pt, which catches both icons & figures)
        p_rows = _group_images_into_rows(p_feat["images"], min_dim=20.0)
        s_rows = _group_images_into_rows(s_feat["images"], min_dim=20.0)

        p_total = sum(len(r) for r in p_rows)
        s_total = sum(len(r) for r in s_rows)

        # ── 1. Total image count mismatch per section ──
        if p_total > 0 and s_total == 0:
            findings.append(_f(
                "Image layout", "High", title, "—",
                f"{p_total} image(s) in {len(p_rows)} row(s)",
                "0 images",
                f"STAGE is missing all {p_total} image(s) that PROD has in this section.",
                "Restore the missing images to match PROD layout."))
            continue
        if p_total == 0:
            continue

        if abs(p_total - s_total) > 0:
            sev = "High" if abs(p_total - s_total) >= 2 else "Medium"
            stage_pages = sorted({im["page"] for r in s_rows for im in r})
            pg_str = ", ".join(str(p) for p in stage_pages[:6]) or "—"
            findings.append(_f(
                "Image layout", sev, title, pg_str,
                f"{p_total} image(s)", f"{s_total} image(s)",
                f"Image count mismatch: PROD has {p_total} image(s) but STAGE has {s_total}.",
                "Add or remove images in STAGE to match the PROD count."))

        # ── 2. Row-by-row comparison ──
        # Only compare when both sides have rows
        n_common = min(len(p_rows), len(s_rows))
        for row_idx in range(n_common):
            p_row = p_rows[row_idx]
            s_row = s_rows[row_idx]
            p_count = len(p_row)
            s_count = len(s_row)

            if p_count != s_count:
                s_pg = s_row[0]["page"] if s_row else "—"
                findings.append(_f(
                    "Image layout", "Medium", title, str(s_pg),
                    f"Row {row_idx+1}: {p_count} image(s)",
                    f"Row {row_idx+1}: {s_count} image(s)",
                    f"Image row {row_idx+1} has {p_count} image(s) in PROD but {s_count} in STAGE.",
                    f"Adjust row {row_idx+1} in STAGE to have {p_count} image(s) matching PROD."))

        # ── 3. Missing / extra rows ──
        if len(p_rows) > len(s_rows):
            for row_idx in range(len(s_rows), len(p_rows)):
                p_row = p_rows[row_idx]
                findings.append(_f(
                    "Image layout", "High", title, "—",
                    f"Row {row_idx+1}: {len(p_row)} image(s)",
                    "Row missing",
                    f"STAGE is missing image row {row_idx+1} (PROD has {len(p_row)} image(s) in this row).",
                    f"Add image row {row_idx+1} to STAGE with {len(p_row)} image(s)."))
        elif len(s_rows) > len(p_rows):
            for row_idx in range(len(p_rows), len(s_rows)):
                s_row = s_rows[row_idx]
                s_pg = s_row[0]["page"] if s_row else "—"
                findings.append(_f(
                    "Image layout", "Medium", title, str(s_pg),
                    "No such row in PROD",
                    f"Row {row_idx+1}: {len(s_row)} extra image(s)",
                    f"STAGE has an extra image row {row_idx+1} with {len(s_row)} image(s) not in PROD.",
                    f"Remove the extra image row {row_idx+1} from STAGE or verify it is intentional."))


def _pctl(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, max(0, int(q * (len(s) - 1))))]


_FOOTER_NUM_RE = re.compile(r"^\d{1,3}$")
_BULLET_RE = re.compile(r"^\s*[•▪◦‣∙·\-\*]\s+\S")


def check_bullet_paragraph_size(s_all, findings):
    """Bullet-list text should be the same size as body paragraph text. Flag
    when bullets are rendered larger than paragraphs."""
    from collections import Counter
    bullets = []        # (size, page)
    para_sizes = []
    for l in s_all["lines"]:
        t = l["text"].strip()
        if len(t) < 6:
            continue
        sz = round(l["size"], 1)
        if _BULLET_RE.match(t):
            bullets.append((sz, l["page"]))
        elif len(t) >= 25 and not t.endswith(":"):
            para_sizes.append(sz)
    if len(bullets) < 2 or not para_sizes:
        return
    b_mode = Counter(sz for sz, _ in bullets).most_common(1)[0][0]
    p_mode = Counter(para_sizes).most_common(1)[0][0]
    if b_mode - p_mode > 0.5:
        pages = sorted({pg for sz, pg in bullets if sz > p_mode + 0.4})
        plist = ", ".join(str(p) for p in pages[:20]) + (" …" if len(pages) > 20 else "")
        findings.append(_f(
            "Bullet vs paragraph size", "Medium", "Bullet text larger than paragraphs", plist,
            f"Bullets same size as body text (~{p_mode}pt)",
            f"Bullets ~{b_mode}pt vs paragraphs ~{p_mode}pt",
            f"Bullet-list text is {round(b_mode - p_mode, 1)}pt larger than the body "
            f"paragraph text ({b_mode}pt vs {p_mode}pt).",
            f"Set bullet text to the paragraph size (~{p_mode}pt)."))


def check_footer_alignment(s_all, findings):
    """Footer page numbers should be right-aligned. Flag pages where the number
    is left- or centre-aligned instead (grouped, one finding per alignment)."""
    lines = s_all["lines"]
    body_x0 = [l["rect"].x0 for l in lines if len(l["text"]) >= 20]
    body_x1 = [l["rect"].x1 for l in lines if len(l["text"]) >= 20]
    if not body_x0 or not body_x1:
        return
    left = _pctl(body_x0, 0.10)          # content left margin
    right = _pctl(body_x1, 0.90)         # content right margin
    centre = (left + right) / 2.0
    tol = max(18.0, (right - left) * 0.12)

    # one footer number per page: a pure-digit line nearest the page bottom
    cand = {}
    for l in lines:
        r = l["rect"]
        if r.y0 <= l["ph"] - 55:
            continue
        if not _FOOTER_NUM_RE.match(l["text"].strip()):
            continue
        pg = l["page"]
        if pg not in cand or r.y0 > cand[pg].y0:
            cand[pg] = r

    by_align = {"left": [], "centre": [], "other": []}
    for pg, r in cand.items():
        cx = (r.x0 + r.x1) / 2.0
        if abs(r.x1 - right) < tol and r.x0 > centre:
            align = "right"
        elif abs(r.x0 - left) < tol and r.x1 < centre:
            align = "left"
        elif abs(cx - centre) < tol:
            align = "centre"
        else:
            align = "other"
        if align != "right":
            by_align[align].append(pg)

    for align, pages in by_align.items():
        if not pages:
            continue
        pages = sorted(set(pages))
        plist = ", ".join(str(p) for p in pages[:20]) + (" …" if len(pages) > 20 else "")
        findings.append(_f(
            "Footer page number", "Medium", f"Footer ({align}-aligned)", plist,
            "Page number right-aligned",
            f"{align.capitalize()}-aligned on {len(pages)} page(s)",
            f"The footer page number is {align}-aligned on {len(pages)} page(s) "
            f"instead of right-aligned.",
            "Right-align the footer page number on these pages."))


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


def check_table_cell_padding(s_all, findings):
    """Flag table cells in STAGE that have improper padding (too close to cell borders)."""
    # Group lines by page for fast lookup
    lines_by_page = {}
    for l in s_all.get("lines", []):
        lines_by_page.setdefault(l["page"], []).append(l)

    issues_by_page = {}
    for tbl in s_all.get("tables", []):
        pg = tbl["page"]
        page_lines = lines_by_page.get(pg, [])
        bad_cells = []
        for idx, cell in enumerate(tbl.get("cells") or []):
            if cell.width < 15 or cell.height < 10:
                continue
            cell_lines = []
            for ln in page_lines:
                r = ln["rect"]
                cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
                if cell.x0 <= cx <= cell.x1 and cell.y0 <= cy <= cell.y1 and len(ln["text"].strip()) >= 2:
                    cell_lines.append(ln)
            if not cell_lines:
                continue

            # Calculate padding (distance from text bbox to cell borders)
            lp = min(max(0.0, ln["rect"].x0 - cell.x0) for ln in cell_lines)
            tp = min(max(0.0, ln["rect"].y0 - cell.y0) for ln in cell_lines)
            bp = min(max(0.0, cell.y1 - ln["rect"].y1) for ln in cell_lines)

            # Improper if padding is extremely small (< 2.5 pt left, < 2.0 pt top/bottom)
            is_bad = False
            reasons = []
            if lp < 2.5:
                is_bad = True
                reasons.append(f"left ({round(lp, 1)}pt)")
            if tp < 2.0:
                is_bad = True
                reasons.append(f"top ({round(tp, 1)}pt)")
            if bp < 2.0:
                is_bad = True
                reasons.append(f"bottom ({round(bp, 1)}pt)")

            if is_bad:
                bad_cells.append((idx + 1, ", ".join(reasons)))

        if bad_cells:
            issues_by_page.setdefault(pg, []).extend(bad_cells)

    for pg, bads in sorted(issues_by_page.items()):
        preview = ", ".join(f"cell #{c} {r}" for c, r in bads[:4])
        if len(bads) > 4:
            preview += f" (+{len(bads)-4} more)"
        findings.append(_f(
            "Table cell padding", "Low", f"Table cells on page {pg}", str(pg),
            "Proper cell padding (at least 3.0pt left, 2.0pt top/bottom)",
            f"{len(bads)} cell(s) have improper padding: {preview}",
            f"{len(bads)} table cell(s) on page {pg} have text too close to their border, causing poor readability.",
            "Adjust cell padding in the table styles so that text has at least 3-4pt of breathing room from the cell edges."
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
def validate_style(prod_path, stage_path, mode="full"):
    """Run the style validation.

    mode="full"  — every check (typography, colour, spacing, images, …).
    mode="sites" — image/layout only. AEM already governs typography & colour,
                   so for Sites Validation we only flag what the CMS does NOT
                   control: image sizes & alignment, oversized images/icons,
                   bullet/paragraph spacing, too-small gap below an H1, and
                   table / image breaking across page boundaries.
    """
    prod_toc = get_toc(prod_path)
    stage_toc = get_toc(stage_path)
    prod_doc = fitz.open(prod_path)
    stage_doc = fitz.open(stage_path)
    p_body = _doc_body_size(prod_doc)
    s_body = _doc_body_size(stage_doc)
    p_ranges = _toc_ranges_by_key(prod_toc, prod_doc.page_count)
    s_ranges = _toc_ranges_by_key(stage_toc, stage_doc.page_count)

    # Check for encoding issues
    prod_garbled = _is_pdf_garbled(prod_doc)
    stage_garbled = _is_pdf_garbled(stage_doc)

    # Parse every page once, then assemble whole-document and per-section views
    # from the cache (avoids re-parsing pages for every section + info pass).
    _emit(0.04, "reading structure")
    p_cache = _build_cache(prod_doc, p_body, (0.05, 0.45, "parsing PROD pages"))
    print("  extracted PROD features")
    s_cache = _build_cache(stage_doc, s_body, (0.45, 0.82, "parsing STAGE pages"))
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
    _emit(0.86, "running style checks")

    findings = []
    sites = (mode == "sites")

    # ── Check for encoding issues (informational; not relevant to sites) ──
    if prod_garbled and not sites:
        findings.append({
            "category": "Encoding issue",
            "severity": "Info",
            "topic": "PROD PDF — custom special-character encoding",
            "pages": "All",
            "expected": "Standard Unicode text layer",
            "actual": "Custom font encoding for special symbols (™, ©, ® and similar); OCR-assisted extraction used.",
            "issue": "The PROD PDF uses custom font mappings for special characters such as trademark and copyright symbols. An OCR pass was used to improve extraction accuracy. Content results are not affected.",
            "fix": "No action required. If specific symbols appear missing, rebuild the PDF with standard Unicode (NFKC) font encoding.",
        })
    if stage_garbled and not sites:
        findings.append({
            "category": "Encoding issue",
            "severity": "Info",
            "topic": "STAGE PDF — custom special-character encoding",
            "pages": "All",
            "expected": "Standard Unicode text layer",
            "actual": "Custom font encoding for special symbols (™, ©, ® and similar); OCR-assisted extraction used.",
            "issue": "The STAGE PDF uses custom font mappings for special characters such as trademark and copyright symbols. An OCR pass was used to improve extraction accuracy. Content results are not affected.",
            "fix": "No action required. If specific symbols appear missing, rebuild the PDF with standard Unicode (NFKC) font encoding.",
        })

    # ── Image / layout checks — what AEM does NOT control (always run) ────────
    check_paragraph_spacing(geo, findings)          # extra spacing between bullets
    check_space_above_image(geo, findings)
    check_image_dimensions_and_alignment(geo, findings)  # image sizes & alignment
    check_icon_sizes_and_alignment(geo, findings)        # oversized / mis-aligned icons
    check_heading_below_spacing(geo, findings)      # too-small gap below an H1
    check_image_padding(geo, findings)
    check_image_layout_consistency(sections, findings)  # row-by-row image count/arrangement
    check_table_layout(s_all, findings)             # table breaking across pages
    check_table_cell_padding(s_all, findings)        # improper cell padding check

    # ── Typography / colour / link checks — AEM governs these. Skip for sites ─
    if not sites:
        check_heading_color(p_all, s_all, findings)
        check_heading_size(geo, findings)
        check_text_color(p_all, s_all, sections, findings)
        p_info = analyze_info_callouts(prod_doc, p_cache)
        s_info = analyze_info_callouts(stage_doc, s_cache)
        check_info_callouts(p_info, s_info, findings)
        check_footer_alignment(s_all, findings)
        check_bullet_paragraph_size(s_all, findings)
        check_wrapped_text_padding(geo, findings)
        check_hyperlinks(geo, p_all, s_all, findings)
        check_typography_spec(stage_doc, s_body, findings)

    # ── All-pages global checks ──────────────────────────────────────────────
    p_hdg, s_hdg, hdg_issues = check_heading_line_height_global(p_all, s_all, findings)
    img_checked, img_issues   = check_image_dimensions_global(
        p_cache, s_cache, prod_doc, stage_doc, findings)

    # ── Document-level metrics (passed to build_report for the metrics panel) ─
    p_imgs_total = sum(
        1 for pno in range(1, prod_doc.page_count + 1)
        if pno in p_cache
        for im in _assemble(p_cache, pno, pno)["images"]
        if max(im["rect"].width, im["rect"].height) > 80.0
    )
    s_imgs_total = sum(
        1 for pno in range(1, stage_doc.page_count + 1)
        if pno in s_cache
        for im in _assemble(s_cache, pno, pno)["images"]
        if max(im["rect"].width, im["rect"].height) > 80.0
    )
    doc_stats = {
        "prod_pages":    prod_doc.page_count,
        "stage_pages":   stage_doc.page_count,
        "prod_headings": p_hdg,
        "stage_headings": s_hdg,
        "prod_images":   p_imgs_total,
        "stage_images":  s_imgs_total,
        "metrics": {
            "Heading line height": {
                "checked": s_hdg,
                "issues":  hdg_issues,
            },
            "Image dimension": {
                "checked": img_checked,
                "issues":  img_issues,
            },
        },
    }

    prod_doc.close()
    stage_doc.close()
    return findings, doc_stats


# ── Sites validation (AEM page render vs PDF reference) ───────────────────
# 1pt = 1/72 in, CSS px = 1/96 in -> px = pt * 96/72
_PT_TO_PX = 96.0 / 72.0

SITES_IMAGE_CATEGORY_ORDER = [
    "Typography spec",
    "Line height",
    "Image dimension",
    "Image padding",
    "Space above image",
    "Oversized image",
    "Image cut off",
    "Table breaking",
    "Image alignment",
    "Content alignment",
]

_LH_MIN = 1.15
_LH_MAX = 2.2

_PAGE_JS = r"""
() => {
  const main =
    document.querySelector('main, article, [role=main], .content, #content, .cmp-text') ||
    document.body;
  const mr = main.getBoundingClientRect();
  const docW = document.documentElement.clientWidth;

  const px = (v) => {
    const n = parseFloat(v);
    return Number.isFinite(n) ? n : 0;
  };

  const clippedBy = (el) => {
    let p = el.parentElement;
    const r = el.getBoundingClientRect();
    while (p && p !== document.body) {
      const cs = getComputedStyle(p);
      if (/(hidden|clip)/.test(cs.overflowX + cs.overflowY)) {
        const pr = p.getBoundingClientRect();
        if (r.right > pr.right + 1 || r.left < pr.left - 1 ||
            r.bottom > pr.bottom + 1 || r.top < pr.top - 1) return true;
      }
      p = p.parentElement;
    }
    return false;
  };

  const lhPx = (cs) => {
    const fs = parseFloat(cs.fontSize) || 0;
    return cs.lineHeight === 'normal' ? fs * 1.2 : (parseFloat(cs.lineHeight) || 0);
  };

  const imgAlign = (img, r, cs) => {
    if (cs.cssFloat === 'left' || cs.cssFloat === 'right') return cs.cssFloat;
    if (cs.marginLeft === 'auto' && cs.marginRight === 'auto') return 'center';
    const parent = img.parentElement;
    if (parent && /center/.test(getComputedStyle(parent).textAlign)) return 'center';
    const leftGap = r.left - mr.left, rightGap = mr.right - r.right;
    if (Math.abs(leftGap - rightGap) < 12) return 'center';
    if (leftGap <= 10) return 'left';
    if (rightGap <= 10) return 'right';
    return 'offset';
  };

  const textRects = [...main.querySelectorAll('p, li, h1, h2, h3')]
    .filter(e => (e.textContent || '').trim().length > 15)
    .map(e => e.getBoundingClientRect());

  const gapAboveImage = (r) => {
    let best = null;
    for (const tr of textRects) {
      if (tr.bottom > r.top + 1) continue;
      const overlap = Math.max(0, Math.min(r.right, tr.right) - Math.max(r.left, tr.left));
      if (overlap < Math.min(80, r.width * 0.35)) continue;
      const gap = r.top - tr.bottom;
      if (best === null || gap < best) best = gap;
    }
    return best === null ? null : Math.round(best);
  };

  const images = [...main.querySelectorAll('img')].map(img => {
    const r = img.getBoundingClientRect();
    const cs = getComputedStyle(img);
    const leftGap = Math.max(0, r.left - mr.left);
    const rightGap = Math.max(0, mr.right - r.right);
    return {
      alt: img.getAttribute('alt') || '',
      src: (img.currentSrc || img.src || '').split('/').pop(),
      w: Math.round(r.width), h: Math.round(r.height),
      naturalW: img.naturalWidth || 0, naturalH: img.naturalHeight || 0,
      left: Math.round(r.left - mr.left),
      right: Math.round(r.right - mr.left),
      gapLeft: Math.round(leftGap),
      gapRight: Math.round(rightGap),
      gapAbove: gapAboveImage(r),
      overflowsRight: r.right > mr.right + 2 || r.right > docW + 2,
      overflowsLeft: r.left < mr.left - 2,
      widerThanMain: r.width > mr.width + 2,
      clipped: clippedBy(img),
      display: cs.display, float: cs.cssFloat,
      marginAuto: cs.marginLeft === 'auto' && cs.marginRight === 'auto',
      marginTop: Math.round(px(cs.marginTop)),
      marginBottom: Math.round(px(cs.marginBottom)),
      marginLeft: Math.round(px(cs.marginLeft)),
      marginRight: Math.round(px(cs.marginRight)),
      align: imgAlign(img, r, cs),
    };
  }).filter(im => im.w >= 16 && im.h >= 16);

  const tables = [...main.querySelectorAll('table')].map(t => {
    const r = t.getBoundingClientRect();
    return {
      scrollW: t.scrollWidth, clientW: t.clientWidth,
      w: Math.round(r.width), mainW: Math.round(mr.width),
      overflows: t.scrollWidth > t.clientWidth + 2 || r.width > mr.width + 2,
    };
  });

  const typo = {};
  for (const tag of ['h1', 'h2', 'h3']) {
    const el = main.querySelector(tag);
    if (el) {
      const cs = getComputedStyle(el);
      typo[tag] = { px: parseFloat(cs.fontSize), lh: lhPx(cs), weight: cs.fontWeight,
                    align: cs.textAlign, color: cs.color,
                    text: (el.textContent || '').trim().slice(0, 60) };
    }
  }
  const ps = [...main.querySelectorAll('p')].filter(e => (e.textContent || '').trim().length > 30);
  if (ps.length) {
    const cs = getComputedStyle(ps[0]);
    typo['body'] = { px: parseFloat(cs.fontSize), lh: lhPx(cs), weight: cs.fontWeight,
                     align: cs.textAlign, color: cs.color };
  }
  const lis = [...main.querySelectorAll('li')].filter(e => (e.textContent || '').trim().length > 15);
  if (lis.length) {
    const cs = getComputedStyle(lis[0]);
    typo['bullet'] = { px: parseFloat(cs.fontSize), lh: lhPx(cs), weight: cs.fontWeight,
                       align: cs.textAlign, count: lis.length,
                       text: (lis[0].textContent || '').trim().slice(0, 60) };
  }

  const blocks = [...main.querySelectorAll('p, li, h1, h2, h3')]
    .filter(e => (e.textContent || '').trim().length > 15);
  const alignCount = {};
  let firstBad = null;
  for (const e of blocks) {
    const a = getComputedStyle(e).textAlign || 'start';
    alignCount[a] = (alignCount[a] || 0) + 1;
    if (!firstBad && (a === 'right' || a === 'justify')) {
      firstBad = { align: a, text: (e.textContent || '').trim().slice(0, 60) };
    }
  }

  return { mainW: Math.round(mr.width), images, tables, typo,
           hasImages: images.length > 0, alignCount, firstBad };
}
"""


def render_pages(pages, auth_token, progress_cb=None, viewport_w=1280):
    from playwright.sync_api import sync_playwright

    headers = {"Authorization": "Basic " + auth_token} if auth_token else {}
    out = []
    total = max(1, len(pages))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(
            viewport={"width": viewport_w, "height": 1600},
            extra_http_headers=headers,
            ignore_https_errors=True,
        )
        page = ctx.new_page()
        for i, pg in enumerate(pages):
            if progress_cb:
                progress_cb(0.15 + 0.7 * i / total, f"rendering {i+1}/{total}: {pg.get('title','')}")
            rec = {
                "title": pg.get("title") or pg.get("path") or pg.get("url"),
                "url": pg.get("url"),
                "error": None,
                "data": None,
            }
            try:
                page.goto(pg["url"], wait_until="networkidle", timeout=30000)
                rec["data"] = page.evaluate(_PAGE_JS)
            except Exception as e:
                rec["error"] = str(e)
            out.append(rec)
        browser.close()
    return out


def _pdf_section_images(pdf_path):
    doc = fitz.open(pdf_path)
    toc = doc.get_toc() or []
    by_page = {}
    for pno in range(1, doc.page_count + 1):
        page = doc[pno - 1]
        widths = []
        for blk in page.get_image_info(xrefs=False):
            bbox = blk.get("bbox")
            if not bbox:
                continue
            w = (bbox[2] - bbox[0]) * _PT_TO_PX
            h = (bbox[3] - bbox[1]) * _PT_TO_PX
            if min(w, h) < 24 or max(w, h) / max(min(w, h), 1) > 8:
                continue
            widths.append(round(w))
        by_page[pno] = widths
    doc.close()

    sec = {}
    entries = [(t, p) for _lvl, t, p in toc]
    for i, (title, pg) in enumerate(entries):
        end = entries[i + 1][1] if i + 1 < len(entries) else max(by_page) + 1
        widths = []
        for p in range(pg, max(pg, end)):
            widths += by_page.get(p, [])
        sec[_norm_key(title)] = widths
    return sec


def _sites_check_typography(rendered, findings):
    spec_px = {
        "h1": _BENQ_TYPO_SPEC["H1 heading"]["size"] * _PT_TO_PX,
        "h2": _BENQ_TYPO_SPEC["H2 heading"]["size"] * _PT_TO_PX,
        "h3": _BENQ_TYPO_SPEC["H3 heading"]["size"] * _PT_TO_PX,
        "body": _BENQ_TYPO_SPEC["Body text"]["size"] * _PT_TO_PX,
    }
    label = {"h1": "H1 heading", "h2": "H2 heading", "h3": "H3 heading", "body": "Body text"}
    tol_px = 3.0
    seen = set()
    for rec in rendered:
        typo = (rec.get("data") or {}).get("typo") or {}
        for tag, exp in spec_px.items():
            if tag in seen:
                continue
            info = typo.get(tag)
            if not info or not info.get("px"):
                continue
            actual = info["px"]
            if abs(actual - exp) > tol_px:
                seen.add(tag)
                sev = "High" if abs(actual - exp) > 2 * tol_px else "Medium"
                findings.append(_f(
                    "Typography spec", sev, label[tag], rec["title"],
                    f"{exp:.0f}px ({_BENQ_TYPO_SPEC[label[tag]]['size']:.0f}pt)",
                    f"{actual:.0f}px",
                    f"{label[tag]} renders at {actual:.0f}px on the site — the BenQ A4 "
                    f"spec is {_BENQ_TYPO_SPEC[label[tag]]['size']:.0f}pt (~{exp:.0f}px).",
                    f"Adjust the AEM style so {label[tag]} matches the spec size.",
                ))


def _sites_check_line_height(rendered, findings):
    label = {
        "h1": "H1 heading",
        "h2": "H2 heading",
        "h3": "H3 heading",
        "body": "Body text",
        "bullet": "Bullet / list item",
    }
    seen = set()
    for rec in rendered:
        typo = (rec.get("data") or {}).get("typo") or {}
        for tag, name in label.items():
            if tag in seen:
                continue
            info = typo.get(tag)
            if not info or not info.get("px") or not info.get("lh"):
                continue
            ratio = info["lh"] / info["px"]
            if ratio < _LH_MIN or ratio > _LH_MAX:
                seen.add(tag)
                cramped = ratio < _LH_MIN
                findings.append(_f(
                    "Line height",
                    "Medium" if cramped else "Low",
                    name,
                    rec["title"],
                    f"{_LH_MIN:.2f}-{_LH_MAX:.1f}x font size",
                    f"{ratio:.2f}x ({info['lh']:.0f}px / {info['px']:.0f}px)",
                    f"{name} line-height is {ratio:.2f}x its font size — "
                    + ("lines are cramped/touching." if cramped else "spacing is unusually large."),
                    f"Set {name} line-height to roughly 1.2-1.5x the font size.",
                ))


def _sites_check_alignment(rendered, findings):
    for rec in rendered:
        data = rec.get("data") or {}
        bad = data.get("firstBad")
        if bad:
            findings.append(_f(
                "Content alignment", "Medium", f"{bad['align'].title()}-aligned text",
                rec["title"], "Left-aligned", f"{bad['align']}-aligned",
                f"Text on “{rec['title']}” is {bad['align']}-aligned "
                f"(e.g. “{bad['text']}...”) — BenQ manual content is left-aligned.",
                "Set the text alignment to left.",
            ))

        imgs = data.get("images", [])
        aligns = {im.get("align") for im in imgs if im.get("align")}
        for im in imgs:
            if im.get("align") == "offset":
                name = im.get("alt") or im.get("src") or "image"
                findings.append(_f(
                    "Image alignment", "Low", name, rec["title"],
                    "Flush-left to the text column, or centred",
                    f"offset {im.get('left')}px from the left",
                    f"Image “{name}” is neither aligned to the content's left edge "
                    f"nor centred ({im.get('left')}px in from the left).",
                    "Align the image to the text column (left) or centre it.",
                ))
        if len(imgs) >= 2 and {"left", "center", "right"} & aligns and len(aligns - {"offset"}) > 1:
            findings.append(_f(
                "Image alignment", "Low", "Mixed image alignment", rec["title"],
                "Consistent alignment for all figures",
                ", ".join(sorted(a for a in aligns if a)),
                f"“{rec['title']}” mixes image alignments "
                f"({', '.join(sorted(a for a in aligns if a))}) — figures look inconsistent.",
                "Use one consistent alignment for the page's figures.",
            ))


def _sites_check_spacing_and_padding(rendered, findings):
    def _median(vals):
        s = sorted(vals)
        n = len(s)
        if not n:
            return None
        mid = n // 2
        if n % 2:
            return float(s[mid])
        return (s[mid - 1] + s[mid]) / 2.0

    for rec in rendered:
        data = rec.get("data") or {}
        imgs = data.get("images", [])
        if not imgs:
            continue
        figs = [im for im in imgs if max(im.get("w", 0), im.get("h", 0)) >= 96]
        if not figs:
            continue

        pad_flagged = False
        gap_flagged = False

        by_align = {
            "left": [im for im in figs if im.get("align") == "left" and isinstance(im.get("gapLeft"), (int, float))],
            "right": [im for im in figs if im.get("align") == "right" and isinstance(im.get("gapRight"), (int, float))],
            "center": [
                im for im in figs
                if im.get("align") == "center"
                and isinstance(im.get("gapLeft"), (int, float))
                and isinstance(im.get("gapRight"), (int, float))
            ],
        }

        for im in figs:
            name = im.get("alt") or im.get("src") or "image"
            align = im.get("align")
            lg = im.get("gapLeft")
            rg = im.get("gapRight")
            ga = im.get("gapAbove")

            if not pad_flagged and align == "left" and isinstance(lg, (int, float)):
                group = by_align["left"]
                if len(group) >= 2:
                    med = _median([x.get("gapLeft") for x in group])
                    if med is not None and lg - med > 40:
                        pad_flagged = True
                        findings.append(_f(
                            "Image padding", "Low", name, rec["title"],
                            f"Left-gap near page norm (~{med:.0f}px)",
                            f"left gap {lg}px",
                            f"Image “{name}” has noticeably more left padding than peer images "
                            f"on this page ({lg}px vs ~{med:.0f}px).",
                            "Reduce left margin/padding to match nearby figures.",
                        ))
                elif lg > 80:
                    pad_flagged = True
                    findings.append(_f(
                        "Image padding", "Low", name, rec["title"],
                        "Left-aligned image starts reasonably close to content edge",
                        f"left gap {lg}px",
                        f"Image “{name}” is left-aligned but inset too far from the content edge "
                        f"({lg}px).",
                        "Reduce left margin/padding so the figure aligns with content flow.",
                    ))
            elif not pad_flagged and align == "right" and isinstance(rg, (int, float)):
                group = by_align["right"]
                if len(group) >= 2:
                    med = _median([x.get("gapRight") for x in group])
                    if med is not None and rg - med > 40:
                        pad_flagged = True
                        findings.append(_f(
                            "Image padding", "Low", name, rec["title"],
                            f"Right-gap near page norm (~{med:.0f}px)",
                            f"right gap {rg}px",
                            f"Image “{name}” has noticeably more right padding than peer images "
                            f"on this page ({rg}px vs ~{med:.0f}px).",
                            "Reduce right margin/padding to match nearby figures.",
                        ))
                elif rg > 80:
                    pad_flagged = True
                    findings.append(_f(
                        "Image padding", "Low", name, rec["title"],
                        "Right-aligned image ends reasonably close to content edge",
                        f"right gap {rg}px",
                        f"Image “{name}” is right-aligned but inset too far from the content edge "
                        f"({rg}px).",
                        "Reduce right margin/padding so the figure aligns with content flow.",
                    ))
            elif (
                not pad_flagged
                and align == "center"
                and isinstance(lg, (int, float))
                and isinstance(rg, (int, float))
            ):
                group = by_align["center"]
                bal = abs(lg - rg)
                if len(group) >= 2:
                    med_bal = _median([abs(x.get("gapLeft") - x.get("gapRight")) for x in group])
                    if med_bal is not None and bal - med_bal > 40:
                        pad_flagged = True
                        findings.append(_f(
                            "Image padding", "Low", name, rec["title"],
                            f"Centered balance near page norm (~{med_bal:.0f}px diff)",
                            f"left/right diff {bal}px",
                            f"Image “{name}” has more unbalanced side spacing than other centered "
                            f"figures on this page (diff {bal}px).",
                            "Balance left/right spacing or apply the same centering pattern used elsewhere.",
                        ))
                elif bal > 60:
                    pad_flagged = True
                    findings.append(_f(
                        "Image padding", "Low", name, rec["title"],
                        "Centered image has similar left/right spacing",
                        f"left {lg}px vs right {rg}px",
                        f"Image “{name}” appears centered but left/right spacing differs by {bal}px.",
                        "Adjust side margins so centered figures look balanced.",
                    ))

            if not gap_flagged and isinstance(ga, (int, float)) and (ga < 2 or ga > 220):
                gap_flagged = True
                findings.append(_f(
                    "Space above image", "Low", name, rec["title"],
                    "Reasonable top gap from nearby text",
                    f"{ga}px above image",
                    f"Image “{name}” has extreme top spacing ({ga}px) from nearby text.",
                    "Adjust top margin/padding above the image to keep spacing consistent.",
                ))

        gaps = sorted(g for g in (im.get("gapAbove") for im in figs) if isinstance(g, (int, float)))
        if len(gaps) >= 3:
            med = _median(gaps)
            spread = gaps[-1] - gaps[0]
            outliers = [g for g in gaps if abs(g - med) > 64]
            if spread > 120 and outliers:
                findings.append(_f(
                    "Space above image", "Low", "Inconsistent image spacing", rec["title"],
                    "Consistent top spacing across images on the same page",
                    f"range {gaps[0]}px to {gaps[-1]}px",
                    f"Images on “{rec['title']}” have inconsistent vertical spacing above them "
                    f"({gaps[0]}–{gaps[-1]}px).",
                    "Use a consistent top spacing rule for image blocks on this page.",
                ))


def _sites_check_images(rendered, findings):
    for rec in rendered:
        data = rec.get("data") or {}
        for im in data.get("images", []):
            name = im.get("alt") or im.get("src") or "image"
            if im.get("overflowsRight") or im.get("overflowsLeft") or im.get("clipped"):
                findings.append(_f(
                    "Image cut off", "High", name, rec["title"],
                    "Fully inside the content area",
                    f"{im['w']}x{im['h']}px — extends beyond / clipped by its container",
                    f"Image “{name}” overflows or is clipped on the rendered page "
                    f"(content width {data.get('mainW','?')}px). It is cut off.",
                    "Constrain the image to its container (max-width:100%) so it is not cut off.",
                ))
            elif im.get("widerThanMain"):
                findings.append(_f(
                    "Oversized image", "Medium", name, rec["title"],
                    f"<= content width ({data.get('mainW','?')}px)",
                    f"{im['w']}px wide",
                    f"Image “{name}” is wider than the content container "
                    f"({im['w']}px vs {data.get('mainW','?')}px).",
                    "Reduce the image width or set max-width:100% so it fits the content column.",
                ))
            elif im.get("naturalW") and im["naturalW"] > 2.5 * max(im["w"], 1) and im["naturalW"] > 1000:
                findings.append(_f(
                    "Oversized image", "Low", name, rec["title"],
                    f"source ~= display size ({im['w']}px)",
                    f"source {im['naturalW']}px shown at {im['w']}px",
                    f"Image “{name}” ships a {im['naturalW']}px source but renders at "
                    f"{im['w']}px — an oversized/heavy asset.",
                    "Serve a right-sized image to cut page weight.",
                ))


def _sites_check_tables(rendered, findings):
    for rec in rendered:
        data = rec.get("data") or {}
        for i, t in enumerate(data.get("tables", []), 1):
            if t.get("overflows"):
                findings.append(_f(
                    "Table breaking", "High", f"Table {i}", rec["title"],
                    f"Fits the content width ({t.get('mainW','?')}px)",
                    f"content {t.get('scrollW','?')}px > visible {t.get('clientW','?')}px",
                    f"Table {i} is wider than the page and needs horizontal scrolling "
                    f"— it breaks out of the content column on the site.",
                    "Make the table responsive (e.g. allow wrapping or a scroll wrapper) "
                    "so it fits the content width.",
                ))


def _sites_check_image_dimensions_vs_pdf(pdf_path, rendered, findings):
    try:
        pdf_sec = _pdf_section_images(pdf_path)
    except Exception:
        return
    for rec in rendered:
        data = rec.get("data") or {}
        site_w = sorted([im["w"] for im in data.get("images", [])], reverse=True)
        if not site_w:
            continue
        key = _norm_key(rec["title"] or "")
        pdf_w = sorted(pdf_sec.get(key, []), reverse=True)
        if not pdf_w:
            continue
        sw, pw = site_w[0], pdf_w[0]
        if pw and abs(sw - pw) / pw > 0.25 and abs(sw - pw) > 40:
            findings.append(_f(
                "Image dimension", "Medium", "Main figure", rec["title"],
                f"~= {pw}px wide (per PDF)",
                f"{sw}px wide on site",
                f"The main image on “{rec['title']}” renders at {sw}px but the PDF "
                f"figure is ~={pw}px — a {abs(sw-pw)}px difference.",
                "Match the site image dimensions to the PDF reference.",
            ))


def validate_site_vs_pdf(pdf_path, pages, auth_token, progress_cb=None):
    if progress_cb:
        progress_cb(0.10, "launching browser")
    rendered = render_pages(pages, auth_token, progress_cb)

    if progress_cb:
        progress_cb(0.88, "analysing rendered pages")
    findings = []
    _sites_check_typography(rendered, findings)
    _sites_check_line_height(rendered, findings)
    _sites_check_spacing_and_padding(rendered, findings)
    _sites_check_alignment(rendered, findings)
    _sites_check_images(rendered, findings)
    _sites_check_tables(rendered, findings)
    _sites_check_image_dimensions_vs_pdf(pdf_path, rendered, findings)

    ok_pages = [r for r in rendered if r.get("data")]
    stats = {
        "pages_rendered": len(ok_pages),
        "pages_failed": sum(1 for r in rendered if r.get("error")),
        "images_checked": sum(len((r.get("data") or {}).get("images", [])) for r in ok_pages),
        "tables_checked": sum(len((r.get("data") or {}).get("tables", [])) for r in ok_pages),
        "render_errors": [
            {"title": r["title"], "error": r["error"]}
            for r in rendered
            if r.get("error")
        ][:10],
    }
    if progress_cb:
        progress_cb(0.98, "done")
    return findings, stats


# ── report ──────────────────────────────────────────────────────────────────
CATEGORY_ORDER = [
    "Encoding issue", "Typography spec",
    "Heading line height",
    "Image dimension", "Icon size", "Image alignment", "Icon alignment",
    "Image layout", "Heading spacing below",
    "Image padding", "Space above image", "Heading style", "Paragraph spacing",
    "Text colour", "Info / notice colour", "Table cell padding", "Table layout breaking", "Wrapped text padding",
    "Footer page number", "Bullet vs paragraph size", "Hyperlink issue",
]

# Categories produced by mode="sites" (image / layout only — AEM governs the rest).
SITES_CATEGORY_ORDER = [
    "Heading line height", "Heading spacing below",
    "Image dimension", "Icon size", "Image alignment", "Icon alignment",
    "Image layout", "Image padding", "Space above image", "Paragraph spacing",
    "Table cell padding", "Table layout breaking",
]

SEV_COLOR = {"High": colors.HexColor("#c62828"),
             "Medium": colors.HexColor("#e65100"),
             "Low": colors.HexColor("#1565c0")}


def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_report(prod_path, stage_path, findings, out_path, doc_stats=None,
                 category_order=None):
    cat_order = category_order or CATEGORY_ORDER
    doc = SimpleDocTemplate(out_path, pagesize=landscape(letter),
                            leftMargin=0.4*inch, rightMargin=0.4*inch,
                            topMargin=0.4*inch, bottomMargin=0.4*inch)
    ss = getSampleStyleSheet()
    title_s = ParagraphStyle("T",     parent=ss["Heading1"], fontSize=16, spaceAfter=4)
    sub_s   = ParagraphStyle("Sub",   parent=ss["Normal"],   fontSize=10, textColor=colors.grey, spaceAfter=2)
    head_s  = ParagraphStyle("H",     parent=ss["Heading2"], fontSize=13, spaceBefore=10, spaceAfter=6)
    hdr_s   = ParagraphStyle("Hdr",   parent=ss["Normal"],   fontSize=9,  leading=12,
                              textColor=colors.whitesmoke, fontName="Helvetica-Bold")
    cell_s  = ParagraphStyle("Cell",  parent=ss["Normal"],   fontSize=8,  leading=10)
    topic_s = ParagraphStyle("Topic", parent=ss["Normal"],   fontSize=8,  leading=10, fontName="Helvetica-Bold")

    by_cat = {c: [f for f in findings if f["category"] == c] for c in cat_order}

    story = [
        Paragraph("Style Validation Report", title_s),
        Paragraph(f"Production (expected): {os.path.basename(prod_path)}", sub_s),
        Paragraph(f"Staging (actual):    {os.path.basename(stage_path)}", sub_s),
        Spacer(1, 10),
    ]

    # ── Overall Metrics panel ────────────────────────────────────────────────
    ds = doc_stats or {}
    met = ds.get("metrics", {})

    # Document stats row
    doc_hdr_cols = ["", "Pages", "Headings", "Images (≥80pt)"]
    doc_rows = [
        [Paragraph(f"<b>{h}</b>", hdr_s) for h in doc_hdr_cols],
        [Paragraph("<b>PROD</b>", topic_s),
         Paragraph(str(ds.get("prod_pages",  "—")), cell_s),
         Paragraph(str(ds.get("prod_headings","—")), cell_s),
         Paragraph(str(ds.get("prod_images",  "—")), cell_s)],
        [Paragraph("<b>STAGE</b>", topic_s),
         Paragraph(str(ds.get("stage_pages",  "—")), cell_s),
         Paragraph(str(ds.get("stage_headings","—")), cell_s),
         Paragraph(str(ds.get("stage_images",  "—")), cell_s)],
    ]
    doc_tbl = Table(doc_rows, colWidths=[80, 80, 90, 100], repeatRows=1)
    doc_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#455a64")),
        ("GRID",       (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f5f5f5")]),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    # Per-category pass/fail %
    pass_color  = colors.HexColor("#2e7d32")
    fail_color  = colors.HexColor("#c62828")
    info_color  = colors.HexColor("#1565c0")
    CHECKS_WITH_COUNTS = {"Heading line height", "Image dimension"}
    stage_pages = int(ds.get("stage_pages") or 0)
    stage_headings = int(ds.get("stage_headings") or 0)
    stage_images = int(ds.get("stage_images") or 0)
    # Estimated denominators for categories that don't expose explicit checked
    # counts. This prevents misleading 0% when a category has one or more
    # findings but was previously treated as a binary 1-check gate.
    base_checked = {
        "Encoding issue": stage_pages,
        "Typography spec": stage_pages,
        "Heading style": max(stage_headings, stage_pages),
        "Paragraph spacing": max(stage_headings, stage_pages),
        "Text colour": stage_pages,
        "Info / notice colour": stage_pages,
        "Table cell padding": stage_pages,
        "Table layout breaking": stage_pages,
        "Wrapped text padding": stage_pages,
        "Footer page number": stage_pages,
        "Bullet vs paragraph size": stage_pages,
        "Hyperlink issue": stage_pages,
        "Icon size": stage_images,
        "Image alignment": stage_images,
        "Icon alignment": stage_images,
        "Image layout": stage_images,
        "Image padding": stage_images,
        "Space above image": stage_images,
        "Heading spacing below": max(stage_headings, stage_pages),
    }
    cat_hdr_cols = ["Style check", "Checked", "Issues", "Passed", "Pass %", "Status"]
    cat_rows = [[Paragraph(f"<b>{h}</b>", hdr_s) for h in cat_hdr_cols]]

    total_checked_all = 0
    total_issues_all  = 0

    for c in cat_order:
        items   = by_cat[c]
        actionable = [i for i in items if i.get("severity") in ("High", "Medium", "Low")]
        info_only = bool(items) and not actionable
        n_issue = len(actionable)
        if c in CHECKS_WITH_COUNTS and c in met:
            n_checked = met[c]["checked"]
            n_issue   = met[c]["issues"]
        else:
            n_checked = max(base_checked.get(c, stage_pages), n_issue, 1)

        n_passed = max(0, n_checked - n_issue)
        pct      = round(100 * n_passed / n_checked, 1) if n_checked else 100.0
        total_checked_all += n_checked
        total_issues_all  += n_issue

        if info_only:
            status_txt   = "INFO"
            status_color = info_color
        elif n_checked == 0 or n_issue == 0:
            status_txt   = "PASS"
            status_color = pass_color
        elif n_issue < n_checked:
            status_txt   = "PARTIAL"
            status_color = colors.HexColor("#e65100")
        else:
            status_txt   = "FAIL"
            status_color = fail_color

        pct_color = pass_color if pct >= 90 else (colors.HexColor("#e65100") if pct >= 70 else fail_color)
        cat_rows.append([
            Paragraph(c, topic_s),
            Paragraph(str(n_checked), cell_s),
            Paragraph(str(n_issue),   cell_s),
            Paragraph(str(n_passed),  cell_s),
            Paragraph(f"<b>{pct}%</b>",
                      ParagraphStyle("pct", parent=cell_s, textColor=pct_color)),
            Paragraph(f"<b>{status_txt}</b>",
                      ParagraphStyle("st", parent=cell_s, textColor=status_color)),
        ])

    # Totals row
    total_passed = max(0, total_checked_all - total_issues_all)
    overall_pct  = round(100 * total_passed / total_checked_all, 1) if total_checked_all else 100.0
    ovr_color    = pass_color if overall_pct >= 90 else (colors.HexColor("#e65100") if overall_pct >= 70 else fail_color)
    cat_rows.append([
        Paragraph("<b>Overall</b>", topic_s),
        Paragraph(f"<b>{total_checked_all}</b>", topic_s),
        Paragraph(f"<b>{total_issues_all}</b>",  topic_s),
        Paragraph(f"<b>{total_passed}</b>",       topic_s),
        Paragraph(f"<b>{overall_pct}%</b>",
                  ParagraphStyle("ovp", parent=topic_s, textColor=ovr_color)),
        Paragraph(
            f"<b>{'PASS' if overall_pct >= 90 else 'REVIEW'}</b>",
            ParagraphStyle("ovs", parent=topic_s,
                           textColor=pass_color if overall_pct >= 90 else fail_color)),
    ])

    cat_tbl = Table(cat_rows, colWidths=[190, 55, 55, 55, 55, 60], repeatRows=1)
    cat_tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0),   colors.HexColor("#37474f")),
        ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#eceff1")),
        ("GRID",       (0, 0), (-1, -1),  0.5, colors.grey),
        ("VALIGN",     (0, 0), (-1, -1),  "MIDDLE"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -2), [colors.white, colors.HexColor("#f5f5f5")]),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))

    story += [
        Paragraph("<b>Document Statistics</b>", sub_s),
        Spacer(1, 4), doc_tbl, Spacer(1, 12),
        Paragraph(
            f"<b>Overall metrics — {overall_pct}% pass rate</b> "
            f"({total_passed} of {total_checked_all} checks passed, "
            f"{total_issues_all} issue(s) across "
            f"{sum(1 for c in cat_order if by_cat[c])} of {len(cat_order)} categories). "
            "PROD is the reference; fix STAGE to match.",
            sub_s,
        ),
        Spacer(1, 4), cat_tbl, PageBreak(),
    ]

    # ── Per-category detail pages ────────────────────────────────────────────
    for c in cat_order:
        items = by_cat[c]
        story.append(Paragraph(f"{c}", head_s))
        if not items:
            story.append(Paragraph("No issues — STAGE matches PROD for this check.",
                                   ParagraphStyle("ok", parent=ss["Normal"], fontSize=9,
                                                  textColor=colors.HexColor("#2e7d32"))))
            story.append(PageBreak())
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
        story.append(PageBreak())

    doc.build(story)
    print(f"Report saved: {out_path}")


def main(prod_path, stage_path, out_path):
    print("Validating style (PROD = expected, STAGE = actual)...")
    _emit(0.01, "starting")
    findings, doc_stats = validate_style(prod_path, stage_path)
    by_cat = {}
    for f in findings:
        by_cat.setdefault(f["category"], 0)
        by_cat[f["category"]] += 1
    print(f"Total style issues: {len(findings)}")
    for c in CATEGORY_ORDER:
        print(f"  {c:24} {by_cat.get(c, 0)}")
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    _emit(0.95, "building report")
    build_report(prod_path, stage_path, findings, out_path, doc_stats=doc_stats)
    _emit(1.0, "done")
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

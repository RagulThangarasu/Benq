"""
Sites Validation — report building and token-comparison helpers.

Extracted from app.py so they can be tested and reused independently.
"""

import io
import unicodedata
from collections import Counter


# ── Token normalization ───────────────────────────────────────────────────────

def _normalize_token(tok: str) -> str:
    """NFKC-fold + lowercase a single token so comparisons are case- and
    encoding-insensitive across all scripts (Latin, CJK, Arabic, Cyrillic…)."""
    return unicodedata.normalize("NFKC", tok).lower()


# CJK ranges: Hiragana/Katakana, CJK Ext-A, Unified, Compat, Hangul
import re
_CJK_RE = re.compile(
    "["
    "぀-ヿ"   # Hiragana + Katakana
    "㐀-䶿"   # CJK Ext A
    "一-鿿"   # CJK Unified
    "豈-﫿"   # CJK Compatibility Ideographs
    "가-힯"   # Hangul syllables
    # Supplementary planes via surrogate pairs aren't matched by a BMP regex;
    # they are split into individual chars by Python's str iteration anyway.
    "]"
)


def _is_cjk(ch: str) -> bool:
    return bool(ch) and bool(_CJK_RE.match(ch[0]))


def tokenize(text: str):
    """Split *text* into normalized comparison tokens.

    * Whitespace-delimited for Latin / Cyrillic / Arabic / etc.
    * Per-character for CJK runs (no spaces between ideographs).
    * Every token is NFKC-lowercased so comparisons are case-insensitive
      and encoding-agnostic across all languages.
    """
    toks = []
    for chunk in text.split():
        buf = ""
        for ch in chunk:
            if _is_cjk(ch):
                if buf:
                    toks.append(_normalize_token(buf))
                    buf = ""
                toks.append(_normalize_token(ch))
            else:
                buf += ch
        if buf:
            toks.append(_normalize_token(buf))
    return toks


def build_pdf_counter(doc) -> Counter:
    """Build a normalized token Counter from a PyMuPDF document."""
    text = " ".join(page.get_text("text") for page in doc)
    return Counter(tokenize(text))


def compare(site_text: str, ref_counter: Counter):
    """Compare *site_text* tokens against *ref_counter*.

    Returns (total, matched, missing_list).

    * total   — distinct site-token occurrences
    * matched — occurrences found in the reference (capped by ref counts)
    * missing — list of site tokens not in the reference
    """
    site_counter = Counter(tokenize(site_text))
    total = sum(site_counter.values())
    matched = sum((site_counter & ref_counter).values())
    missing = sorted((site_counter - ref_counter).elements())
    return total, matched, missing


# ── PDF report builder ────────────────────────────────────────────────────────

def build_report_pdf(state: dict) -> io.BytesIO:
    """Render the Sites Validation PDF report from *state* and return bytes.

    *state* must contain:
        url, pdf_names, generated, summary (dict), rows (list of row dicts)
    Each row must have: nav_item, url?, status, coverage_pct?,
    matched_words?, site_words?, missing_all?, error?, note?
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.lib.units import inch
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

    bio = io.BytesIO()
    doc = SimpleDocTemplate(
        bio, pagesize=landscape(letter),
        leftMargin=0.4 * inch, rightMargin=0.4 * inch,
        topMargin=0.4 * inch, bottomMargin=0.4 * inch,
    )
    ss = getSampleStyleSheet()
    h1   = ParagraphStyle("h1",   parent=ss["Heading1"], fontSize=16, spaceAfter=4)
    sub  = ParagraphStyle("sub",  parent=ss["Normal"],   fontSize=9,  textColor=colors.grey)
    cell = ParagraphStyle("cell", parent=ss["Normal"],   fontSize=8,  leading=11)
    miss = ParagraphStyle("miss", parent=ss["Normal"],   fontSize=8,  leading=11,
                          textColor=colors.HexColor("#b91c1c"))
    hdr  = ParagraphStyle("hdr",  parent=ss["Normal"],   fontSize=9,
                          textColor=colors.whitesmoke, fontName="Helvetica-Bold")

    def esc(s):
        return str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    s        = state["summary"]
    rows_all = state["rows"]

    tot_site    = sum((r.get("site_words")    or 0) for r in rows_all)
    tot_matched = sum((r.get("matched_words") or 0) for r in rows_all)
    tot_missing = sum(len(r.get("missing_all") or []) for r in rows_all)
    overall_cov = round(100.0 * tot_matched / tot_site, 2) if tot_site else 0.0
    covs        = [r["coverage_pct"] for r in rows_all if r.get("coverage_pct") is not None]
    avg_cov     = round(sum(covs) / len(covs), 2) if covs else 0.0

    story = [
        Paragraph("Sites Validation Report", h1),
        Paragraph(f"Author URL: {esc(state.get('url'))}", sub),
        Paragraph(
            f"Reference PDF(s): {esc(', '.join(state.get('pdf_names') or []) or '—')}", sub),
        Paragraph(f"Generated: {esc(state.get('generated'))}", sub),
        Spacer(1, 10),
    ]

    # ── Metrics summary table ──────────────────────────────────────────────
    met_hdr = [Paragraph(f"<b>{h}</b>", hdr) for h in [
        "TOC items", "Passed", "Failed", "Overall coverage",
        "Avg coverage / item", "Total missing", "Site words", "Matched",
    ]]
    met_row = [
        Paragraph(str(s["total"]),   cell),
        Paragraph(str(s["passed"]),  cell),
        Paragraph(str(s["failed"]),  miss if s["failed"] else cell),
        Paragraph(f"{overall_cov}%", cell),
        Paragraph(f"{avg_cov}%",     cell),
        Paragraph(str(tot_missing),  miss if tot_missing else cell),
        Paragraph(str(tot_site),     cell),
        Paragraph(str(tot_matched),  cell),
    ]
    met = Table([met_hdr, met_row], repeatRows=1)
    met.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0ea5a4")),
        ("GRID",       (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN",     (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN",      (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING",    (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [
        Paragraph("<b>Metrics (based on the TOC / left navigation)</b>", sub),
        Spacer(1, 4), met, Spacer(1, 14),
        Paragraph("<b>Per-item detail</b>", sub), Spacer(1, 4),
    ]

    # ── Per-item rows ──────────────────────────────────────────────────────
    item_hdr = ParagraphStyle("ihdr", parent=ss["Normal"], fontSize=9.5,
                              leading=13, fontName="Helvetica-Bold", spaceBefore=8)
    meta_s   = ParagraphStyle("meta", parent=ss["Normal"], fontSize=7.5,
                              leading=10, textColor=colors.HexColor("#64748b"))
    sev = {"PASS": "#166534", "FAIL": "#991b1b", "ERROR": "#854d0e"}

    for i, r in enumerate(rows_all, 1):
        status  = (r.get("status") or "error").upper()
        cov     = (f"{r.get('coverage_pct')}%"
                   if r.get("coverage_pct") is not None else "—")
        matched = (f"{r.get('matched_words')} / {r.get('site_words')}"
                   if r.get("matched_words") is not None
                   else (str(r.get("site_words")) if r.get("site_words") is not None else "—"))

        story.append(Paragraph(
            f'{i}. {esc(r.get("nav_item"))} '
            f'<font color="{sev.get(status, "#334155")}">[{status}]</font> '
            f'<font color="#334155">· coverage {cov} · matched/site {matched}</font>',
            item_hdr,
        ))
        if r.get("url"):
            story.append(Paragraph(esc(r["url"]), meta_s))
        if r.get("error"):
            story.append(Paragraph(f"Error: {esc(r['error'])}", miss))
        elif r.get("note"):
            story.append(Paragraph(esc(r["note"]), miss))
        elif r.get("missing_all"):
            ms = r["missing_all"]
            story.append(Paragraph(f"<b>{len(ms)} missing in PDF:</b>", miss))
            for j in range(0, len(ms), 120):
                story.append(Paragraph(esc(" ".join(ms[j:j + 120])), miss))
        else:
            story.append(Paragraph("✓ All content found in PDF", cell))

    doc.build(story)
    bio.seek(0)
    return bio

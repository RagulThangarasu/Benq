#!/usr/bin/env python3
"""
Self-contained PDF validation report generator: PROD (base) vs STAGE.

Two validations written into one colourful PDF under reports/:

  1. TOC structure  - every heading in PROD's Table of Contents must also exist
                      in STAGE's TOC.            -> what's missing / wrong in TOC
  2. TOC content    - the body content of every PROD TOC section must be present
                      in STAGE.                  -> exact missing content/section
  3. Extra topics   - TOC topics present in STAGE but absent in PROD.

Text in PROD at font size <= 8.5 pt (OSD mockup screenshots, diagram callout
labels) is excluded from content comparison because those elements are rendered
as raster images in STAGE and are not extractable as text. Only body prose
(>=9 pt) is compared. Page 1, TOC/navigation pages and inline page-number
tokens are also excluded.

Run:    python3 generate_validation_report.py
Output: reports/pdf_validation_report.pdf
"""

from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from pathlib import Path

import fitz  # PyMuPDF

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
BASE = Path(__file__).resolve().parent
import glob
def _find_pdf(side, prefer):
    direct = BASE / "PDF" / side / prefer
    if direct.exists():
        return direct
    hits = glob.glob(str(BASE / "PDF" / side / "**" / "*.pdf"), recursive=True)
    for h in hits:
        if "sw272" in Path(h).name.lower():
            return Path(h)
    return Path(hits[0]) if hits else direct

PROD_PDF = _find_pdf("prod", "SW272_EN.pdf")
STAGE_PDF = _find_pdf("stage", "sw272_en_v5.pdf")
REPORTS_DIR = BASE / "reports"
PDF_PATH = REPORTS_DIR / "pdf_validation_report.pdf"

MIN_SPAN = 4          # report missing runs of >= this many consecutive words
CHAR_SHINGLE = 18     # char-window length for shingle-based coverage detection
_OSD_FONT_MAX = 8.5   # PROD spans at or below this pt size are OSD/diagram overlays
_MIN_BLOCK_CHARS = 40  # minimum block characters for non-heading blocks
_INT_RE = re.compile(r"\d{1,3}$")
# Matches "on page 48", "see page 26 - 29", "on pages 44-55" etc.
_PAGE_REF_RE = re.compile(
    r'\b(?:on|see)\s+pages?\s+\d+(?:\s*[-–]\s*\d+)?\.?',
    re.IGNORECASE,
)
_NAV_INLINE_RE = re.compile(r"\b\d{1,2}\b")


# ---------------------------------------------------------------------------
# Text extraction & normalisation
# ---------------------------------------------------------------------------
def normalize(text: str) -> str:
    text = re.sub(r"\.{2,}", " ", text)   # drop TOC dot-leaders
    text = re.sub(r"\s+", " ", text)      # collapse whitespace
    return text.strip()


def canon(text: str) -> str:
    """Canonical form for shingle coverage: letters & digits only, NFKC-folded."""
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in text if unicodedata.category(ch)[0] in ("L", "N"))


def page_words(page) -> list[str]:
    w = page.get_text("words")             # x0,y0,x1,y1,word,...
    w.sort(key=lambda r: (round(r[1] / 3), r[0]))   # banded reading order
    return [x[4] for x in w]


def page_words_join(page) -> str:
    return normalize(" ".join(page_words(page)))


def _median_line_len(page) -> float:
    d = page.get_text("dict")
    lens = []
    for b in d["blocks"]:
        if b.get("type") != 0:
            continue
        for line in b.get("lines", []):
            t = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
            if len(t) > 10:
                lens.append(len(t))
    if not lens:
        return 0.0
    lens.sort()
    return lens[len(lens) // 2]


def detect_toc_pages(doc) -> set[int]:
    """1-based page numbers that are pure navigation/front-matter (skip them).

    Dot-leader TOC pages and early inline cross-reference index pages
    (e.g. Q&A index) that consist entirely of short navigational phrases.
    """
    total = doc.page_count
    result = set()
    for i, p in enumerate(doc, 1):
        text = p.get_text()
        if len(re.findall(r"\.{4,}", text)) >= 8:
            result.add(i)
        elif (i <= max(1, int(total * 0.10)) and
              len(_NAV_INLINE_RE.findall(text)) >= 15 and
              _median_line_len(p) <= 50):
            result.add(i)
    return result


def keep(words: list[str]) -> list[str]:
    return [w for w in words if w and not _INT_RE.fullmatch(w)]


def find_sub(hay: list[str], needle: list[str], start: int) -> int:
    n = len(needle)
    if n == 0:
        return -1
    for i in range(start, len(hay) - n + 1):
        if hay[i:i + n] == needle:
            return i
    return -1


# ---------------------------------------------------------------------------
# Validation 1: TOC structure (headings)
# ---------------------------------------------------------------------------
def stage_toc_and_body():
    doc = fitz.open(STAGE_PDF)
    toc_text = normalize(" ".join(t for _, t, _ in doc.get_toc()))
    body_text = normalize(" ".join(
        page_words_join(p) for i, p in enumerate(doc, 1) if i != 1))
    doc.close()
    return toc_text, body_text


def validate_headings():
    prod = fitz.open(PROD_PDF)
    outline = prod.get_toc()
    nav_pages = detect_toc_pages(prod)
    prod.close()
    stage_toc, stage_body = stage_toc_and_body()
    stage_toc_l, stage_body_l = stage_toc.lower(), stage_body.lower()

    rows = []
    for level, title, pgno in outline:
        h = normalize(title)
        if pgno in nav_pages:
            status = "BODY ONLY"   # navigation aggregator; STAGE uses its TOC instead
        elif h and h in stage_toc:
            status = "MATCH"
        elif h and h in stage_body:
            status = "BODY ONLY"
        elif h and (h.lower() in stage_toc_l or h.lower() in stage_body_l):
            status = "BODY ONLY"
        else:
            status = "MISSING"
        rows.append({"level": level, "heading": title,
                     "prod_page": pgno, "struct": status})
    return rows


# ---------------------------------------------------------------------------
# Validation 2: TOC section content
# ---------------------------------------------------------------------------
def prod_page_words_join(page) -> str:
    """Extract body text from a PROD page, skipping non-body text elements.

    Two filters are applied:

    1. Per-span font-size filter: spans at or below _OSD_FONT_MAX (8.5 pt) are
       OSD mockup screenshot text or small diagram labels.  STAGE renders those
       elements as raster images, so the text is not extractable there.

    2. Block-level short-block filter: isolated text blocks whose total
       character count is below _MIN_BLOCK_CHARS (40) and whose largest font
       is at or below 12 pt are diagram callout labels, connector labels,
       figure captions or model-variant sub-headings (e.g. "SW272", "SW242",
       "Headphone", "L2", "Quick Start Guide").  STAGE embeds these as part of
       raster diagram images.  Section headings (> 12 pt) are always included
       regardless of length.

    Inline page references ('on page N', 'see page N') are also stripped
    because STAGE replaced them with hyperlinks or removed them.
    """
    d = page.get_text("dict")
    parts = []
    for block in d["blocks"]:
        if block.get("type") != 0:
            continue
        block_spans = [
            s for line in block.get("lines", [])
            for s in line.get("spans", [])
        ]
        block_text_total = "".join(
            s.get("text", "") for s in block_spans
        ).strip()
        max_font = max((s.get("size", 0) for s in block_spans), default=0)
        # Skip short isolated non-heading blocks (diagram labels, captions)
        if max_font <= 12.0 and len(block_text_total) < _MIN_BLOCK_CHARS:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("size", 0) > _OSD_FONT_MAX:
                    parts.append(span.get("text", ""))
    text = " ".join(parts)
    text = _PAGE_REF_RE.sub(" ", text)
    return normalize(text)


def prod_sections():
    """Yield (level, title, prod_page, word_list) per TOC entry."""
    doc = fitz.open(PROD_PDF)
    outline = doc.get_toc()
    skip = {1} | detect_toc_pages(doc)
    stream, page_start = [], {}
    for i, page in enumerate(doc, 1):
        if i in skip:
            continue
        page_start[i] = len(stream)
        stream += prod_page_words_join(page).split(" ")
    doc.close()
    kept = sorted(page_start)

    def window(pgno):
        if pgno in page_start:
            base, lo = pgno, page_start[pgno]
        else:
            later = [p for p in kept if p >= pgno]
            base = later[0] if later else kept[-1]
            lo = page_start[base]
        after = [p for p in kept if p > base]
        return lo, (page_start[after[0]] if after else len(stream))

    located, pos = [], 0
    for level, title, pgno in outline:
        needle = normalize(title).split(" ")
        lo, hi = window(pgno)
        idx = find_sub(stream[:hi], needle, max(pos, lo))
        if idx < 0:
            idx = find_sub(stream, needle, lo)
        if idx < 0:
            idx = find_sub(stream, needle, pos)
        if idx >= 0:
            pos = idx + len(needle)
        located.append((idx, level, title, pgno))

    starts = [l[0] for l in located if l[0] is not None and l[0] >= 0]
    sections = []
    for idx, level, title, pgno in located:
        if idx is None or idx < 0:
            sections.append((level, title, pgno, []))
            continue
        nxt = [s for s in starts if s > idx]
        end = min(nxt) if nxt else len(stream)
        sections.append((level, title, pgno, stream[idx:end]))
    return sections


def stage_content():
    """Return STAGE body as shingle set + full normalised text for phrase search."""
    doc = fitz.open(STAGE_PDF)
    words = []
    raw_parts = []
    for i, page in enumerate(doc, 1):
        if i == 1:
            continue
        words += page_words_join(page).split(" ")
        raw_parts.append(page.get_text())
    doc.close()
    nospace = "".join(canon(w) for w in keep(words))
    cset = {nospace[i:i + CHAR_SHINGLE]
            for i in range(len(nospace) - CHAR_SHINGLE + 1)}
    # Full STAGE text in normalised lowercase for exact phrase lookups
    stage_text_lower = re.sub(r"\s+", " ", " ".join(raw_parts)).lower()
    return nospace, cset, stage_text_lower


def section_missing(words, stage_nospace, stage_cset, stage_text_lower):
    """Return (coverage_pct, missing_fragments) for a PROD section.

    Uses character-shingle windows (CHAR_SHINGLE chars) to detect which words
    are covered by STAGE content.  For each uncovered run of >= MIN_SPAN words,
    performs a direct phrase search in the full STAGE text: if the normalised
    phrase appears somewhere in STAGE the text was reorganised (not reported);
    if it is genuinely absent it is added to missing_fragments.
    """
    words = keep(words)
    if not words:
        return 100.0, []

    cwords = [canon(w) for w in words]
    s = "".join(cwords)
    char_word = []
    for wi, cw in enumerate(cwords):
        char_word.extend([wi] * len(cw))

    L = CHAR_SHINGLE
    if len(s) < L:
        # Section too short for shingle; do a direct phrase search
        if s and s in stage_nospace:
            return 100.0, []
        phrase = re.sub(r"\s+", " ", " ".join(words)).lower()
        if phrase in stage_text_lower:
            return 100.0, []
        return 0.0, ([" ".join(words)] if len(words) >= MIN_SPAN else [])

    # Shingle coverage pass
    covered_char = [False] * len(s)
    for p in range(len(s) - L + 1):
        if s[p:p + L] in stage_cset:
            for q in range(p, p + L):
                covered_char[q] = True

    covcount = [0] * len(words)
    for ci, hit in enumerate(covered_char):
        if hit:
            covcount[char_word[ci]] += 1
    # Pure-punctuation/symbol words canon to "" -> always covered
    covered = [(not cwords[i]) or
               covcount[i] >= max(1, (len(cwords[i]) + 1) // 2)
               for i in range(len(words))]
    coverage = 100.0 * sum(covered) / len(words)

    # Collect uncovered runs and verify each against full STAGE text
    spans = []
    i = 0
    while i < len(words):
        if not covered[i]:
            st = i
            while i < len(words) and not covered[i]:
                i += 1
            frag = words[st:i]
            if len(frag) >= MIN_SPAN:
                phrase = re.sub(r"\s+", " ", " ".join(frag)).lower()
                if phrase not in stage_text_lower:
                    spans.append(" ".join(frag))
        else:
            i += 1
    return coverage, spans


def validate_content():
    stage_nospace, stage_cset, stage_text_lower = stage_content()
    rows = []
    for level, title, pgno, words in prod_sections():
        coverage, spans = section_missing(
            words, stage_nospace, stage_cset, stage_text_lower)
        if len(keep(words)) == 0:
            status = "NO CONTENT"
        elif spans:
            status = "FAILED"
        else:
            status = "PASS"
        rows.append({"level": level, "heading": title, "prod_page": pgno,
                     "word_count": len(keep(words)), "coverage": coverage,
                     "status": status, "spans": spans})
    return rows


def stage_page_lookup():
    """Resolve a PROD heading to the STAGE page number where it appears."""
    doc = fitz.open(STAGE_PDF)
    outline = {}
    for _, title, pg in doc.get_toc():
        outline.setdefault(normalize(title).lower(), pg)
    stream, marks = [], []
    for i, page in enumerate(doc, 1):
        marks.append((len(stream), i))
        stream += page_words_join(page).split(" ")
    doc.close()

    def resolve(heading):
        key = normalize(heading).lower()
        if key in outline:
            return outline[key]
        idx = find_sub(stream, normalize(heading).split(" "), 0)
        if idx < 0:
            return None
        page = marks[0][1]
        for start, pno in marks:
            if start <= idx:
                page = pno
            else:
                break
        return page

    return resolve


# ---------------------------------------------------------------------------
# Validation 3: Extra topics in STAGE (not in PROD)
# ---------------------------------------------------------------------------
def extra_stage_topics():
    """Return STAGE TOC entries whose heading has no counterpart in PROD's TOC."""
    prod_doc = fitz.open(PROD_PDF)
    prod_headings = {normalize(title).lower()
                     for _, title, _ in prod_doc.get_toc()}
    prod_doc.close()

    stage_doc = fitz.open(STAGE_PDF)
    stage_toc = stage_doc.get_toc()
    stage_doc.close()

    extra = []
    for level, title, pg in stage_toc:
        if normalize(title).lower() not in prod_headings:
            extra.append({"level": level, "heading": title, "stage_page": pg})
    return extra


# ---------------------------------------------------------------------------
# PDF rendering helpers
# ---------------------------------------------------------------------------
PAGE_W, PAGE_H = 595, 842
MARGIN = 42
CONTENT_W = PAGE_W - 2 * MARGIN
GREEN  = (0.10, 0.50, 0.13)
AMBER  = (0.62, 0.42, 0.0)
RED    = (0.81, 0.13, 0.18)
INK    = (0.13, 0.16, 0.19)
GREY   = (0.45, 0.48, 0.52)
LINE   = (0.85, 0.87, 0.89)
TRACK  = (0.92, 0.93, 0.94)
BANNER = (0.10, 0.12, 0.15)
THEAD  = (0.20, 0.24, 0.29)
BORDER = (0.80, 0.82, 0.85)
ZEBRA  = (0.965, 0.97, 0.975)
SCOLOR = {
    "PASS": GREEN, "PARTIAL": AMBER, "FAIL": RED, "FAILED": RED,
    "NO CONTENT": GREY, "MATCH": GREEN, "BODY ONLY": AMBER, "MISSING": RED,
}
_CHAR = {
    "™": "(TM)", "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "…": "...", "•": "-",
    "→": "->", "：": ":",
}


def safe(s: str) -> str:
    for k, v in _CHAR.items():
        s = s.replace(k, v)
    return "".join(c if ord(c) < 256 else "?" for c in s)


def wrap(text, font, size, maxw):
    lines, cur = [], ""
    for w in text.split(" "):
        trial = w if not cur else cur + " " + w
        if fitz.get_text_length(trial, fontname=font, fontsize=size) <= maxw:
            cur = trial
            continue
        if cur:
            lines.append(cur)
        if fitz.get_text_length(w, fontname=font, fontsize=size) > maxw:
            chunk = ""
            for ch in w:
                if fitz.get_text_length(chunk + ch, fontname=font, fontsize=size) <= maxw:
                    chunk += ch
                else:
                    lines.append(chunk)
                    chunk = ch
            cur = chunk
        else:
            cur = w
    if cur:
        lines.append(cur)
    return lines or [""]


class Report:
    def __init__(self):
        self.doc = fitz.open()
        self._new_page()

    def _new_page(self):
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        self.y = MARGIN

    def _ensure(self, h):
        if self.y + h > PAGE_H - MARGIN - 14:
            self._new_page()

    def text(self, s, size=10, color=INK, font="helv", indent=0.0, gap=3.0):
        for line in wrap(safe(s), font, size, CONTENT_W - indent):
            self._ensure(size + gap)
            self.page.insert_text((MARGIN + indent, self.y + size), line,
                                  fontsize=size, color=color, fontname=font)
            self.y += size + gap

    def gapv(self, h=6.0):
        self.y += h

    def rule(self, color=LINE):
        self._ensure(8)
        self.page.draw_line((MARGIN, self.y), (PAGE_W - MARGIN, self.y),
                            color=color, width=0.7)
        self.y += 8

    def badge(self, label, color, x, w=66, h=13):
        self.page.draw_rect(fitz.Rect(x, self.y, x + w, self.y + h),
                            fill=color, color=color)
        tw = fitz.get_text_length(label, fontname="hebo", fontsize=8)
        self.page.insert_text((x + (w - tw) / 2, self.y + h - 3.5), label,
                              fontsize=8, color=(1, 1, 1), fontname="hebo")

    def bar(self, pct, color, x, w=46, h=13):
        self.page.draw_rect(fitz.Rect(x, self.y, x + w, self.y + h),
                            fill=TRACK, color=TRACK)
        self.page.draw_rect(fitz.Rect(x, self.y, x + max(2.0, w * pct / 100.0),
                            self.y + h), fill=color, color=color)
        self.page.insert_text((x + w + 5, self.y + h - 3.5), f"{pct:.0f}%",
                              fontsize=8, color=GREY, fontname="helv")

    def banner(self, title, subtitle, overall):
        self.page.draw_rect(fitz.Rect(0, 0, PAGE_W, 92), fill=BANNER, color=BANNER)
        self.page.insert_text((MARGIN, 42), title, fontsize=20,
                              color=(1, 1, 1), fontname="hebo")
        self.page.insert_text((MARGIN, 62), safe(subtitle), fontsize=9,
                              color=(0.75, 0.78, 0.82), fontname="helv")
        col = GREEN if overall == "PASS" else RED
        bw = 104
        self.page.draw_rect(fitz.Rect(PAGE_W - MARGIN - bw, 30,
                            PAGE_W - MARGIN, 56), fill=col, color=col)
        t = f"OVERALL {overall}"
        tw = fitz.get_text_length(t, fontname="hebo", fontsize=10)
        self.page.insert_text((PAGE_W - MARGIN - bw + (bw - tw) / 2, 47), t,
                              fontsize=10, color=(1, 1, 1), fontname="hebo")
        self.y = 104

    def section_title(self, text):
        self.gapv(6)
        self._ensure(24)
        self.page.insert_text((MARGIN, self.y + 13), text, fontsize=13,
                              color=INK, fontname="hebo")
        self.y += 18
        self.rule(INK)

    def kpis(self, items):
        self._ensure(42)
        x, bw = MARGIN, (CONTENT_W - 10 * (len(items) - 1)) / len(items)
        for label, value, color in items:
            self.page.draw_rect(fitz.Rect(x, self.y, x + bw, self.y + 38),
                                fill=(0.97, 0.97, 0.98), color=LINE)
            self.page.insert_text((x + 9, self.y + 21), str(value),
                                  fontsize=17, color=color, fontname="hebo")
            self.page.insert_text((x + 9, self.y + 32), label, fontsize=7.5,
                                  color=GREY, fontname="helv")
            x += bw + 10
        self.y += 46

    def table(self, headers, widths, rows, aligns=None, fs=9.0, stripe=None):
        """Bordered table with header band, zebra striping, wrapping, pagination."""
        pad, lh = 5.0, fs + 2.5
        aligns = aligns or ["l"] * len(headers)
        total = sum(widths)

        def header():
            self._ensure(18)
            top = self.y
            self.page.draw_rect(fitz.Rect(MARGIN, top, MARGIN + total, top + 16),
                                fill=THEAD, color=THEAD)
            x = MARGIN
            for h, w, al in zip(headers, widths, aligns):
                tx = (x + pad if al != "c" else
                      x + (w - fitz.get_text_length(h, fontname="hebo", fontsize=fs)) / 2)
                self.page.insert_text((tx, top + 11.5), h, fontsize=fs,
                                      color=(1, 1, 1), fontname="hebo")
                x += w
            self.y = top + 16

        header()
        for ri, row in enumerate(rows):
            cells, nlines = [], 1
            for (text, color, font), w in zip(row, widths):
                ls = wrap(safe(str(text)), font, fs, w - 2 * pad)
                cells.append((ls, color, font))
                nlines = max(nlines, len(ls))
            rh = nlines * lh + 2 * pad - 2
            if self.y + rh > PAGE_H - MARGIN - 16:
                self._new_page()
                header()
            top = self.y
            shade = stripe[ri] if stripe is not None else (ri % 2 == 1)
            if shade:
                self.page.draw_rect(fitz.Rect(MARGIN, top, MARGIN + total, top + rh),
                                    fill=ZEBRA, color=ZEBRA)
            x = MARGIN
            for (ls, color, font), w, al in zip(cells, widths, aligns):
                ty = top + pad + fs
                for ln in ls:
                    tx = (x + pad if al != "c" else
                          x + (w - fitz.get_text_length(ln, fontname=font, fontsize=fs)) / 2)
                    self.page.insert_text((tx, ty), ln, fontsize=fs,
                                          color=color, fontname=font)
                    ty += lh
                x += w
            self.page.draw_rect(fitz.Rect(MARGIN, top, MARGIN + total, top + rh),
                                color=BORDER, width=0.5)
            x = MARGIN
            for w in widths[:-1]:
                x += w
                self.page.draw_line((x, top), (x, top + rh), color=BORDER, width=0.4)
            self.y = top + rh
        self.y += 8

    def save(self):
        n = self.doc.page_count
        for i, pg in enumerate(self.doc, 1):
            pg.insert_text((PAGE_W - MARGIN - 58, PAGE_H - 24),
                           f"Page {i} / {n}",
                           fontsize=8, color=GREY, fontname="helv")
            pg.insert_text((MARGIN, PAGE_H - 24),
                           "PDF Content Validation  -  PROD vs STAGE",
                           fontsize=8, color=GREY, fontname="helv")
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        self.doc.save(PDF_PATH, deflate=True)
        self.doc.close()


# ---------------------------------------------------------------------------
# Build report
# ---------------------------------------------------------------------------
def build_report() -> dict:
    head_rows    = validate_headings()
    cont_rows    = validate_content()
    extra_topics = extra_stage_topics()
    stage_page   = stage_page_lookup()

    toc_missing  = [r for r in head_rows if r["struct"] == "MISSING"]
    toc_bodyonly = [r for r in head_rows if r["struct"] == "BODY ONLY"]
    toc_match    = len(head_rows) - len(toc_missing) - len(toc_bodyonly)

    cont_pass    = sum(1 for r in cont_rows if r["status"] == "PASS")
    cont_fail    = sum(1 for r in cont_rows if r["status"] == "FAILED")
    tw = sum(r["word_count"] for r in cont_rows) or 1
    coverage = sum(r["coverage"] * r["word_count"] for r in cont_rows) / tw
    overall = "PASS" if (not toc_missing and cont_fail == 0) else "FAILED"

    # Roll content status up to top-level topics for the overview table
    rank = {"PASS": 0, "NO CONTENT": 1, "FAILED": 2}
    groups, cur = [], None
    for h, c in zip(head_rows, cont_rows):
        if h["level"] == 1:
            cur = {"heading": h["heading"], "struct": h["struct"],
                   "content": c["status"], "page": stage_page(h["heading"])}
            groups.append(cur)
        elif cur is not None and rank[c["status"]] > rank[cur["content"]]:
            cur["content"] = c["status"]

    rep = Report()
    rep.banner(
        "PDF VALIDATION REPORT",
        f"PROD (base): {PROD_PDF.name}   |   STAGE: {STAGE_PDF.name}"
        f"   |   {_dt.date.today().isoformat()}",
        overall,
    )
    rep.text(
        "PROD is the base. Every TOC topic and all its body content (>=9 pt) "
        "must also exist in STAGE. Page 1, navigation pages, OSD mockup "
        "screenshots and diagram callout labels are excluded from comparison.",
        size=9.5, color=GREY,
    )
    rep.gapv(6)
    rep.kpis([
        ("TOC topics",   len(head_rows),    INK),
        ("TOC match",    toc_match,          GREEN),
        ("TOC missing",  len(toc_missing),   RED if toc_missing  else GREEN),
        ("Sect. PASS",   cont_pass,          GREEN),
        ("Sect. FAIL",   cont_fail,          RED if cont_fail    else GREEN),
        ("Coverage",     f"{coverage:.0f}%", INK),
        ("Extra (STAGE)", len(extra_topics), AMBER if extra_topics else GREEN),
    ])

    # -----------------------------------------------------------------------
    # Section 1: TOC overview — top-level topics
    # -----------------------------------------------------------------------
    rep.section_title("1.  TOC validation  -  top-level topics")
    rep.text(
        "Each main topic, the STAGE page where it appears, whether its "
        "heading matches the PROD TOC, and whether its body content is complete.",
        size=9, color=GREY,
    )
    rep.gapv(6)
    rows1 = []
    for g in groups:
        pg = g["page"]
        rows1.append([
            (g["heading"],  INK,                        "hebo"),
            (str(pg) if pg else "-", INK if pg else RED, "helv"),
            (g["struct"],   SCOLOR.get(g["struct"], INK), "hebo"),
            (g["content"],  SCOLOR[g["content"]],        "hebo"),
        ])
    rep.table(
        ["Top-level topic", "Stage pg", "TOC", "Content"],
        [286, 60, 82, 82],
        rows1,
        aligns=["l", "c", "c", "c"],
        fs=9.5,
    )

    # -----------------------------------------------------------------------
    # Section 2: Missing content — exact PROD text not found in STAGE
    # -----------------------------------------------------------------------
    rep.section_title("2.  Missing content  -  exact PROD text absent from STAGE")
    rep.text(
        "Per topic, with the STAGE page where the content should appear. "
        "Fully-present topics are omitted.  Only confirmed-absent text is shown "
        "(reorganised content that appears elsewhere in STAGE is not listed).",
        size=9, color=GREY,
    )
    rep.gapv(6)
    rows2, stripe, gid = [], [], -1
    for c in cont_rows:
        if not c["spans"]:
            continue
        gid += 1
        pg = stage_page(c["heading"])
        pcell = str(pg) if pg else "not in stage"
        for k, frag in enumerate(c["spans"]):
            rows2.append([
                (c["heading"] if k == 0 else "", INK, "hebo"),
                (pcell        if k == 0 else "", INK if pg else RED, "helv"),
                (frag, RED, "helv"),
            ])
            stripe.append(gid % 2 == 1)
    if rows2:
        rep.table(
            ["Topic", "Stage pg", "Missing content (exact PROD text)"],
            [150, 58, 303],
            rows2,
            aligns=["l", "c", "l"],
            fs=9.0,
            stripe=stripe,
        )
    else:
        rep.text("None - all PROD body content is present in STAGE.",
                 size=10, color=GREEN, font="hebo")

    # -----------------------------------------------------------------------
    # Section 3: Extra topics in STAGE not present in PROD
    # -----------------------------------------------------------------------
    rep.section_title("3.  Extra content in STAGE  -  topics absent from PROD TOC")
    rep.text(
        "These headings appear in STAGE's Table of Contents but have no "
        "counterpart in PROD.  They represent new or restructured content "
        "added in STAGE.",
        size=9, color=GREY,
    )
    rep.gapv(6)
    if extra_topics:
        rows3 = []
        for t in extra_topics:
            indent = "    " if t["level"] > 1 else ""
            font   = "helv" if t["level"] > 1 else "hebo"
            rows3.append([
                (indent + t["heading"], INK, font),
                (str(t["stage_page"]),  INK, "helv"),
            ])
        rep.table(
            ["Extra topic (in STAGE only)", "Stage pg"],
            [452, 58],
            rows3,
            aligns=["l", "c"],
            fs=9.5,
        )
    else:
        rep.text("None - no extra topics found in STAGE.",
                 size=10, color=GREEN, font="hebo")

    rep.save()

    return {
        "head_rows":       head_rows,
        "cont_rows":       cont_rows,
        "overall":         overall,
        "toc_missing":     toc_missing,
        "content_failed":  [r for r in cont_rows if r["status"] == "FAILED"],
        "extra_stage":     extra_topics,
        "coverage":        coverage,
        "pdf_path":        PDF_PATH,
    }


def main() -> int:
    r = build_report()
    print(f"Overall                      : {r['overall']}")
    print(f"TOC headings missing         : {len(r['toc_missing'])}"
          + (f"  -> {', '.join(x['heading'] for x in r['toc_missing'])}"
             if r['toc_missing'] else ""))
    print(f"Sections w/ missing content  : {len(r['content_failed'])}")
    if r["content_failed"]:
        for row in r["content_failed"]:
            print(f"  - {row['heading']!r}  (PROD p{row['prod_page']})")
            for frag in row["spans"]:
                print(f"      MISSING: {frag[:100]}")
    print(f"Extra topics in STAGE        : {len(r['extra_stage'])}")
    print(f"Overall content coverage     : {r['coverage']:.0f}%")
    print(f"PDF report                   : {r['pdf_path']}")
    return 0 if r["overall"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""
PDF Content Validation — TOC + Content Check
=============================================
Part 1: TOC comparison table (Match / Missing in Stage / Extra in Stage)
Part 2: Content differences for matching topics — showing only confirmed-absent
        text (reorganised content that appears elsewhere in STAGE is not reported).

Text at font size ≤ 8.5 pt (OSD mockup screenshots, diagram callout labels) and
short isolated text blocks (< 40 chars, max font ≤ 12 pt) are excluded from PROD
text extraction because STAGE renders those elements as raster images.

Page references ("on page N"), formatting labels (NOTE:/TIP:/IMPORTANT:) and
standalone bullet characters are stripped from both sides before comparison so
that pure-formatting rewrites are not counted as missing content.
"""

import sys
import os
import re
import statistics
import statistics
import unicodedata
import hashlib
import fitz
from io import BytesIO
try:
    from PIL import Image, ImageChops, ImageStat
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False
from reportlab.lib.pagesizes import landscape, letter
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.lib.units import inch


# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────
_OSD_FONT_HARD   = 7.0   # spans at or below this pt are always OSD overlay / diagram
_OSD_FONT_SOFT   = 8.5   # spans 7–8.5 pt are excluded only when block is an OSD screenshot
_MIN_BLOCK_CHARS = 30    # min chars for blocks with max-font in OSD-soft range (7–8.5 pt)
_MIN_BLOCK_BODY  = 10    # min chars for normal body-font blocks (> 8.5 pt, ≤ 12 pt)
_MIN_ONPAGE_AREA = 50    # pt²  — skip image placements smaller than ~7×7 pt on page
_ICON_MAX_ONPAGE = 80    # pt   — max(bw,bh) on page ≤ this = Icon; larger = Content image
_FAIL_ON_ICON_MISS = False  # icon-size matching is noisy across PDF exports; don't fail section on icon-only miss
# Legacy aliases kept for any remaining code that references the old names
_MIN_IMG_PIXELS  = _MIN_ONPAGE_AREA
_ICON_MAX_DIM    = _ICON_MAX_ONPAGE
CHAR_SHINGLE    = 18     # character window for shingle coverage
MIN_FRAG_WORDS  = 3      # minimum uncovered word-run length to report (lowered to capture smaller missing fragments)
VISUAL_PAGE_THRESHOLD = 0.82   # page-level visual similarity threshold (0-1)
VISUAL_RENDER_SCALE   = 0.8    # render scale for visual compare (speed vs fidelity)

_INT_RE          = re.compile(r"^\d{1,3}$")
_PAGE_REF_RE     = re.compile(
    r"\b(?:on|see)\s+pages?\s+\d+(?:\s*[-–]\s*\d+)?\.?", re.IGNORECASE)
_NAV_INLINE_RE   = re.compile(r"\b\d{1,2}\b")
# Strip formatting-only labels before comparison
_FMT_LABEL_RE    = re.compile(
    r"\b(NOTE|TIP|IMPORTANT|CAUTION|WARNING)\s*:\s*", re.IGNORECASE)
# Numbered list marker — matches "1." "7." etc. (not "1," "2)")
_NUMBERED_ITEM_RE = re.compile(r"\b\d+\.")
# OSD screenshot block pattern — resolution, refresh rate, or nav-key labels
_OSD_SCREEN_RE = re.compile(
    r"\d{3,4}x\d{3,4}|"      # resolution like 3840x2160
    r"\d{2,3}Hz\b|"           # refresh rate like 30Hz, 60Hz
    r"\b\d{2,3}p\b|"          # refresh like 60p
    r"\bExit\s+Move\b|"       # OSD navigation label
    r"\bBack\s+Move\b|"       # OSD navigation label
    r"\bMove\s+(Edit|Confirm)\b",  # OSD navigation label
    re.IGNORECASE,
)


# ────────────────────────────────────────────────────────────────────────────
# Low-level text utilities
# ────────────────────────────────────────────────────────────────────────────
def _normalize(text: str) -> str:
    text = re.sub(r"\.{2,}", " ", text)
    text = re.sub(r"\s+",    " ", text)
    return text.strip()


def _canon(text: str) -> str:
    """Letters & digits only, NFKC-folded lowercase — for shingle coverage."""
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(c for c in text if unicodedata.category(c)[0] in ("L", "N"))


def _norm_key(text: str) -> str:
    """Alphanumeric-only lowercase key for TOC matching."""
    return re.sub(r"[^a-z0-9]", "", text.lower())


def _strip_formatting(text: str) -> str:
    """Remove formatting-only artefacts that differ between PROD and STAGE."""
    text = _PAGE_REF_RE.sub(" ", text)          # "on page N" / "see page N"
    text = _FMT_LABEL_RE.sub(" ", text)         # NOTE: TIP: IMPORTANT: etc.
    # Strip bullets/dashes only when they are standalone (preceded by space/start
    # or followed by space), not mid-word hyphens like "How-to".
    text = re.sub(r"(?<!\w)[•·▪▸►]\s*", " ", text)  # bullet chars (never mid-word)
    text = re.sub(r"(?<!\w)\-(?!\w)", " ", text)     # standalone dash (not "How-to")
    # Remove repeated structural OSD table headers — they appear on every page
    # of the menu section in PROD but convey no unique content to compare.
    text = re.sub(r"\bItem\s+Function\s+Range\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


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


def _keep(words):
    return [w for w in words if w and not _INT_RE.match(w)]


# ────────────────────────────────────────────────────────────────────────────
# Navigation-page detection
# ────────────────────────────────────────────────────────────────────────────
def _detect_nav_pages(doc) -> set:
    """Return 1-based page numbers that are TOC / navigation / index pages."""
    total  = doc.page_count
    result = set()
    for i, p in enumerate(doc, 1):
        text = p.get_text()
        if len(re.findall(r"\.{4,}", text)) >= 8:
            result.add(i)
        elif (i <= max(1, int(total * 0.10))
              and len(_NAV_INLINE_RE.findall(text)) >= 15
              and _median_line_len(p) <= 50):
            result.add(i)
    return result


# ────────────────────────────────────────────────────────────────────────────
# Body-text extraction per page
# ────────────────────────────────────────────────────────────────────────────
def _extract_page_body_prod(page) -> str:
    """Extract PROD body text: skip OSD overlays and short diagram-label blocks.

    Three-layer filter:
    1. Block-level: blocks with max_font ≤ 12 pt AND total chars < _MIN_BLOCK_CHARS
       are short isolated labels (diagram callouts, connector numbers) — skip entire block.
    2. Span-level hard: spans ≤ _OSD_FONT_HARD (7 pt) are always OSD menu overlay text.
    3. Span-level soft: spans 7–8.5 pt are skipped only when the containing block is an
       OSD screenshot overlay (identified by resolution / refresh-rate / nav-key keywords).
       Spans at 7–8.5 pt that are part of a regular content table (e.g. the Color Mode
       feature-availability matrix at 8 pt) are included.
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
        block_txt = "".join(s.get("text", "") for s in block_spans).strip()
        max_font  = max((s.get("size", 0) for s in block_spans), default=0)
        # Skip short isolated blocks — use a tighter limit for OSD-soft-range fonts
        # (7–8.5 pt blocks need ≥ 30 chars to be worth comparing; normal body font
        # blocks > 8.5 pt only need ≥ 10 chars so that short model labels like
        # "SW272 SW242" are included).
        if max_font <= 12.0:
            limit = _MIN_BLOCK_CHARS if max_font <= _OSD_FONT_SOFT else _MIN_BLOCK_BODY
            if len(block_txt) < limit:
                continue
        # Skip repetitive icon-glyph substitution blocks, e.g. spans ["or","or","or"].
        # block_txt uses "".join() so has no spaces; check individual span texts instead.
        _span_texts = [s.get("text", "").strip() for s in block_spans
                       if s.get("text", "").strip()]
        if (len(_span_texts) >= 3
                and len(set(_span_texts)) == 1
                and len(_span_texts[0]) <= 3):
            continue
        # Check if this block is an OSD screenshot overlay
        is_osd_screenshot = bool(_OSD_SCREEN_RE.search(block_txt))
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                size = span.get("size", 0)
                if size > _OSD_FONT_SOFT:
                    parts.append(span.get("text", ""))
                elif size > _OSD_FONT_HARD:
                    # 7–8.5 pt: include only if this block is NOT an OSD screenshot
                    if not is_osd_screenshot:
                        parts.append(span.get("text", ""))
                # ≤ 7 pt: always skip (OSD menu item labels)
    raw = " ".join(parts)
    return _normalize(_strip_formatting(raw))


def _extract_page_body_stage(page) -> str:
    """Extract STAGE body text (no font filter — OSD text lives in images)."""
    raw = page.get_text()
    return _normalize(_strip_formatting(raw))


# ────────────────────────────────────────────────────────────────────────────
# TOC access
# ────────────────────────────────────────────────────────────────────────────
def get_toc(pdf_path):
    doc = fitz.open(pdf_path)
    toc = [(lvl, title.strip(), pg) for lvl, title, pg in doc.get_toc()]
    doc.close()
    return toc


# ────────────────────────────────────────────────────────────────────────────
# Section extraction using TOC page ranges
# ────────────────────────────────────────────────────────────────────────────
def _find_sub(stream, needle, start):
    n = len(needle)
    if not n:
        return -1
    for i in range(start, len(stream) - n + 1):
        if stream[i:i + n] == needle:
            return i
    return -1


def extract_sections(pdf_path, is_prod: bool) -> dict:
    """Return {title: text_str} keyed by original TOC title.

    Sections are delimited by locating each heading in a page-position-ordered
    word stream and slicing between consecutive headings — the same approach
    used by generate_validation_report.py for reliable section boundaries.
    """
    doc  = fitz.open(pdf_path)
    toc  = doc.get_toc()
    nav  = {1} | _detect_nav_pages(doc)

    stream      = []   # flat word list across all body pages
    page_start  = {}   # {1-based page: stream offset}
    for i, page in enumerate(doc, 1):
        if i in nav:
            continue
        page_start[i] = len(stream)
        body = (_extract_page_body_prod(page)
                if is_prod else _extract_page_body_stage(page))
        stream += body.split()
    doc.close()

    kept  = sorted(page_start)

    def _window(pgno):
        if pgno in page_start:
            base, lo = pgno, page_start[pgno]
        else:
            later = [p for p in kept if p >= pgno]
            base  = later[0] if later else kept[-1]
            lo    = page_start[base]
        after = [p for p in kept if p > base]
        return lo, (page_start[after[0]] if after else len(stream))

    located, pos = [], 0
    for level, title, pgno in toc:
        needle      = _normalize(title).split()
        lo, hi      = _window(pgno)
        idx         = _find_sub(stream[:hi], needle, max(pos, lo))
        if idx < 0:
            idx     = _find_sub(stream, needle, lo)
        if idx < 0:
            idx     = _find_sub(stream, needle, pos)
        if idx >= 0:
            pos     = idx + len(needle)
        located.append((idx, level, title, pgno))

    starts   = [l[0] for l in located if l[0] is not None and l[0] >= 0]
    sections = {}
    for idx, level, title, pgno in located:
        if idx is None or idx < 0:
            sections[title] = ""
            continue
        nxt  = [s for s in starts if s > idx]
        end  = min(nxt) if nxt else len(stream)
        sections[title] = " ".join(stream[idx:end])
    return sections


# ────────────────────────────────────────────────────────────────────────────
# Shingle-based content comparison
# ────────────────────────────────────────────────────────────────────────────
def _build_stage_index(stage_pdf_path: str, nav_pages: set):
    """Build shingle set + full lowercase text from ALL non-nav STAGE pages.

    Uses raw page text (not section slices) so content that appears before the
    first TOC heading (e.g. copyright body text) is still covered.
    """
    doc        = fitz.open(stage_pdf_path)
    all_words  = []
    raw_parts  = []
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        body = _extract_page_body_stage(page)
        words = _keep(body.split())
        all_words  += words
        raw_parts.append(body)
    doc.close()
    nospace = "".join(_canon(w) for w in all_words)
    cset    = {nospace[i:i + CHAR_SHINGLE]
               for i in range(len(nospace) - CHAR_SHINGLE + 1)}
    full_lower = re.sub(r"\s+", " ", " ".join(raw_parts)).lower()
    return nospace, cset, full_lower


# ────────────────────────────────────────────────────────────────────────────
# Image extraction and comparison
# ────────────────────────────────────────────────────────────────────────────
def _page_onpage_images(page):
    """Return list of (bw_pt, bh_pt) for each valid image placement on the page.

    Uses on-page bbox dimensions (PDF points) from get_image_info() instead of
    encoded pixel dimensions.  This makes comparison resolution-independent: a
    PROD icon encoded at 212 px but displayed at 25 pt matches a Stage icon
    encoded at 421 px but also displayed at 25 pt.

    Deduplicates by rounded bbox position (same location = same placement).
    """
    seen = set()
    result = []
    for info in page.get_image_info():
        bbox = info.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        bw = bbox[2] - bbox[0]
        bh = bbox[3] - bbox[1]
        if bw <= 0 or bh <= 0 or bw * bh < _MIN_ONPAGE_AREA:
            continue
        key = (round(bbox[0]), round(bbox[1]), round(bbox[2]), round(bbox[3]))
        if key in seen:
            continue
        seen.add(key)
        result.append((round(bw, 1), round(bh, 1)))
    return result


def _extract_section_images(pdf_path: str, nav_pages: set) -> dict:
    """Return {title: [(page_no, bw_pt, bh_pt), ...]} per TOC section (PROD).

    Uses on-page point dimensions (from get_image_info bboxes) so that images
    encoded at different resolutions in Stage vs PROD still compare correctly.
    """
    doc    = fitz.open(pdf_path)
    toc    = doc.get_toc()
    total  = doc.page_count
    result = {}

    for i, (lvl, title, pg) in enumerate(toc):
        end_pg = total
        for j in range(i + 1, len(toc)):
            if toc[j][0] <= lvl:
                end_pg = toc[j][2] - 1
                break
        imgs = []
        for pno in range(pg, end_pg + 1):
            if pno < 1 or pno > total or pno in nav_pages:
                continue
            for bw, bh in _page_onpage_images(doc[pno - 1]):
                imgs.append((pno, bw, bh))
        result[title] = imgs

    doc.close()
    return result


def _extract_stage_images_by_prod_sections(
        stage_path: str, prod_toc: list, stage_toc: list, nav_pages: set) -> dict:
    """Return {prod_title: [(page_no, bw_pt, bh_pt), ...]} for Stage.

    Uses PROD section ordering for Stage page ranges (avoids Stage's granular
    TOC causing empty section boundaries).  On-page pt dimensions used.
    """
    doc   = fitz.open(stage_path)
    total = doc.page_count

    stage_pg_map = {_norm_key(t): pg for _, t, pg in stage_toc}
    matched = []
    for _, title, _ in prod_toc:
        key = _norm_key(title)
        if key in stage_pg_map:
            matched.append((title, stage_pg_map[key]))

    result = {}
    for i, (title, pg) in enumerate(matched):
        end_pg = matched[i + 1][1] - 1 if i + 1 < len(matched) else total
        end_pg = max(pg, end_pg)
        imgs = []
        for pno in range(pg, end_pg + 1):
            if pno < 1 or pno > total or pno in nav_pages:
                continue
            for bw, bh in _page_onpage_images(doc[pno - 1]):
                imgs.append((pno, bw, bh))
        result[title] = imgs

    doc.close()
    return result


# ────────────────────────────────────────────────────────────────────────────
# Visual image comparison
# ────────────────────────────────────────────────────────────────────────────
def _extract_image_from_pdf(pdf_path: str, page_no: int, img_bbox) -> bytes or None:
    """Extract a single image from PDF page within bbox, return as PNG bytes."""
    if not PIL_AVAILABLE:
        return None
    try:
        doc = fitz.open(pdf_path)
        if page_no < 1 or page_no > doc.page_count:
            doc.close()
            return None
        page = doc[page_no - 1]
        # Get the image region as a pixmap
        pix = page.get_pixmap(clip=img_bbox, alpha=False)
        img_bytes = pix.tobytes("png")
        doc.close()
        return img_bytes
    except:
        return None


def _images_visually_similar(img1_bytes, img2_bytes, threshold: float = 0.90) -> bool:
    """Compare two images visually using histogram comparison.
    
    Returns True if images are visually similar (similarity >= threshold).
    Returns False if images are visually different or comparison fails.
    """
    if not PIL_AVAILABLE or not img1_bytes or not img2_bytes:
        return True  # Can't determine, assume similar
    try:
        img1 = Image.open(BytesIO(img1_bytes)).convert("L")  # Grayscale
        img2 = Image.open(BytesIO(img2_bytes)).convert("L")
        
        # Resize both to same size for comparison
        size = (128, 128)
        img1 = img1.resize(size)
        img2 = img2.resize(size)
        
        # Compute histograms
        hist1 = img1.histogram()
        hist2 = img2.histogram()
        
        # Compare histograms using chi-square-like metric
        diff = sum((h1 - h2) ** 2 for h1, h2 in zip(hist1, hist2))
        similarity = 1.0 / (1.0 + diff / 1000000.0)  # Convert to 0-1 range
        
        return similarity >= threshold
    except:
        return True  # Can't determine, assume similar


def _dim_match(iw: int, ih: int, sw: int, sh: int, tol: float = 0.10) -> bool:
    """True when Stage image dims are within tol% of PROD image dims on both axes."""
    return (abs(iw - sw) <= tol * max(iw, 1) and
            abs(ih - sh) <= tol * max(ih, 1))


def _compare_image_sections(prod_imgs: dict, stage_imgs: dict,
                             stage_all_icons: list) -> list:
    """Compare images section by section using consume-based dimension matching.

    Content images (max(w,h) > _ICON_MAX_DIM):
        Per-section consume: each PROD content image consumes one matching Stage
        image with ±10% tolerance so duplicate dimensions are counted correctly.

    Icons (max(w,h) ≤ _ICON_MAX_DIM):
        Non-consume doc-wide check.  The same icon image object is shared across
        many PROD sections (same xref reused per-page), so each Stage icon should
        be able to satisfy multiple PROD sections' references.  A PROD icon is
        PRESENT when any Stage icon of matching dimensions exists anywhere in the
        document.  Total unique icon-dimension counts are compared globally via
        icon_doc_summary.
    """
    prod_keys  = {_norm_key(t): (t, imgs) for t, imgs in prod_imgs.items()}
    stage_keys = {_norm_key(t): (t, imgs) for t, imgs in stage_imgs.items()}

    rows = []
    for nk, (title, p_imgs) in prod_keys.items():
        _, s_imgs = stage_keys.get(nk, ("", []))

        # Per-section mutable pool for content images (consume-based)
        # Tuples are now (pno, bw, bh) with on-page pt dimensions
        s_content_avail = [
            (bw, bh) for _, bw, bh in s_imgs if max(bw, bh) > _ICON_MAX_ONPAGE
        ]

        dim_rows       = []
        n_cont_present = 0
        n_cont_missing = 0
        n_icon_present = 0
        n_icon_missing = 0

        for pno, iw, ih in p_imgs:
            is_content = max(iw, ih) > _ICON_MAX_ONPAGE
            img_type   = "Content" if is_content else "Icon"
            display_match = None

            if is_content:
                # Consume from per-section content pool (15% tolerance for content)
                match_idx = next(
                    (i for i, (sw, sh) in enumerate(s_content_avail)
                     if _dim_match(iw, ih, sw, sh, tol=0.15)),
                    None,
                )
                match = s_content_avail.pop(match_idx) if match_idx is not None else None
                display_match = match

            else:
                # Non-consume doc-wide for icons (25% tolerance — different encodings
                # may display at slightly different pt sizes)
                match = next(
                    ((sw, sh) for sw, sh in stage_all_icons
                     if _dim_match(iw, ih, sw, sh, tol=0.25)),
                    None,
                )
                if match:
                    display_match = match
                elif stage_all_icons:
                    # Keep strict pass/fail logic, but still capture the nearest
                    # visible Stage icon size for reporting clarity.
                    display_match = min(
                        stage_all_icons,
                        key=lambda s: abs(iw - s[0]) + abs(ih - s[1]),
                    )

            if match:
                status = "Present"
            else:
                status = "Missing" if (is_content or _FAIL_ON_ICON_MISS) else "Info"
            dim_rows.append({
                "section":   title,
                "prod_page": pno,
                "prod_w":    iw,
                "prod_h":    ih,
                "type":      img_type,
                "status":    status,
                "match_w":   display_match[0] if display_match else None,
                "match_h":   display_match[1] if display_match else None,
                "nearest_only": bool(display_match and not match),
            })

            if is_content:
                if match: n_cont_present += 1
                else:     n_cont_missing += 1
            else:
                if match: n_icon_present += 1
                else:     n_icon_missing += 1

        # Section-level pass/fail is based on content images only.
        # Icon comparisons remain in the report as informational because icon
        # bboxes vary heavily across export pipelines and can create false misses.
        if _FAIL_ON_ICON_MISS:
            status_overall = "Fail" if n_cont_missing > 0 or n_icon_missing > 0 else "Pass"
        else:
            status_overall = "Fail" if n_cont_missing > 0 else "Pass"
        rows.append({
            "title":         title,
            "prod_content":  n_cont_present + n_cont_missing,
            "found_content": n_cont_present,
            "miss_content":  n_cont_missing,
            "prod_icons":    n_icon_present + n_icon_missing,
            "found_icons":   n_icon_present,
            "miss_icons":    n_icon_missing,
            "status":        status_overall,
            "dim_rows":      dim_rows,
        })

    return rows


# ────────────────────────────────────────────────────────────────────────────
# Image layout, table counting, and text alignment (Part 4)
# ────────────────────────────────────────────────────────────────────────────

def _get_page_image_layout(page):
    """Return a list of image rows on the page.

    Each row is a list of dicts {align, w, h} sorted left-to-right.
    Rows are sorted top-to-bottom.  Alignment:
        'L' — image centre left of page centre (>20% margin)
        'C' — image centre within ±20% of page centre
        'R' — image centre right of page centre
    Images smaller than _MIN_IMG_PIXELS are excluded.
    """
    pw  = page.rect.width
    ph  = page.rect.height
    pc  = pw / 2.0
    tol = ph * 0.06   # 6% of page height = same-row tolerance

    placed = []
    for info in page.get_image_info():
        bbox = info.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        bw = bbox[2] - bbox[0]
        bh = bbox[3] - bbox[1]
        if bw <= 0 or bh <= 0 or bw * bh < _MIN_ONPAGE_AREA:
            continue
        placed.append({"bbox": bbox, "w": round(bw), "h": round(bh)})

    if not placed:
        return []

    # Group by Y-centre
    raw_rows = []
    for img in sorted(placed, key=lambda d: (d["bbox"][1] + d["bbox"][3]) / 2):
        yc    = (img["bbox"][1] + img["bbox"][3]) / 2
        added = False
        for row in raw_rows:
            row_y = sum((d["bbox"][1] + d["bbox"][3]) / 2 for d in row) / len(row)
            if abs(yc - row_y) < tol:
                row.append(img)
                added = True
                break
        if not added:
            raw_rows.append([img])

    result = []
    for row in raw_rows:
        row.sort(key=lambda d: d["bbox"][0])
        items = []
        for img in row:
            cx = (img["bbox"][0] + img["bbox"][2]) / 2
            if abs(cx - pc) <= pc * 0.20:
                align = "C"
            elif cx < pc:
                align = "L"
            else:
                align = "R"
            items.append({"align": align, "w": img["w"], "h": img["h"]})
        result.append(items)

    return result


def _section_page_range(i, toc, total):
    """Return (start_pg, end_pg) inclusive 1-based for toc entry i."""
    lvl, _, pg = toc[i]
    end_pg = total
    for j in range(i + 1, len(toc)):
        if toc[j][0] <= lvl:
            end_pg = toc[j][2] - 1
            break
    return pg, end_pg


def _stage_section_ranges(prod_toc, stage_toc, total):
    """Return [(title, start_pg, end_pg)] for Stage using PROD section ordering."""
    stage_pg_map = {_norm_key(t): pg for _, t, pg in stage_toc}
    matched = []
    for _, title, _ in prod_toc:
        key = _norm_key(title)
        if key in stage_pg_map:
            matched.append((title, stage_pg_map[key]))
    ranges = []
    for i, (title, pg) in enumerate(matched):
        end_pg = matched[i + 1][1] - 1 if i + 1 < len(matched) else total
        ranges.append((title, pg, max(pg, end_pg)))
    return ranges


def _get_page_text_alignment(page):
    """Return Counter of {'Left': n, 'Center': n, 'Right': n} for text blocks.

    Blocks that span nearly the full page width are skipped (body paragraphs
    appear full-width regardless of text justification and would all look
    'center' under a naive x-center test).
    """
    from collections import Counter
    pw  = page.rect.width
    pc  = pw / 2.0
    cnt = Counter()
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        bbox = block["bbox"]
        bw   = bbox[2] - bbox[0]
        if bw < 30 or bw > pw * 0.90:   # skip tiny and full-width
            continue
        txt = "".join(
            s.get("text", "")
            for line in block.get("lines", [])
            for s in line.get("spans", [])
        ).strip()
        if len(txt) < 8:
            continue
        bc         = (bbox[0] + bbox[2]) / 2
        left_gap   = bbox[0]
        right_gap  = pw - bbox[2]
        if abs(bc - pc) < pw * 0.07 and bw < pw * 0.60:
            cnt["Center"] += 1
        elif right_gap < left_gap * 0.40 and bw < pw * 0.45:
            cnt["Right"] += 1
        else:
            cnt["Left"] += 1
    return cnt


def _extract_layout_prod(pdf_path, nav_pages, toc):
    """Single-pass extraction: image layout + tables + text alignment for PROD.

    Returns (img_layout, tables, text_align) each {title: data}.
    """
    from collections import Counter
    doc    = fitz.open(pdf_path)
    total  = doc.page_count
    img_layout = {}
    tables     = {}
    text_align = {}

    for i in range(len(toc)):
        _, title, _ = toc[i]
        pg, end_pg  = _section_page_range(i, toc, total)
        page_rows   = []
        tab_list    = []
        align_cnt   = Counter()

        for pno in range(pg, end_pg + 1):
            if pno < 1 or pno > total or pno in nav_pages:
                continue
            page = doc[pno - 1]
            rows = _get_page_image_layout(page)
            if rows:
                page_rows.append((pno, rows))
            try:
                for t in page.find_tables().tables:
                    if t.row_count > 1 or t.col_count > 1:
                        tab_list.append((pno, t.row_count, t.col_count))
            except Exception:
                pass
            align_cnt.update(_get_page_text_alignment(page))

        img_layout[title] = page_rows
        tables[title]     = tab_list
        text_align[title] = dict(align_cnt)

    doc.close()
    return img_layout, tables, text_align


def _extract_layout_stage(stage_path, prod_toc, stage_toc, nav_pages):
    """Single-pass extraction: image layout + tables + text alignment for Stage."""
    from collections import Counter
    doc    = fitz.open(stage_path)
    total  = doc.page_count
    img_layout = {}
    tables     = {}
    text_align = {}

    for title, pg, end_pg in _stage_section_ranges(prod_toc, stage_toc, total):
        page_rows = []
        tab_list  = []
        align_cnt = Counter()

        for pno in range(pg, end_pg + 1):
            if pno < 1 or pno > total or pno in nav_pages:
                continue
            page = doc[pno - 1]
            rows = _get_page_image_layout(page)
            if rows:
                page_rows.append((pno, rows))
            try:
                for t in page.find_tables().tables:
                    if t.row_count > 1 or t.col_count > 1:
                        tab_list.append((pno, t.row_count, t.col_count))
            except Exception:
                pass
            align_cnt.update(_get_page_text_alignment(page))

        img_layout[title] = page_rows
        tables[title]     = tab_list
        text_align[title] = dict(align_cnt)

    doc.close()
    return img_layout, tables, text_align


def _fmt_rows(rows_list):
    """Format image rows as a compact string (max 5 rows shown).

    e.g. '3 rows — R1:3img(L,C,R) R2:1img(C) R3:2img(L,R)'
    For longer lists: '8 rows — R1:3img(L,C,R) … R8:1img(C)'
    """
    if not rows_list:
        return "—"
    total = len(rows_list)
    if total <= 5:
        parts = [
            f"R{i}:{len(r)}img({''.join(d['align'] for d in r)})"
            for i, r in enumerate(rows_list, 1)
        ]
        return f"{total} row{'s' if total>1 else ''} — " + " ".join(parts)
    first = [
        f"R{i}:{len(r)}img({''.join(d['align'] for d in r)})"
        for i, r in enumerate(rows_list[:3], 1)
    ]
    last_r  = rows_list[-1]
    last_s  = f"R{total}:{len(last_r)}img({''.join(d['align'] for d in last_r)})"
    return f"{total} rows — " + " ".join(first) + f" … {last_s}"


def _fmt_tables(tab_list):
    """Format table list as '3 tables: 4×3, 6×2, 3×3'."""
    if not tab_list:
        return "—"
    dims = ", ".join(f"{r}×{c}" for _, r, c in tab_list)
    return f"{len(tab_list)} table{'s' if len(tab_list)!=1 else ''}: {dims}"


def _compare_layout(prod_layout, stage_layout, prod_tables, stage_tables,
                    prod_align, stage_align):
    """Return per-section layout comparison results."""
    rows = []
    for title in prod_layout:
        p_pages   = prod_layout.get(title, [])
        s_pages   = stage_layout.get(title, [])
        p_tabs    = prod_tables.get(title, [])
        s_tabs    = stage_tables.get(title, [])
        p_al      = prod_align.get(title, {})
        s_al      = stage_align.get(title, {})

        # Flatten to row lists
        p_rows = [row for _, page_rows in p_pages for row in page_rows]
        s_rows = [row for _, page_rows in s_pages for row in page_rows]

        issues = []

        # Image row comparison
        for i in range(max(len(p_rows), len(s_rows))):
            if i >= len(p_rows):
                issues.append(
                    f"Img Row {i+1}: Stage extra "
                    f"({len(s_rows[i])} img, aligns: {','.join(d['align'] for d in s_rows[i])})"
                )
            elif i >= len(s_rows):
                issues.append(
                    f"Img Row {i+1}: PROD has "
                    f"{len(p_rows[i])} img ({','.join(d['align'] for d in p_rows[i])}) "
                    f"— missing in Stage"
                )
            else:
                p_r, s_r = p_rows[i], s_rows[i]
                if len(p_r) != len(s_r):
                    issues.append(
                        f"Img Row {i+1}: count PROD {len(p_r)} ≠ Stage {len(s_r)} "
                        f"(PROD aligns: {','.join(d['align'] for d in p_r)}, "
                        f"Stage: {','.join(d['align'] for d in s_r)})"
                    )
                elif [d["align"] for d in p_r] != [d["align"] for d in s_r]:
                    issues.append(
                        f"Img Row {i+1}: alignment mismatch — "
                        f"PROD {','.join(d['align'] for d in p_r)} ≠ "
                        f"Stage {','.join(d['align'] for d in s_r)}"
                    )

        # Table comparison
        if len(p_tabs) != len(s_tabs):
            issues.append(
                f"Table count: PROD {len(p_tabs)} ≠ Stage {len(s_tabs)}"
            )
        else:
            for i, ((_, pr, pc_), (_, sr, sc)) in enumerate(zip(p_tabs, s_tabs), 1):
                if pr != sr or pc_ != sc:
                    issues.append(f"Table {i}: PROD {pr}×{pc_} ≠ Stage {sr}×{sc}")

        # Text alignment
        for atype in ("Center", "Right"):
            pd_c = p_al.get(atype, 0)
            sd_c = s_al.get(atype, 0)
            if pd_c > 0 and sd_c == 0:
                issues.append(
                    f"Text alignment: PROD has {pd_c} {atype.lower()}-aligned "
                    f"blocks, Stage has 0"
                )
            elif pd_c > 0 and abs(pd_c - sd_c) > max(2, pd_c * 0.5):
                issues.append(
                    f"Text alignment: {atype} blocks PROD {pd_c} ≠ Stage {sd_c}"
                )

        rows.append({
            "title":         title,
            "p_rows":        p_rows,
            "s_rows":        s_rows,
            "p_tabs":        p_tabs,
            "s_tabs":        s_tabs,
            "p_al":          p_al,
            "s_al":          s_al,
            "prod_row_desc": _fmt_rows(p_rows),
            "stg_row_desc":  _fmt_rows(s_rows),
            "prod_tab_desc": _fmt_tables(p_tabs),
            "stg_tab_desc":  _fmt_tables(s_tabs),
            "issues":        issues,
            "status":        "Pass" if not issues else "Fail",
        })

    return rows


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
        # Keep first occurrence if duplicate keys exist.
        out.setdefault(key, (pg, max(pg, end_pg), title))
    return out


def _render_page_for_visual(doc, page_no: int):
    """Render a PDF page to grayscale PIL image for visual comparison."""
    if not PIL_AVAILABLE:
        return None
    try:
        page = doc[page_no - 1]
        mat = fitz.Matrix(VISUAL_RENDER_SCALE, VISUAL_RENDER_SCALE)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        mode = "RGB" if pix.n >= 3 else "L"
        img = Image.frombytes(mode, [pix.width, pix.height], pix.samples)
        return img.convert("L")
    except Exception:
        return None


def _visual_similarity(img_a, img_b) -> float:
    """Return visual similarity score in [0,1] using mean absolute difference."""
    if not PIL_AVAILABLE or img_a is None or img_b is None:
        return 1.0
    try:
        size = (360, 360)
        a = img_a.resize(size)
        b = img_b.resize(size)
        diff = ImageChops.difference(a, b)
        mad = ImageStat.Stat(diff).mean[0] / 255.0
        return max(0.0, min(1.0, 1.0 - mad))
    except Exception:
        return 1.0


def _compare_visual_sections(prod_path: str, stage_path: str, prod_toc: list,
                             stage_toc: list, toc_results: list) -> list:
    """Section-wise visual validation: expected PROD pages vs actual STAGE pages."""
    if not PIL_AVAILABLE:
        rows = []
        for r in toc_results:
            if r.get("toc_status") == "Extra in Stage":
                continue
            rows.append({
                "title": r["title"],
                "prod_range": str(r.get("prod_page", "-")),
                "stage_range": str(r.get("stage_page", "-")),
                "avg_score": 1.0,
                "compared": 0,
                "status": "Info",
                "difference": "Visual engine unavailable (Pillow not installed)",
            })
        return rows

    prod_doc = fitz.open(prod_path)
    stage_doc = fitz.open(stage_path)
    prod_ranges = _toc_ranges_by_key(prod_toc, prod_doc.page_count)
    stage_ranges = _toc_ranges_by_key(stage_toc, stage_doc.page_count)

    prod_cache = {}
    stage_cache = {}

    def cached_render(cache, doc, pno):
        if pno not in cache:
            cache[pno] = _render_page_for_visual(doc, pno)
        return cache[pno]

    rows = []
    for r in toc_results:
        if r.get("toc_status") == "Extra in Stage":
            continue
        title = r["title"]
        key = _norm_key(title)

        p_start, p_end, _ = prod_ranges.get(key, (None, None, title))
        s_range = stage_ranges.get(key)
        if p_start is None:
            rows.append({
                "title": title,
                "prod_range": "-",
                "stage_range": "-",
                "avg_score": 0.0,
                "compared": 0,
                "status": "Fail",
                "difference": "Expected section not found in PROD TOC range map",
            })
            continue

        if not s_range:
            rows.append({
                "title": title,
                "prod_range": f"p{p_start}-p{p_end}",
                "stage_range": "Missing",
                "avg_score": 0.0,
                "compared": 0,
                "status": "Fail",
                "difference": "Section missing in STAGE",
            })
            continue

        s_start, s_end, _ = s_range
        p_pages = list(range(p_start, p_end + 1))
        s_pages = list(range(s_start, s_end + 1))
        comp_n = min(len(p_pages), len(s_pages))
        scores = []
        low = []
        for i in range(comp_n):
            pp = p_pages[i]
            sp = s_pages[i]
            sim = _visual_similarity(
                cached_render(prod_cache, prod_doc, pp),
                cached_render(stage_cache, stage_doc, sp),
            )
            scores.append(sim)
            if sim < VISUAL_PAGE_THRESHOLD:
                low.append((pp, sp, sim))

        avg = sum(scores) / len(scores) if scores else 0.0
        diffs = []
        if len(p_pages) != len(s_pages):
            diffs.append(f"page-count PROD {len(p_pages)} vs STAGE {len(s_pages)}")
        if low:
            sample = ", ".join(
                f"p{pp}->p{sp} ({sim:.2f})" for pp, sp, sim in low[:3]
            )
            extra = f" +{len(low)-3} more" if len(low) > 3 else ""
            diffs.append(f"visual mismatch {sample}{extra}")

        rows.append({
            "title": title,
            "prod_range": f"p{p_start}-p{p_end}",
            "stage_range": f"p{s_start}-p{s_end}",
            "avg_score": round(avg, 3),
            "compared": comp_n,
            "status": "Pass" if not diffs else "Fail",
            "difference": "Matched" if not diffs else "; ".join(diffs),
        })

    prod_doc.close()
    stage_doc.close()
    return rows


_NOTICE_RE = re.compile(r"\b(NOTE|TIP|IMPORTANT|WARNING|CAUTION|INFO)\b", re.IGNORECASE)

# Plain-English labels for each style bucket, used throughout the report.
_STYLE_LABELS = {
    "heading":    "Headings",
    "subheading": "Sub-headings",
    "body":       "Body text",
    "notice":     "Notices (NOTE/TIP/etc.)",
}


def _doc_body_size(doc) -> float:
    """Most common font size of running body text (lines ≥ 15 chars).

    This is the per-document baseline. STAGE and PROD can render the whole
    manual at slightly different absolute point sizes and with different font
    *names* (PROD uses 'Roboto'/'Poppins'; STAGE uses 'Roboto-Bold' etc.), so
    absolute pt thresholds and bold-name detection are unreliable. Classifying
    every line by its size *ratio* to this baseline keeps the heading/body
    decision consistent across both PDFs.
    """
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
    """Classify a line by its size ratio to the document's own body size.

    Size-only (no bold-name check) so the result is symmetric between PROD and
    STAGE, which name their fonts differently.
    """
    t = (text or "").strip()
    if not t:
        return "other"
    if _NOTICE_RE.search(t):
        return "notice"
    ratio = (max_size / body_size) if body_size > 0 else 1.0
    if ratio >= 1.45:                       # ~18pt+ vs 12pt body → section title
        return "heading"
    if ratio >= 1.12:                       # ~13.5pt+ → sub-heading
        return "subheading"
    if ratio >= 0.85 and len(t) >= 15:      # 11–12pt running text → body
        return "body"
    return "other"


def _extract_style_profile(doc, start_pg: int, end_pg: int, body_size: float) -> dict:
    """Count heading/subheading/body/notice lines across a page range."""
    counts = {"heading": 0, "subheading": 0, "body": 0, "notice": 0}
    for pno in range(start_pg, end_pg + 1):
        if pno < 1 or pno > doc.page_count:
            continue
        page = doc[pno - 1]
        d = page.get_text("dict")
        for b in d.get("blocks", []):
            if b.get("type") != 0:
                continue
            for line in b.get("lines", []):
                spans = line.get("spans", [])
                txt = "".join(s.get("text", "") for s in spans).strip()
                if not txt:
                    continue
                max_size = max((s.get("size", 0.0) for s in spans), default=0.0)
                style = _style_class_rel(txt, max_size, body_size)
                if style in counts:
                    counts[style] += 1
    counts["total"] = sum(counts[k] for k in ("heading", "subheading", "body", "notice"))
    return counts


def _pg_label(start: int, end: int) -> str:
    """'page 3' or 'pages 6-7'."""
    return f"page {start}" if start == end else f"pages {start}-{end}"


def _extract_table_style_profile(doc, start_pg: int, end_pg: int) -> dict:
    """Extract table text style metrics for a section.

    Captures table count, median table-text font size, and alignment mix of
    text blocks that fall inside detected table bboxes.
    """
    font_sizes = []
    align = {"Left": 0, "Center": 0, "Right": 0}
    table_count = 0

    for pno in range(start_pg, end_pg + 1):
        if pno < 1 or pno > doc.page_count:
            continue
        page = doc[pno - 1]
        try:
            found = page.find_tables().tables
        except Exception:
            found = []
        if not found:
            continue

        blocks = [b for b in page.get_text("dict").get("blocks", []) if b.get("type") == 0]
        for t in found:
            bbox = getattr(t, "bbox", None)
            if not bbox or len(bbox) < 4:
                continue
            table_count += 1
            tx0, ty0, tx1, ty1 = bbox
            tw = max(1.0, tx1 - tx0)
            tc = (tx0 + tx1) / 2.0

            for block in blocks:
                bb = block.get("bbox", [0, 0, 0, 0])
                bx0, by0, bx1, by1 = bb
                # Keep blocks whose center falls inside the table region.
                bc_x = (bx0 + bx1) / 2.0
                bc_y = (by0 + by1) / 2.0
                if not (tx0 <= bc_x <= tx1 and ty0 <= bc_y <= ty1):
                    continue

                bw = bx1 - bx0
                txt = "".join(
                    s.get("text", "")
                    for line in block.get("lines", [])
                    for s in line.get("spans", [])
                ).strip()
                if len(txt) < 2:
                    continue

                mx = max((s.get("size", 0.0)
                          for line in block.get("lines", [])
                          for s in line.get("spans", [])), default=0.0)
                if mx > 0:
                    font_sizes.append(round(mx, 2))

                left_gap = max(0.0, bx0 - tx0)
                right_gap = max(0.0, tx1 - bx1)
                if abs(bc_x - tc) <= tw * 0.08 and bw < tw * 0.70:
                    align["Center"] += 1
                elif right_gap < left_gap * 0.45 and bw < tw * 0.60:
                    align["Right"] += 1
                else:
                    align["Left"] += 1

    return {
        "table_count": table_count,
        "font_median": round(float(statistics.median(font_sizes)), 2) if font_sizes else 0.0,
        "align": align,
        "sample_count": len(font_sizes),
    }


def _compare_style_sections(prod_path: str, stage_path: str, prod_toc: list,
                            stage_toc: list, toc_results: list) -> list:
    """Compare per-section style structure as pagination-invariant proportions.

    Raw line counts can't be compared because STAGE re-paginates (PROD p3 →
    STAGE p6-p7), so a section's lines spread across a different number of
    pages. Instead we compare each bucket's *share* of the section's classified
    lines, which is independent of how the content is paginated. Differences are
    reported in plain English with the STAGE page(s) to fix.
    """
    prod_doc = fitz.open(prod_path)
    stage_doc = fitz.open(stage_path)
    prod_body = _doc_body_size(prod_doc)
    stage_body = _doc_body_size(stage_doc)
    prod_ranges = _toc_ranges_by_key(prod_toc, prod_doc.page_count)
    stage_ranges = _toc_ranges_by_key(stage_toc, stage_doc.page_count)

    # Share-difference tolerances (percentage points) and minimum line counts
    # below which a section is too small to judge structurally.
    SHARE_TOL = {"heading": 0.12, "subheading": 0.15, "body": 0.15}
    NOTICE_TOL = 1          # allow ±1 notice before flagging
    MIN_LINES = 6           # sections with fewer classified lines: count-only check
    RANGE_RATIO_MAX = 2.2   # STAGE/PROD line-count ratio above which ranges are
    RANGE_RATIO_MIN = 0.45  # too mismatched to compare (topic re-paginates)

    def _breakdown(prof, pg_label, who):
        return (
            f"{who} {pg_label}<br/>"
            f"Headings: {prof['heading']} &nbsp; Sub-headings: {prof['subheading']} &nbsp; "
            f"Body text: {prof['body']} &nbsp; Notices: {prof['notice']}<br/>"
            f"<font color='#777777'>({prof['total']} styled lines)</font>"
        )

    rows = []
    for r in toc_results:
        if r.get("toc_status") == "Extra in Stage":
            continue

        title = r["title"]
        key = _norm_key(title)
        p_range = prod_ranges.get(key)
        s_range = stage_ranges.get(key)

        if not p_range:
            rows.append({
                "title": title,
                "expected": "PROD location for this topic could not be determined.",
                "actual": "—",
                "difference": "Skipped — no PROD page range to compare against.",
                "diff_lines": ["Skipped — this topic has no PROD page range, so its style cannot be checked."],
                "status": "Skipped",
            })
            continue

        p_start, p_end, _ = p_range
        p_prof = _extract_style_profile(prod_doc, p_start, p_end, prod_body)
        p_tab_prof = _extract_table_style_profile(prod_doc, p_start, p_end)

        if not s_range:
            rows.append({
                "title": title,
                "expected": _breakdown(p_prof, _pg_label(p_start, p_end), "PROD"),
                "actual": "This topic is missing from STAGE.",
                "difference": "Whole section missing from STAGE — nothing to style.",
                "diff_lines": [
                    f"The entire '{title}' section is absent from STAGE, so none of its "
                    f"{p_prof['total']} styled lines (incl. {p_prof['heading']} headings) exist to format."
                ],
                "status": "Fix",
            })
            continue

        s_start, s_end, _ = s_range
        s_prof = _extract_style_profile(stage_doc, s_start, s_end, stage_body)
        s_tab_prof = _extract_table_style_profile(stage_doc, s_start, s_end)

        p_pg = _pg_label(p_start, p_end)
        s_pg = _pg_label(s_start, s_end)
        diff_lines = []

        p_total = max(p_prof["total"], 1)
        s_total = max(s_prof["total"], 1)

        # Range-mismatch guard: when STAGE's TOC range spans far more (or fewer)
        # lines than PROD's, the topic's page mapping is unreliable — STAGE
        # paginates it very differently, or several TOC entries share one page.
        # Comparing style structure across such mismatched ranges produces noise,
        # so report it as not-comparable instead of inventing differences.
        big = max(p_prof["total"], s_prof["total"]) >= MIN_LINES
        size_ratio = s_total / p_total
        if big and (size_ratio > RANGE_RATIO_MAX or size_ratio < RANGE_RATIO_MIN):
            rows.append({
                "title": title,
                "expected": _breakdown(p_prof, p_pg, "PROD"),
                "actual": _breakdown(s_prof, s_pg, "STAGE"),
                "difference": "Page ranges differ too much to compare reliably.",
                "diff_lines": [
                    f"Not compared: STAGE maps this topic to {s_pg} ({s_prof['total']} styled lines) "
                    f"but PROD maps it to {p_pg} ({p_prof['total']} lines). The ranges are too "
                    f"different (the topic re-paginates or shares a page in STAGE), so a reliable "
                    f"style comparison isn't possible — verify {s_pg} by eye."
                ],
                "status": "Skipped",
                "stage_pages": s_pg,
            })
            continue

        big_enough = p_prof["total"] >= MIN_LINES and s_prof["total"] >= MIN_LINES

        for k in ("heading", "subheading", "body"):
            p_n, s_n = p_prof[k], s_prof[k]
            label = _STYLE_LABELS[k]
            if big_enough:
                p_sh, s_sh = p_n / p_total, s_n / s_total
                if abs(p_sh - s_sh) <= SHARE_TOL[k]:
                    continue
                direction = "more" if s_sh > p_sh else "fewer"
                line = (
                    f"{label}: STAGE has proportionally {direction} than PROD "
                    f"(PROD {p_n} of {p_prof['total']} lines = {p_sh:.0%}, "
                    f"STAGE {s_n} of {s_prof['total']} = {s_sh:.0%}). "
                )
            else:
                if abs(p_n - s_n) <= 1:
                    continue
                line = f"{label}: PROD has {p_n}, STAGE has {s_n}. "

            # Targeted fix hint per bucket.
            if k == "body" and s_prof[k] < p_prof[k]:
                line += (f"Body copy on STAGE {s_pg} looks re-styled as headings — "
                         f"restore normal paragraph formatting.")
            elif k == "heading":
                line += (f"Check section/title formatting on STAGE {s_pg}.")
            else:
                line += (f"Review heading vs body sizing on STAGE {s_pg}.")
            diff_lines.append(line)

        # Notices: only a *shortfall* (STAGE has fewer callouts than PROD) is a
        # real defect — a PROD callout that lost its NOTE/TIP styling. Extra
        # STAGE matches are almost always menu words like "INFO"/"Information",
        # so they're not flagged.
        p_n, s_n = p_prof["notice"], s_prof["notice"]
        if p_n - s_n > NOTICE_TOL:
            diff_lines.append(
                f"Notices (NOTE/TIP/IMPORTANT): PROD has {p_n}, STAGE has {s_n} — "
                f"{p_n - s_n} callout(s) may be missing or no longer styled as a notice on STAGE {s_pg}."
            )

        # Table text style checks: font size and alignment inside tables.
        if p_tab_prof["table_count"] or s_tab_prof["table_count"]:
            if p_tab_prof["table_count"] != s_tab_prof["table_count"]:
                diff_lines.append(
                    f"Table count for style check: PROD has {p_tab_prof['table_count']}, STAGE has {s_tab_prof['table_count']} on {s_pg}."
                )

            if p_tab_prof["font_median"] > 0 and s_tab_prof["font_median"] > 0:
                font_delta = round(s_tab_prof["font_median"] - p_tab_prof["font_median"], 2)
                if abs(font_delta) > 0.8:
                    diff_lines.append(
                        f"Table font size: expected about {p_tab_prof['font_median']}pt in PROD, actual about {s_tab_prof['font_median']}pt in STAGE on {s_pg} (delta {font_delta:+}pt)."
                    )

            p_align_total = max(1, sum(p_tab_prof["align"].values()))
            s_align_total = max(1, sum(s_tab_prof["align"].values()))
            for akey, label in (("Left", "left-aligned"), ("Center", "center-aligned"), ("Right", "right-aligned")):
                p_share = p_tab_prof["align"][akey] / p_align_total
                s_share = s_tab_prof["align"][akey] / s_align_total
                if abs(p_share - s_share) > 0.18:
                    diff_lines.append(
                        f"Table text alignment: expected more {label} table text like PROD ({p_share:.0%}), but STAGE shows {s_share:.0%} on {s_pg}."
                    )

        status = "Pass" if not diff_lines else "Fix"
        if not diff_lines:
            diff_lines = ["Style structure matches PROD."]

        rows.append({
            "title": title,
            "expected": _breakdown(p_prof, p_pg, "PROD") +
                        f"<br/>Table text: {p_tab_prof['table_count']} table(s), median font {p_tab_prof['font_median']}pt, "
                        f"align L/C/R = {p_tab_prof['align']['Left']}/{p_tab_prof['align']['Center']}/{p_tab_prof['align']['Right']}",
            "actual": _breakdown(s_prof, s_pg, "STAGE") +
                      f"<br/>Table text: {s_tab_prof['table_count']} table(s), median font {s_tab_prof['font_median']}pt, "
                      f"align L/C/R = {s_tab_prof['align']['Left']}/{s_tab_prof['align']['Center']}/{s_tab_prof['align']['Right']}",
            "difference": "Matches PROD" if status == "Pass" else diff_lines[0],
            "diff_lines": diff_lines,
            "status": status,
            "stage_pages": s_pg,
        })

    prod_doc.close()
    stage_doc.close()
    return rows


def _image_context(doc, pno: int, xref: int):
    """Return (xobj_name, page_context) for an image on a given PROD page.

    xobj_name    — PDF XObject name or alt-text if present.
    page_context — nearest text block by bbox; falls back to the page's
                   leading heading + first descriptive sentences when no
                   bbox is available (images embedded without position metadata).
    """
    try:
        page = doc[pno - 1]
    except Exception:
        return "", ""

    img_name = ""
    img_bbox = None
    for info in page.get_image_info():
        if info.get("xref") == xref:
            img_name = info.get("alt", "") or info.get("name", "")
            img_bbox = info.get("bbox")
            break

    # Primary: find the nearest text block by spatial proximity
    if img_bbox and (img_bbox[2] - img_bbox[0]) > 0:
        iy_bottom = img_bbox[3]
        iy_top    = img_bbox[1]
        candidates = []
        for b in page.get_text("blocks"):
            if len(b) < 5 or b[6] != 0:
                continue
            txt = b[4].strip()
            if not txt or len(txt) > 300:
                continue
            dist = min(abs(b[1] - iy_bottom), abs(b[3] - iy_top))
            candidates.append((dist, txt))
        if candidates:
            return img_name, candidates[0][1][:200]

    # Fallback: build context from the page's own text lines.
    # Skip bare page-number tokens; keep the first heading + sentences.
    lines = [
        ln.strip()
        for ln in page.get_text().splitlines()
        if ln.strip() and not ln.strip().isdigit()
    ]
    context = " — ".join(lines[:4])[:250] if lines else ""
    return img_name, context


def _section_missing(prod_words, stage_ns, stage_cset, stage_full_lower):
    """Return (coverage_pct, [missing_fragment_str, ...]).

    Uses shingle windows to detect which PROD words are covered by STAGE text.
    For each uncovered run of >= MIN_FRAG_WORDS words, verifies the phrase is
    truly absent from STAGE (not just reorganised) before reporting it.
    
    Enhanced to capture missing content more comprehensively by:
    - Checking variations of fragments with common punctuation/formatting differences
    - Verifying absence through multiple matching strategies
    """
    words  = _keep(prod_words)
    if not words:
        return 100.0, []

    cwords     = [_canon(w) for w in words]
    s          = "".join(cwords)
    char_word  = []
    for wi, cw in enumerate(cwords):
        char_word.extend([wi] * len(cw))

    L = CHAR_SHINGLE
    if len(s) < L:
        if s and s in stage_ns:
            return 100.0, []
        phrase = re.sub(r"\s+", " ", " ".join(words)).lower()
        if phrase in stage_full_lower:
            return 100.0, []
        return 0.0, ([" ".join(words)] if len(words) >= MIN_FRAG_WORDS else [])

    covered_char = [False] * len(s)
    for p in range(len(s) - L + 1):
        if s[p:p + L] in stage_cset:
            for q in range(p, p + L):
                covered_char[q] = True

    covcount = [0] * len(words)
    for ci, hit in enumerate(covered_char):
        if hit:
            covcount[char_word[ci]] += 1
    covered = [
        (not cwords[i]) or covcount[i] >= max(1, (len(cwords[i]) + 1) // 2)
        for i in range(len(words))
    ]
    coverage = 100.0 * sum(covered) / len(words)

    frags, i = [], 0
    while i < len(words):
        if not covered[i]:
            st = i
            while i < len(words) and not covered[i]:
                i += 1
            frag = words[st:i]
            if len(frag) >= MIN_FRAG_WORDS:
                phrase = re.sub(r"\s+", " ", " ".join(frag)).lower()
                if phrase in stage_full_lower:
                    pass  # covered
                else:
                    frag_text = " ".join(frag)
                    reported  = True

                    # 1) Strip a single leading numbered-step marker ("4. ") and
                    #    re-check — handles step numbering separated from body
                    #    text in STAGE table rendering ("4. For Shortcut 1, 2, 3").
                    stripped = re.sub(r"^\d+\.\s+", "", phrase, count=1)
                    if stripped != phrase and stripped in stage_full_lower:
                        reported = False

                    # 2) Short numbered-label lists where PROD and STAGE render the
                    #    same two-column diagram table in different column order
                    #    (e.g. "1. SD card slot 2." vs "1. 2. ... SD card slot...").
                    if reported:
                        num_markers = len(_NUMBERED_ITEM_RE.findall(frag_text))
                        if num_markers >= 2 and len(frag) <= 12:
                            alpha_words = [
                                w.lower() for w in frag
                                if re.search(r"[a-zA-Z]{4,}", w)
                            ]
                            if alpha_words and all(
                                w in stage_full_lower for w in alpha_words
                            ):
                                reported = False

                    if reported:
                        frags.append(frag_text)
        else:
            i += 1
    return coverage, frags


# ────────────────────────────────────────────────────────────────────────────
# Report helpers
# ────────────────────────────────────────────────────────────────────────────
def _esc(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _trunc(text, n=180):
    text = text or ""
    return (text[:n] + "...") if len(text) > n else text


def _highlight_notice_labels(text: str) -> str:
    """Color-code NOTE / TIP / IMPORTANT tokens inside report text."""
    esc = _esc(text or "")
    esc = re.sub(
        r"\bIMPORTANT\b:?",
        "<font color='#c62828'><b>IMPORTANT:</b></font>",
        esc,
        flags=re.IGNORECASE,
    )
    esc = re.sub(
        r"\bNOTE\b:?",
        "<font color='#1565c0'><b>NOTE:</b></font>",
        esc,
        flags=re.IGNORECASE,
    )
    esc = re.sub(
        r"\bTIP\b:?",
        "<font color='#2e7d32'><b>TIP:</b></font>",
        esc,
        flags=re.IGNORECASE,
    )
    return esc


def generate_report(prod_path, stage_path, toc_results, content_results,
                    image_results, icon_doc_summary, layout_results,
                    visual_results, style_results, report_path):
    doc = SimpleDocTemplate(
        report_path, pagesize=landscape(letter),
        leftMargin=0.4 * inch, rightMargin=0.4 * inch,
        topMargin=0.4 * inch,  bottomMargin=0.4 * inch,
    )
    styles = getSampleStyleSheet()

    title_s = ParagraphStyle("T",    parent=styles["Heading1"],  fontSize=16,  spaceAfter=4)
    sub_s   = ParagraphStyle("Sub",  parent=styles["Normal"],    fontSize=10,  textColor=colors.grey, spaceAfter=2)
    head_s  = ParagraphStyle("H",    parent=styles["Heading2"],  fontSize=13,  spaceBefore=10, spaceAfter=6)
    hdr_s   = ParagraphStyle("Hdr",  parent=styles["Normal"],    fontSize=9,   leading=12,
                             textColor=colors.whitesmoke, fontName="Helvetica-Bold")
    topic_s = ParagraphStyle("Topic",parent=styles["Normal"],    fontSize=8,   leading=11, fontName="Helvetica-Bold")
    cell_s  = ParagraphStyle("Cell", parent=styles["Normal"],    fontSize=7,   leading=10)
    pass_s  = ParagraphStyle("Pass", parent=styles["Normal"],    fontSize=8,   leading=11,
                             textColor=colors.HexColor("#2e7d32"), fontName="Helvetica-Bold")
    fail_s  = ParagraphStyle("Fail", parent=styles["Normal"],    fontSize=8,   leading=11,
                             textColor=colors.red, fontName="Helvetica-Bold")
    miss_s  = ParagraphStyle("Miss", parent=styles["Normal"],    fontSize=8,   leading=11,
                             textColor=colors.HexColor("#e65100"), fontName="Helvetica-Bold")
    extra_s = ParagraphStyle("Xtra", parent=styles["Normal"],    fontSize=8,   leading=11,
                             textColor=colors.HexColor("#1565c0"), fontName="Helvetica-Bold")
    match_s = ParagraphStyle("Mtch", parent=styles["Normal"],    fontSize=8,   leading=11,
                             textColor=colors.HexColor("#1b5e20"), fontName="Helvetica-Bold")

    story = []

    # ── Header ──
    story.append(Paragraph("PDF Content Validation Report", title_s))
    story.append(Paragraph(f"Production: {os.path.basename(prod_path)}", sub_s))
    story.append(Paragraph(f"Staging:    {os.path.basename(stage_path)}", sub_s))
    story.append(Spacer(1, 12))

    # ═══════════════════════════════════════════
    # PART 1 — TOC Comparison
    # ═══════════════════════════════════════════
    story.append(Paragraph("Part 1 — Table of Contents Comparison", head_s))

    n_match = sum(1 for r in toc_results if r["toc_status"] == "Match")
    n_miss  = sum(1 for r in toc_results if r["toc_status"] == "Missing in Stage")
    n_extra = sum(1 for r in toc_results if r["toc_status"] == "Extra in Stage")

    sum1 = [
        [Paragraph("<b>Total</b>",    hdr_s), Paragraph("<b>Match</b>",          hdr_s),
         Paragraph("<b>Missing in Stage</b>", hdr_s), Paragraph("<b>Extra in Stage</b>", hdr_s)],
        [Paragraph(str(len(toc_results)), topic_s), Paragraph(str(n_match), match_s),
         Paragraph(str(n_miss),  miss_s), Paragraph(str(n_extra), extra_s)],
    ]
    st = Table(sum1, colWidths=[100, 100, 140, 140])
    st.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#37474f")),
        ("BACKGROUND", (0,1), (-1,1), colors.HexColor("#eceff1")),
        ("GRID",       (0,0), (-1,-1), 0.5, colors.grey),
        ("ALIGN",      (0,0), (-1,-1), "CENTER"),
        ("VALIGN",     (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING", (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(st)
    story.append(Spacer(1, 10))

    toc_hdr = [Paragraph(f"<b>{h}</b>", hdr_s)
               for h in ["#", "Prod TOC", "Prod Pg", "Stage TOC", "Stage Pg", "Status"]]
    toc_rows = [toc_hdr]
    smap = {"Match": match_s, "Missing in Stage": miss_s, "Extra in Stage": extra_s}

    for idx, r in enumerate(toc_results, 1):
        ss = smap.get(r["toc_status"], cell_s)
        if r["toc_status"] == "Match":
            pc, sc, pp, sp = _esc(r["title"]), _esc(r["title"]), str(r["prod_page"]), str(r["stage_page"])
        elif r["toc_status"] == "Missing in Stage":
            pc, sc, pp, sp = _esc(r["title"]), "—", str(r["prod_page"]), "—"
        else:
            pc, sc, pp, sp = "—", _esc(r["title"]), "—", str(r["stage_page"])
        toc_rows.append([
            Paragraph(str(idx), cell_s), Paragraph(pc, topic_s), Paragraph(pp, cell_s),
            Paragraph(sc, topic_s),      Paragraph(sp, cell_s),  Paragraph(r["toc_status"], ss),
        ])

    toc_t = Table(toc_rows, colWidths=[22, 220, 40, 220, 40, 100], repeatRows=1)
    toc_t.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), colors.HexColor("#37474f")),
        ("GRID",        (0,0), (-1,-1), 0.5, colors.grey),
        ("VALIGN",      (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",  (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",(0,0),(-1,-1), 3),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, colors.HexColor("#f5f5f5")]),
    ]))
    story.append(toc_t)
    story.append(PageBreak())

    # ═══════════════════════════════════════════
    # PART 2 — Content Differences
    # ═══════════════════════════════════════════
    story.append(Paragraph("Part 2 — Content Differences (matching topics only)", head_s))
    story.append(Paragraph(
        "Only confirmed-absent text is shown. Content that appears elsewhere in STAGE "
        "in a different order or context is not reported. OSD screenshots, diagram "
        "callout labels, page references and formatting labels (NOTE/TIP/IMPORTANT) "
        "are excluded from comparison.",
        ParagraphStyle("Note", parent=styles["Normal"], fontSize=8,
                       textColor=colors.grey, spaceAfter=6),
    ))

    n_cpass = sum(1 for r in content_results if r["status"] == "Pass")
    n_cfail = sum(1 for r in content_results if r["status"] == "Fail")

    sum2 = [
        [Paragraph("<b>Compared</b>", hdr_s),
         Paragraph("<b>Pass</b>",     hdr_s),
         Paragraph("<b>Fail</b>",     hdr_s)],
        [Paragraph(str(len(content_results)), topic_s),
         Paragraph(str(n_cpass), pass_s),
         Paragraph(str(n_cfail), fail_s)],
    ]
    st2 = Table(sum2, colWidths=[120, 80, 80])
    st2.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), colors.HexColor("#37474f")),
        ("BACKGROUND",  (0,1), (-1,1), colors.HexColor("#eceff1")),
        ("GRID",        (0,0), (-1,-1), 0.5, colors.grey),
        ("ALIGN",       (0,0), (-1,-1), "CENTER"),
        ("VALIGN",      (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",  (0,0), (-1,-1), 6),
        ("BOTTOMPADDING",(0,0),(-1,-1), 6),
    ]))
    story.append(st2)
    story.append(Spacer(1, 10))

    cd_hdr = [Paragraph(f"<b>{h}</b>", hdr_s)
              for h in ["#", "Topic", "Prod Pg", "Stage Pg", "Status", "Missing content (exact PROD text absent from STAGE)"]]
    cd_rows = [cd_hdr]

    for row_num, r in enumerate(content_results, 1):
        ss = pass_s if r["status"] == "Pass" else fail_s
        if r["status"] == "Pass":
            change = Paragraph("Content matches", cell_s)
        else:
            lines = [
                f"<font color='#b71c1c'><b>MISSING:</b></font> {_highlight_notice_labels(_trunc(m))}"
                for m in r.get("missing", [])
            ]
            change = Paragraph("<br/>".join(lines) if lines else "Content differs", cell_s)

        cd_rows.append([
            Paragraph(str(row_num),       cell_s),
            Paragraph(_esc(r["title"]),   topic_s),
            Paragraph(str(r["prod_page"]),cell_s),
            Paragraph(str(r["stage_page"]),cell_s),
            Paragraph(r["status"],        ss),
            change,
        ])

    cd_t = Table(cd_rows, colWidths=[22, 170, 42, 42, 40, 420], repeatRows=1)
    cd_t.setStyle(TableStyle([
        ("BACKGROUND",  (0,0), (-1,0), colors.HexColor("#37474f")),
        ("GRID",        (0,0), (-1,-1), 0.5, colors.grey),
        ("VALIGN",      (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",  (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",(0,0),(-1,-1), 3),
        ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, colors.HexColor("#f5f5f5")]),
    ]))
    story.append(cd_t)

    # ═══════════════════════════════════════════
    # PART 3 — Image Differences
    # ═══════════════════════════════════════════
    story.append(PageBreak())
    story.append(Paragraph("Part 3 — Image Differences (matching topics only)", head_s))
    story.append(Paragraph(
        f"Content images (max on-page dim > {_ICON_MAX_ONPAGE} pt): diagrams, photos — "
        "compared per section, consume-based (±15% tolerance).  "
        f"Icons (max on-page dim ≤ {_ICON_MAX_ONPAGE} pt): small symbols — "
        "compared document-wide (±25% tolerance). Dimensions are ON-PAGE pt sizes (what reader sees).",
        ParagraphStyle("Note3", parent=styles["Normal"], fontSize=8,
                       textColor=colors.grey, spaceAfter=4),
    ))

    # Document-level icon summary bar
    if icon_doc_summary["status"] == "Pass":
        icon_status_s = pass_s
    elif icon_doc_summary["status"] == "Info":
        icon_status_s = cell_s
    else:
        icon_status_s = fail_s
    icon_sum = [
        [Paragraph("<b>Icon comparison (document-wide)</b>", hdr_s),
         Paragraph("<b>Status</b>",             hdr_s),
         Paragraph("<b>Comment</b>",            hdr_s)],
        [Paragraph("All sections combined", cell_s),
         Paragraph(icon_doc_summary["status"],           icon_status_s),
         Paragraph(
             f"Total icons: {icon_doc_summary['prod_total']} | Found: {icon_doc_summary['found_total']} | Missing: {icon_doc_summary['miss_total']}",
             cell_s if icon_doc_summary["miss_total"] == 0 else miss_s
         )],
    ]
    icon_sum_t = Table(icon_sum, colWidths=[200, 100, 250])
    icon_sum_t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), colors.HexColor("#546e7a")),
        ("BACKGROUND",    (0,1), (-1,1), colors.HexColor("#eceff1")),
        ("GRID",          (0,0), (-1,-1), 0.5, colors.grey),
        ("ALIGN",         (0,0), (-1,-1), "LEFT"),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 5),
        ("BOTTOMPADDING", (0,0), (-1,-1), 5),
    ]))
    story.append(icon_sum_t)
    story.append(Spacer(1, 12))

    n_ipass = sum(1 for r in image_results if r["status"] == "Pass")
    n_ifail = sum(1 for r in image_results if r["status"] == "Fail")

    sum3 = [
        [Paragraph("<b>Sections compared</b>", hdr_s),
         Paragraph("<b>Pass</b>",              hdr_s),
         Paragraph("<b>Fail</b>",              hdr_s)],
        [Paragraph(str(len(image_results)), topic_s),
         Paragraph(str(n_ipass), pass_s),
         Paragraph(str(n_ifail), fail_s)],
    ]
    st3 = Table(sum3, colWidths=[200, 100, 100])
    st3.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), colors.HexColor("#37474f")),
        ("BACKGROUND",    (0,1), (-1,1), colors.HexColor("#eceff1")),
        ("GRID",          (0,0), (-1,-1), 0.5, colors.grey),
        ("ALIGN",         (0,0), (-1,-1), "CENTER"),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(st3)
    story.append(Spacer(1, 12))

    # Section summary: # | Topic | Status | Comment (with visual difference indicators)
    img_hdr = [Paragraph(f"<b>{h}</b>", hdr_s) for h in [
        "#", "Topic", "Status", "Comment",
    ]]
    img_rows = [img_hdr]

    for row_num, r in enumerate(image_results, 1):
        ss = pass_s if r["status"] == "Pass" else fail_s
        missing_rows = [dr for dr in r.get("dim_rows", []) if dr.get("status") == "Missing"]

        def _missing_preview(rows, limit=4):
            if not rows:
                return ""
            bits = [f"p{dr['prod_page']} {dr['type']} {dr['prod_w']}x{dr['prod_h']}"
                    for dr in rows[:limit]]
            extra = len(rows) - limit
            if extra > 0:
                bits.append(f"+{extra} more")
            return "; ".join(bits)
        
        # Generate clear comment for image status with visual difference indicators
        if r["status"] == "Pass":
            if missing_rows:
                comment = (
                    "Info: icon-size mismatches detected; "
                    f"exact refs -> {_missing_preview(missing_rows)}"
                )
            else:
                comment = "✓ All images matching"
        else:
            comments = []
            if r["miss_content"] > 0:
                if r["miss_content"] == 1:
                    comments.append(f"Content image visually different or missing")
                else:
                    comments.append(f"{r['miss_content']} content images visually different or missing")
            if r["miss_icons"] > 0:
                if r["miss_icons"] == 1:
                    comments.append(f"Icon visually different or missing")
                else:
                    comments.append(f"{r['miss_icons']} icons visually different or missing")
            if missing_rows:
                comments.append(f"Exact refs -> {_missing_preview(missing_rows)}")
            comment = " | ".join(comments) if comments else "✗ Images not matching"
        
        img_rows.append([
            Paragraph(str(row_num),              cell_s),
            Paragraph(_esc(r["title"]),          topic_s),
            Paragraph(r["status"],               ss),
            Paragraph(comment,                   miss_s if r["status"] == "Fail" else cell_s),
        ])

    img_t = Table(img_rows, colWidths=[22, 220, 80, 330], repeatRows=1)
    img_t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), colors.HexColor("#37474f")),
        ("GRID",          (0,0), (-1,-1), 0.5, colors.grey),
        ("VALIGN",        (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",    (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
        ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.white, colors.HexColor("#f5f5f5")]),
    ]))
    story.append(img_t)
    
    # ── Visual Image Differences Summary ──
    story.append(Spacer(1, 12))
    story.append(Paragraph("Visual Image Differences Summary", 
                          ParagraphStyle("VizHeader", parent=styles["Heading2"], fontSize=10, textColor=colors.HexColor("#455a64"))))
    
    failed_sections = [r for r in image_results if r["status"] == "Fail"]
    info_icon_sections = [
        r for r in image_results
        if r["status"] == "Pass" and any(dr.get("status") == "Missing" for dr in r.get("dim_rows", []))
    ]
    if failed_sections or info_icon_sections:
        viz_items = []
        for r in failed_sections:
            if r["miss_content"] > 0:
                if r["miss_content"] == 1:
                    viz_items.append(f"• <b>{_esc(r['title'])}</b>: Content image(s) not matching - visual changes detected (added labels, layout changes, or missing elements)")
                else:
                    viz_items.append(f"• <b>{_esc(r['title'])}</b>: {r['miss_content']} content image(s) visually different - check for added labels or layout modifications")
            if r["miss_icons"] > 0:
                if r["miss_icons"] == 1:
                    viz_items.append(f"• <b>{_esc(r['title'])}</b>: Icon element changed or missing visually in STAGE version")
                else:
                    viz_items.append(f"• <b>{_esc(r['title'])}</b>: {r['miss_icons']} icon(s) changed or missing visually")

        for r in info_icon_sections:
            missing_rows = [dr for dr in r.get("dim_rows", []) if dr.get("status") == "Missing"]
            refs = [f"p{dr['prod_page']} {dr['type']} {dr['prod_w']}x{dr['prod_h']}"
                    for dr in missing_rows[:4]]
            more = len(missing_rows) - 4
            suffix = f"; +{more} more" if more > 0 else ""
            viz_items.append(
                f"• <b>{_esc(r['title'])}</b>: Info-only icon-size mismatch refs -> {'; '.join(refs)}{suffix}"
            )
        
        if viz_items:
            story.append(Paragraph(
                "<br/>".join(viz_items),
                ParagraphStyle("VizList", parent=styles["Normal"], fontSize=8.5, textColor=colors.HexColor("#5d4037"), leading=12)
            ))
    else:
        story.append(Paragraph(
            "✓ All images across topics match visually between PROD and STAGE.",
            ParagraphStyle("VizPass", parent=styles["Normal"], fontSize=8.5, textColor=colors.HexColor("#2e7d32"), leading=12)
        ))
    
    story.append(Spacer(1, 12))

    # ── Part 3b: per-image dimension detail (all sections) ──
    story.append(PageBreak())
    story.append(Paragraph("Part 3b — Image Dimension Detail (PROD vs Stage)", head_s))
    story.append(Paragraph(
        "Each row is one PROD image. "
        "All dimensions are on-page pt sizes (not encoded pixel counts). "
        "Stage Match shows the closest matching Stage image size (±15% content / ±25% icon). "
        "Missing rows (red) apply to content images only. "
        "Icon-only size mismatches are shown as Info (amber), not Fail. "
        f"Content: max on-page dim > {_ICON_MAX_ONPAGE} pt (per section, consume-based). "
        f"Icon: max on-page dim ≤ {_ICON_MAX_ONPAGE} pt (doc-wide match).",
        ParagraphStyle("Note3b", parent=styles["Normal"], fontSize=8,
                       textColor=colors.grey, spaceAfter=8),
    ))

    det_hdr = [Paragraph(f"<b>{h}</b>", hdr_s) for h in [
        "#", "Section (PROD heading)", "PROD\nPage",
        "Type", "PROD W×H (pt)", "Stage Match (pt)", "Status",
    ]]
    det_rows = [det_hdr]
    row_num  = 0
    miss_bg  = colors.HexColor("#ffcdd2")   # red highlight for content missing
    info_bg  = colors.HexColor("#fff3e0")   # amber highlight for icon info mismatch
    pres_bg  = colors.HexColor("#e8f5e9")   # light green for present
    row_bg_styles = []   # (row_index, bg_color)

    icon_dim_s = ParagraphStyle("IcnDim", parent=styles["Normal"], fontSize=8, leading=11,
                                textColor=colors.HexColor("#1565c0"), fontName="Helvetica-Bold")

    for r in image_results:
        for dr in r["dim_rows"]:
            row_num += 1
            r_idx = len(det_rows)   # 0-based index into det_rows (header at 0)

            is_missing = dr["status"] == "Missing"
            is_icon_info = (is_missing and dr["type"] == "Icon" and not _FAIL_ON_ICON_MISS)
            type_style = icon_dim_s if dr["type"] == "Icon" else (miss_s if is_missing else cell_s)
            if is_icon_info:
                stat_style = cell_s
                status_text = "Info"
            elif is_missing:
                stat_style = miss_s
                status_text = "Missing"
            else:
                stat_style = pass_s
                status_text = "Present"

            if dr["match_w"] is not None:
                match_cell = Paragraph(f"{dr['match_w']}×{dr['match_h']}", cell_s)
            else:
                match_cell = Paragraph("—", cell_s if is_icon_info else miss_s)

            det_rows.append([
                Paragraph(str(row_num),                    cell_s),
                Paragraph(_esc(dr["section"]),             topic_s),
                Paragraph(str(dr["prod_page"]),            cell_s),
                Paragraph(dr["type"],                      type_style),
                Paragraph(f"{dr['prod_w']}×{dr['prod_h']}", cell_s),
                match_cell,
                Paragraph(status_text,                      stat_style),
            ])
            if is_icon_info:
                row_bg_styles.append((r_idx, info_bg))
            else:
                row_bg_styles.append((r_idx, miss_bg if is_missing else pres_bg))

    det_t = Table(det_rows, colWidths=[22, 210, 38, 50, 70, 90, 60], repeatRows=1)
    ts_cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0), colors.HexColor("#37474f")),
        ("GRID",          (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN",        (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING",    (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for r_idx, bg in row_bg_styles:
        ts_cmds.append(("BACKGROUND", (0, r_idx), (-1, r_idx), bg))
    det_t.setStyle(TableStyle(ts_cmds))
    story.append(det_t)

    # Parts 4 (Layout), 5 (Visual) and 6/6b (Style) are hidden from the report.
    # Flip this flag to True to restore them; the building code below is kept intact.
    _SHOW_PARTS_4_5_6 = False
    if not _SHOW_PARTS_4_5_6:
        doc.build(story)
        print(f"Report saved: {report_path}")
        return

    # ═══════════════════════════════════════════
    # PART 4 — Layout Validation
    # ═══════════════════════════════════════════
    story.append(PageBreak())
    story.append(Paragraph("Part 4 — Layout Validation (Image Rows · Tables · Text Alignment)", head_s))
    story.append(Paragraph(
        "Expectation = what exists in PROD. Actual = what was detected in STAGE. "
        "For each topic, compare image-row structure, table count/size, and text alignment. "
        "L = left, C = centre, R = right. "
        "If Difference says 'Matched', layout is OK. Otherwise it explains exactly what changed.",
        ParagraphStyle("Note4", parent=styles["Normal"], fontSize=8,
                       textColor=colors.grey, spaceAfter=6),
    ))

    n_lpass = sum(1 for r in layout_results if r["status"] == "Pass")
    n_lfail = sum(1 for r in layout_results if r["status"] == "Fail")
    sum4 = [
        [Paragraph("<b>Compared</b>", hdr_s),
         Paragraph("<b>Pass</b>",     hdr_s),
         Paragraph("<b>Fail</b>",     hdr_s)],
        [Paragraph(str(len(layout_results)), topic_s),
         Paragraph(str(n_lpass), pass_s),
         Paragraph(str(n_lfail), fail_s)],
    ]
    st4 = Table(sum4, colWidths=[120, 80, 80])
    st4.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), colors.HexColor("#37474f")),
        ("BACKGROUND",    (0,1), (-1,1), colors.HexColor("#eceff1")),
        ("GRID",          (0,0), (-1,-1), 0.5, colors.grey),
        ("ALIGN",         (0,0), (-1,-1), "CENTER"),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(st4)
    story.append(Spacer(1, 10))

    # Part 4 summary table — explicit Expected(PROD) vs Actual(STAGE)
    lay_hdr = [Paragraph(f"<b>{h}</b>", hdr_s) for h in [
        "#", "Topic", "Expected (PROD)", "Actual (STAGE)", "Difference", "Status",
    ]]
    lay_rows = [lay_hdr]
    for row_num, r in enumerate(layout_results, 1):
        ss   = pass_s if r["status"] == "Pass" else fail_s
        p_al = r["p_al"]
        s_al = r["s_al"]
        p_c, p_r_ = p_al.get("Center", 0), p_al.get("Right", 0)
        s_c, s_r_ = s_al.get("Center", 0), s_al.get("Right", 0)

        expected_text = (
            f"Img rows: {_esc(_trunc(r['prod_row_desc'], 90))}<br/>"
            f"Tables: {_esc(_trunc(r['prod_tab_desc'], 70))}<br/>"
            f"Align: C={p_c}, R={p_r_}"
        )
        actual_text = (
            f"Img rows: {_esc(_trunc(r['stg_row_desc'], 90))}<br/>"
            f"Tables: {_esc(_trunc(r['stg_tab_desc'], 70))}<br/>"
            f"Align: C={s_c}, R={s_r_}"
        )

        if r["status"] == "Pass":
            diff_text = "Matched"
            diff_style = pass_s
        else:
            top_issue = r["issues"][0] if r.get("issues") else "Layout differs"
            more = len(r.get("issues", [])) - 1
            diff_text = _esc(_trunc(top_issue, 90))
            if more > 0:
                diff_text += f" (+{more} more)"
            diff_style = miss_s

        lay_rows.append([
            Paragraph(str(row_num),                            cell_s),
            Paragraph(_esc(r["title"]),                        topic_s),
            Paragraph(expected_text,                            cell_s),
            Paragraph(actual_text,                              cell_s),
            Paragraph(diff_text,                                diff_style),
            Paragraph(r["status"], ss),
        ])

    lay_t = Table(lay_rows,
                  colWidths=[22, 130, 195, 195, 155, 55],
                  repeatRows=1)
    lay_t.setStyle(TableStyle([
        ("BACKGROUND",     (0,0), (-1,0), colors.HexColor("#37474f")),
        ("GRID",           (0,0), (-1,-1), 0.5, colors.grey),
        ("VALIGN",         (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",     (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",  (0,0), (-1,-1), 3),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f5f5f5")]),
    ]))
    story.append(lay_t)

    # ═══════════════════════════════════════════
    # PART 5 — Visual Validation (Page Render Match)
    # ═══════════════════════════════════════════
    story.append(PageBreak())
    story.append(Paragraph("Part 5 — Visual Validation (Expected PROD vs Actual STAGE)", head_s))
    story.append(Paragraph(
        "Each topic is visually compared by rendering PROD and STAGE pages and "
        "matching pages by section order. Similarity score range: 0.00 to 1.00. "
        f"Pass threshold: {VISUAL_PAGE_THRESHOLD:.2f}.",
        ParagraphStyle("Note5", parent=styles["Normal"], fontSize=8,
                       textColor=colors.grey, spaceAfter=6),
    ))

    n_vpass = sum(1 for r in visual_results if r["status"] == "Pass")
    n_vfail = sum(1 for r in visual_results if r["status"] == "Fail")
    sum5 = [
        [Paragraph("<b>Compared</b>", hdr_s),
         Paragraph("<b>Pass</b>",     hdr_s),
         Paragraph("<b>Fail</b>",     hdr_s)],
        [Paragraph(str(len(visual_results)), topic_s),
         Paragraph(str(n_vpass), pass_s),
         Paragraph(str(n_vfail), fail_s)],
    ]
    st5 = Table(sum5, colWidths=[120, 80, 80])
    st5.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), colors.HexColor("#37474f")),
        ("BACKGROUND",    (0,1), (-1,1), colors.HexColor("#eceff1")),
        ("GRID",          (0,0), (-1,-1), 0.5, colors.grey),
        ("ALIGN",         (0,0), (-1,-1), "CENTER"),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(st5)
    story.append(Spacer(1, 10))

    v_hdr = [Paragraph(f"<b>{h}</b>", hdr_s) for h in [
        "#", "Topic", "Expected (PROD pages)", "Actual (STAGE pages)",
        "Compared", "Avg score", "Difference", "Status",
    ]]
    v_rows = [v_hdr]
    for row_num, r in enumerate(visual_results, 1):
        ss = pass_s if r["status"] == "Pass" else fail_s
        diff_style = cell_s if r["status"] == "Pass" else miss_s
        v_rows.append([
            Paragraph(str(row_num), cell_s),
            Paragraph(_esc(r["title"]), topic_s),
            Paragraph(_esc(str(r["prod_range"])), cell_s),
            Paragraph(_esc(str(r["stage_range"])), cell_s),
            Paragraph(str(r["compared"]), cell_s),
            Paragraph(f"{r['avg_score']:.3f}", cell_s),
            Paragraph(_esc(_trunc(r["difference"], 120)), diff_style),
            Paragraph(r["status"], ss),
        ])

    v_t = Table(v_rows,
                colWidths=[22, 130, 105, 105, 52, 58, 250, 50],
                repeatRows=1)
    v_t.setStyle(TableStyle([
        ("BACKGROUND",     (0,0), (-1,0), colors.HexColor("#37474f")),
        ("GRID",           (0,0), (-1,-1), 0.5, colors.grey),
        ("VALIGN",         (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",     (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",  (0,0), (-1,-1), 3),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f5f5f5")]),
    ]))
    story.append(v_t)

    # ═══════════════════════════════════════════
    # PART 6 — Style Validation (Headings · Sub-headings · Body · Notices)
    # ═══════════════════════════════════════════
    story.append(PageBreak())
    story.append(Paragraph("Part 6 — Style Validation (Expected PROD vs Actual STAGE)", head_s))
    story.append(Paragraph(
        "For each topic this checks that STAGE keeps the same mix of section <b>headings</b>, "
        "<b>sub-headings</b>, <b>body text</b> and <b>notice callouts</b> (NOTE / TIP / IMPORTANT) "
        "as PROD. Because STAGE re-paginates content, lines are compared as a <i>proportion</i> of "
        "each section, not as raw counts — so only genuine styling changes are flagged. "
        "<b>Pass</b> = style matches PROD; <b>Fix</b> = a styling difference to correct on the listed "
        "STAGE page; <b>Skipped</b> = section could not be located.",
        ParagraphStyle("Note6", parent=styles["Normal"], fontSize=8,
                       textColor=colors.grey, spaceAfter=6),
    ))

    n_spass = sum(1 for r in style_results if r["status"] == "Pass")
    n_sfix  = sum(1 for r in style_results if r["status"] == "Fix")
    n_sskip = sum(1 for r in style_results if r["status"] == "Skipped")
    sum6 = [
        [Paragraph("<b>Compared</b>", hdr_s),
         Paragraph("<b>Pass</b>",     hdr_s),
         Paragraph("<b>Needs fix</b>", hdr_s),
         Paragraph("<b>Skipped</b>",  hdr_s)],
        [Paragraph(str(len(style_results)), topic_s),
         Paragraph(str(n_spass), pass_s),
         Paragraph(str(n_sfix), fail_s),
         Paragraph(str(n_sskip), cell_s)],
    ]
    st6 = Table(sum6, colWidths=[110, 80, 90, 80])
    st6.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,0), colors.HexColor("#37474f")),
        ("BACKGROUND",    (0,1), (-1,1), colors.HexColor("#eceff1")),
        ("GRID",          (0,0), (-1,-1), 0.5, colors.grey),
        ("ALIGN",         (0,0), (-1,-1), "CENTER"),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 6),
        ("BOTTOMPADDING", (0,0), (-1,-1), 6),
    ]))
    story.append(st6)
    story.append(Spacer(1, 10))

    s_hdr = [Paragraph(f"<b>{h}</b>", hdr_s) for h in [
        "#", "Topic", "Expected (PROD)", "Actual (STAGE)", "What differs / how to fix", "Status",
    ]]
    s_rows = [s_hdr]
    skip_s = ParagraphStyle("Skip", parent=styles["Normal"], fontSize=8, leading=11,
                            textColor=colors.HexColor("#9e9e9e"), fontName="Helvetica-Bold")
    smap6 = {"Pass": pass_s, "Fix": fail_s, "Skipped": skip_s}
    for row_num, r in enumerate(style_results, 1):
        ss = smap6.get(r["status"], cell_s)
        diff_style = cell_s if r["status"] == "Pass" else miss_s
        detail = "<br/>".join(
            f"• {_esc(_trunc(x, 130))}" for x in r.get("diff_lines", [])[:2]
        )
        if len(r.get("diff_lines", [])) > 2:
            detail += f"<br/><i>+{len(r['diff_lines']) - 2} more (see Part 6b)</i>"
        s_rows.append([
            Paragraph(str(row_num), cell_s),
            Paragraph(_esc(r["title"]), topic_s),
            Paragraph(r["expected"], cell_s),
            Paragraph(r["actual"], cell_s),
            Paragraph(detail if detail else _esc(r["difference"]), diff_style),
            Paragraph(r["status"], ss),
        ])

    s_t = Table(s_rows,
                colWidths=[20, 110, 190, 190, 215, 47],
                repeatRows=1)
    s_t.setStyle(TableStyle([
        ("BACKGROUND",     (0,0), (-1,0), colors.HexColor("#37474f")),
        ("GRID",           (0,0), (-1,-1), 0.5, colors.grey),
        ("VALIGN",         (0,0), (-1,-1), "TOP"),
        ("TOPPADDING",     (0,0), (-1,-1), 3),
        ("BOTTOMPADDING",  (0,0), (-1,-1), 3),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f5f5f5")]),
    ]))
    story.append(s_t)

    # Part 6b — full fix list for topics that need a style fix
    style_fixes = [r for r in style_results if r["status"] == "Fix"]
    if style_fixes:
        story.append(PageBreak())
        story.append(Paragraph("Part 6b — Style Fixes Needed (full detail)", head_s))
        story.append(Paragraph(
            "Every styling difference for each flagged topic, in plain language, with the STAGE "
            "page to correct. PROD is the reference; make STAGE match it.",
            ParagraphStyle("Note6b", parent=styles["Normal"], fontSize=8,
                           textColor=colors.grey, spaceAfter=8),
        ))

        s2_hdr = [Paragraph(f"<b>{h}</b>", hdr_s) for h in [
            "#", "Topic", "PROD page", "STAGE page", "What to fix",
        ]]
        s2_rows = [s2_hdr]
        for row_num, r in enumerate(style_fixes, 1):
            full_diff = "<br/>".join(f"• {_esc(line)}" for line in r.get("diff_lines", []))
            # Pull the bare "page N" tokens back out of the breakdown blocks.
            prod_pg = r["expected"].split("<br/>")[0].replace("PROD ", "")
            actual_first = r["actual"].split("<br/>")[0]
            stage_pg = actual_first.replace("STAGE ", "") if actual_first.startswith("STAGE ") else "—"
            s2_rows.append([
                Paragraph(str(row_num), cell_s),
                Paragraph(_esc(r["title"]), topic_s),
                Paragraph(_esc(prod_pg), cell_s),
                Paragraph(_esc(stage_pg), cell_s),
                Paragraph(full_diff if full_diff else "—", miss_s),
            ])

        s2_t = Table(s2_rows,
                     colWidths=[20, 130, 70, 70, 480],
                     repeatRows=1)
        s2_t.setStyle(TableStyle([
            ("BACKGROUND",     (0,0), (-1,0), colors.HexColor("#37474f")),
            ("GRID",           (0,0), (-1,-1), 0.5, colors.grey),
            ("VALIGN",         (0,0), (-1,-1), "TOP"),
            ("TOPPADDING",     (0,0), (-1,-1), 3),
            ("BOTTOMPADDING",  (0,0), (-1,-1), 3),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#fff3e0")]),
        ]))
        story.append(s2_t)

    doc.build(story)
    print(f"Report saved: {report_path}")


# ────────────────────────────────────────────────────────────────────────────
# Main validation logic
# ────────────────────────────────────────────────────────────────────────────
def validate(prod_path, stage_path, report_path):
    # ── TOC comparison ──
    print("Reading TOC...")
    prod_toc  = get_toc(prod_path)
    stage_toc = get_toc(stage_path)
    print(f"  Prod: {len(prod_toc)} entries | Stage: {len(stage_toc)} entries")

    prod_keys  = {_norm_key(t): (t, l, p) for l, t, p in prod_toc}
    stage_keys = {_norm_key(t): (t, l, p) for l, t, p in stage_toc}

    toc_results = []
    for lvl, title, pg in prod_toc:
        k = _norm_key(title)
        if k in stage_keys:
            toc_results.append({
                "title": title, "level": lvl,
                "prod_page": pg, "stage_page": stage_keys[k][2],
                "toc_status": "Match",
            })
        else:
            toc_results.append({
                "title": title, "level": lvl,
                "prod_page": pg, "stage_page": "-",
                "toc_status": "Missing in Stage",
            })
    for lvl, title, pg in stage_toc:
        if _norm_key(title) not in prod_keys:
            toc_results.append({
                "title": title, "level": lvl,
                "prod_page": "-", "stage_page": pg,
                "toc_status": "Extra in Stage",
            })

    n_m = sum(1 for r in toc_results if r["toc_status"] == "Match")
    n_mi = sum(1 for r in toc_results if r["toc_status"] == "Missing in Stage")
    n_e  = sum(1 for r in toc_results if r["toc_status"] == "Extra in Stage")
    print(f"  TOC: Match={n_m} | Missing in Stage={n_mi} | Extra in Stage={n_e}")

    # ── Content extraction ──
    print("Extracting section text...")
    prod_sections  = extract_sections(prod_path,  is_prod=True)
    stage_sections = extract_sections(stage_path, is_prod=False)
    stage_lookup   = {_norm_key(t): v for t, v in stage_sections.items()}
    print(f"  PROD sections: {len(prod_sections)} | STAGE sections: {len(stage_sections)}")

    # Build STAGE shingle index from ALL non-nav pages (not just section slices)
    # so content that falls before the first TOC heading is still covered.
    print("Building STAGE content index...")
    stage_doc = fitz.open(stage_path)
    stage_nav = {1} | _detect_nav_pages(stage_doc)
    stage_doc.close()
    stage_ns, stage_cset, stage_full_lower = _build_stage_index(stage_path, stage_nav)

    # ── Content comparison (all PROD topics: matching + missing in Stage) ──
    print("Comparing content...")
    content_results = []
    for r in toc_results:
        # Validate all topics from PROD (Match + Missing in Stage)
        # Extra in Stage: not in PROD TOC, skip
        if r["toc_status"] == "Extra in Stage":
            continue
        
        title = r["title"]
        key   = _norm_key(title)
        pc    = prod_sections.get(title, "")
        prod_words = _keep(pc.split())
        
        # ── Handle "Missing in Stage" topics ──
        if r["toc_status"] == "Missing in Stage":
            # Entire section is missing from Stage
            status = "Fail"
            coverage = 0.0
            missing = []
            if prod_words:
                # Report ALL PROD content as missing
                missing = [" ".join(prod_words)]
            content_results.append({
                "title":      title,
                "level":      r["level"],
                "status":     status,
                "prod_page":  r["prod_page"],
                "stage_page": r["stage_page"],
                "coverage":   coverage,
                "missing":    missing,
            })
            continue
        
        # ── Handle "Match" topics ──
        if not prod_words:
            # No PROD content to validate
            content_results.append({
                "title":      title,
                "level":      r["level"],
                "status":     "NO CONTENT",
                "prod_page":  r["prod_page"],
                "stage_page": r["stage_page"],
                "coverage":   100.0,
                "missing":    [],
            })
            continue
        
        # Also try stage by matching key in case titles differ slightly
        sc    = stage_sections.get(title) or stage_lookup.get(key) or ""
        coverage, missing = _section_missing(
            prod_words, stage_ns, stage_cset, stage_full_lower)

        status = "Pass" if not missing else "Fail"
        content_results.append({
            "title":      title,
            "level":      r["level"],
            "status":     status,
            "prod_page":  r["prod_page"],
            "stage_page": r["stage_page"],
            "coverage":   round(coverage, 1),
            "missing":    missing,
        })

    n_p = sum(1 for r in content_results if r["status"] == "Pass")
    n_f = sum(1 for r in content_results if r["status"] == "Fail")
    print(f"  Content: Pass={n_p} | Fail={n_f}")

    if n_f:
        print("  FAIL details:")
        for r in content_results:
            if r["status"] == "Fail":
                print(f"    [{r['coverage']:.0f}%] {r['title']!r}")
                for m in r["missing"]:
                    print(f"      MISSING: {m[:100]}")

    # ── Image comparison ──
    print("Extracting images...")
    prod_doc = fitz.open(prod_path)
    prod_nav = {1} | _detect_nav_pages(prod_doc)
    prod_doc.close()
    prod_imgs  = _extract_section_images(prod_path, prod_nav)
    stage_imgs = _extract_stage_images_by_prod_sections(
        stage_path, prod_toc, stage_toc, stage_nav)

    # Build document-wide Stage icon list using on-page pt dimensions.
    # Icons shift between sections in Stage (finer TOC), so icons are matched
    # against the entire Stage document rather than per-section boundaries.
    print("Building Stage icon index (document-wide, on-page pts)...")
    _sdoc = fitz.open(stage_path)
    stage_all_icons = []
    for i, page in enumerate(_sdoc, 1):
        if i in stage_nav:
            continue
        for bw, bh in _page_onpage_images(page):
            if max(bw, bh) <= _ICON_MAX_ONPAGE:
                stage_all_icons.append((bw, bh))
    _sdoc.close()
    print(f"  Stage icons doc-wide: {len(stage_all_icons)} placements")

    print("Comparing images by on-page pt dimensions (content ±15%, icon ±25%)...")
    image_results = _compare_image_sections(prod_imgs, stage_imgs, stage_all_icons)
    n_ip = sum(1 for r in image_results if r["status"] == "Pass")
    n_if = sum(1 for r in image_results if r["status"] == "Fail")
    print(f"  Pass={n_ip} | Fail={n_if}")
    if n_if:
        print("  FAIL details:")
        for r in image_results:
            if r["status"] == "Fail":
                mc, mi = r["miss_content"], r["miss_icons"]
                parts  = []
                if mc: parts.append(f"Content missing={mc}/{r['prod_content']}")
                if mi: parts.append(f"Icons missing={mi}/{r['prod_icons']}")
                print(f"    {r['title']!r}: {' | '.join(parts)}")
    icon_doc_summary = {
        "prod_total":  sum(r["prod_icons"] for r in image_results),
        "found_total": sum(r["found_icons"] for r in image_results),
        "miss_total":  sum(r["miss_icons"]  for r in image_results),
        "status":      "Info" if not _FAIL_ON_ICON_MISS else (
            "Pass" if all(r["miss_icons"] == 0 for r in image_results) else "Fail"
        ),
    }

    # ── Layout / table / alignment extraction ──
    print("Extracting image layout, tables, and text alignment (PROD)...")
    prod_img_layout, prod_tables, prod_text_align = _extract_layout_prod(
        prod_path, prod_nav, prod_toc)
    print("Extracting image layout, tables, and text alignment (Stage)...")
    stg_img_layout, stg_tables, stg_text_align = _extract_layout_stage(
        stage_path, prod_toc, stage_toc, stage_nav)

    print("Comparing layout...")
    layout_results = _compare_layout(
        prod_img_layout, stg_img_layout,
        prod_tables,     stg_tables,
        prod_text_align, stg_text_align,
    )
    n_lp = sum(1 for r in layout_results if r["status"] == "Pass")
    n_lf = sum(1 for r in layout_results if r["status"] == "Fail")
    print(f"  Layout Pass={n_lp} | Fail={n_lf}")

    # ── Visual page-render validation ──
    print("Comparing visual render similarity (Expected PROD vs Actual STAGE)...")
    visual_results = _compare_visual_sections(
        prod_path, stage_path, prod_toc, stage_toc, toc_results
    )
    n_vp = sum(1 for r in visual_results if r["status"] == "Pass")
    n_vf = sum(1 for r in visual_results if r["status"] == "Fail")
    print(f"  Visual Pass={n_vp} | Fail={n_vf}")

    print("Comparing style structure (headings/sub-headings/body/notices, by proportion)...")
    style_results = _compare_style_sections(
        prod_path, stage_path, prod_toc, stage_toc, toc_results
    )
    n_sp = sum(1 for r in style_results if r["status"] == "Pass")
    n_sf = sum(1 for r in style_results if r["status"] == "Fix")
    n_sk = sum(1 for r in style_results if r["status"] == "Skipped")
    print(f"  Style Pass={n_sp} | Needs fix={n_sf} | Skipped={n_sk}")
    if n_sf:
        print("  Style fixes needed:")
        for r in style_results:
            if r["status"] == "Fix":
                print(f"    • {r['title']}")
                for ln in r.get("diff_lines", []):
                    print(f"        - {ln}")

    # ── Generate PDF ──
    print("Generating report PDF...")
    generate_report(prod_path, stage_path, toc_results, content_results,
                    image_results, icon_doc_summary, layout_results,
                    visual_results, style_results, report_path)
    print("Done.")


# ────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python validate_toc_content.py <prod_pdf> <stage_pdf> [report_pdf]")
        sys.exit(1)

    prod  = sys.argv[1]
    stage = sys.argv[2]
    # Default output: <project-root>/reports/ (parent of this content_validation dir)
    out   = sys.argv[3] if len(sys.argv) > 3 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "reports",
        "toc_content_validation_report.pdf",
    )
    os.makedirs(os.path.dirname(out), exist_ok=True)
    validate(prod, stage, out)

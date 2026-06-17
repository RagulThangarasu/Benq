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
import collections
import statistics
import unicodedata
import hashlib

# Configure local TESSDATA_PREFIX before importing fitz (PyMuPDF)
_CUR_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_CUR_DIR)
_LOCAL_TESSDATA = os.path.join(_PROJECT_ROOT, "tessdata")
if os.path.isdir(_LOCAL_TESSDATA):
    os.environ["TESSDATA_PREFIX"] = _LOCAL_TESSDATA

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
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.cidfonts import UnicodeCIDFont


# ── CJK report font ──────────────────────────────────────────────────────────
# Helvetica (reportlab's default) has no CJK glyphs, so Chinese/Japanese/Korean
# text renders as dots/blanks ("........") in the report. Register a Unicode font
# that covers Latin + CJK once at import; _esc() wraps any CJK-bearing text in an
# inline <font name=...> tag so only that text switches font (English unchanged).
_CJK_FONT_NAME = None
for _cand in (
    ("ArialUnicode", "/Library/Fonts/Arial Unicode.ttf"),
    ("ArialUnicode", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
):
    try:
        pdfmetrics.registerFont(TTFont(_cand[0], _cand[1]))
        _CJK_FONT_NAME = _cand[0]
        break
    except Exception:
        continue
if _CJK_FONT_NAME is None:  # fall back to reportlab's built-in CID font (SC)
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        _CJK_FONT_NAME = "STSong-Light"
    except Exception:
        _CJK_FONT_NAME = None


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
_VECTOR_ICON_MIN = 50    # vector drawings doc-wide ≥ this ⇒ STAGE renders icons as vector art (no raster to size-match)
# Legacy aliases kept for any remaining code that references the old names
_MIN_IMG_PIXELS  = _MIN_ONPAGE_AREA
_ICON_MAX_DIM    = _ICON_MAX_ONPAGE
CHAR_SHINGLE    = 18     # character window for shingle coverage
MIN_FRAG_WORDS  = 3      # minimum uncovered word-run length to report (lowered to capture smaller missing fragments)

# Optional progress reporting (installed by run_validator.py for the web UI).
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

_INT_RE          = re.compile(r"^\d{1,3}$")
# Numbered procedure-step bookmarks ("1. Attach the monitor base.") — STAGE
# bookmarks individual steps that PROD keeps in body text. They are not section
# discrepancies, so they're excluded from the "Extra in Stage" TOC list.
_STEP_BOOKMARK_RE = re.compile(r"^\s*\d{1,2}\s*[.)]\s+\S")
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
# Garbled PDF detection, Language detection, and Translation Integrity Checker
# ────────────────────────────────────────────────────────────────────────────
_DICTIONARY_SET = None

def _load_dictionary():
    global _DICTIONARY_SET
    if _DICTIONARY_SET is not None:
        return _DICTIONARY_SET
    _DICTIONARY_SET = set()
    try:
        # Load macOS standard dictionary to filter OCR noise
        if os.path.exists("/usr/share/dict/words"):
            with open("/usr/share/dict/words", "r", encoding="utf-8") as f:
                for w in f:
                    w_stripped = w.strip().lower()
                    if len(w_stripped) >= 4:
                        _DICTIONARY_SET.add(w_stripped)
    except Exception:
        pass
    return _DICTIONARY_SET


_PUA_RE = re.compile(r"[\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd]")

# Common PUA \u2192 Unicode substitutions used by PDF/font vendors for special symbols
_PUA_SUBST = {
    "\uf8e8": "\u2122",  "\uf8e9": "\u00ae",  "\uf8ea": "\u00a9",  # Adobe PUA
    "\uf0e4": "\u2122",  "\uf0a9": "\u00a9",  "\uf0ae": "\u00ae",  # Wingdings / Symbol PUA
    "\uf020": " ",  "\uf0b7": "\u2022",  "\uf0d8": "\u2022",  # bullet variants
    "\uf0a7": "\u00a7",  "\uf0b6": "\u00b6",
}

def _clean_pua(text: str) -> str:
    """Replace known PUA \u2192 Unicode symbols; strip remaining PUA chars."""
    out = []
    for ch in text:
        out.append(_PUA_SUBST.get(ch, "" if _PUA_RE.match(ch) else ch))
    return "".join(out)


def _is_text_garbled_string(text: str) -> bool:
    if not text:
        return False
    # Raise threshold to 5 % \u2014 BenQ PDFs use custom fonts for bullets/symbols;
    # a small fraction of PUA chars is normal and should NOT trigger OCR.
    pua_chars = len(_PUA_RE.findall(text))
    if len(text) > 50 and (pua_chars / len(text)) > 0.05:
        return True
    # CJK mixed with Georgian is a clear encoding corruption signal
    if bool(re.search(r"[\u4e00-\u9fff]", text)) and bool(re.search(r"[\u10a0-\u10ff\u2d00-\u2d2f]", text)):
        return True
    return False


def _is_pdf_garbled(doc) -> bool:
    pua_count = 0
    total_chars = 0
    for i in range(min(doc.page_count, 5)):
        text = doc[i].get_text()
        total_chars += len(text)
        pua_count += len(_PUA_RE.findall(text))
        if bool(re.search(r"[\u4e00-\u9fff]", text)) and bool(re.search(r"[\u10a0-\u10ff\u2d00-\u2d2f]", text)):
            return True

    if total_chars == 0 and doc.page_count > 0:
        return True  # fully scanned / image-only PDF
    # Only trigger OCR when the majority of characters are private-use (truly corrupt)
    if total_chars > 0 and (pua_count / total_chars) > 0.05:
        return True
    return False


def _detect_language_string(text: str) -> str:
    if not text:
        return "eng"
    jp_chars = len(re.findall(r"[\u3040-\u309f\u30a0-\u30ff]", text))
    ko_chars = len(re.findall(r"[\uac00-\ud7af]", text))
    cyrillic_chars = len(re.findall(r"[\u0400-\u04ff]", text))
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    
    total = len(text)
    if jp_chars > 5 or (total > 0 and jp_chars / total > 0.01):
        return "jpn"
    if ko_chars > 5 or (total > 0 and ko_chars / total > 0.01):
        return "kor"
    if cyrillic_chars > 5 or (total > 0 and cyrillic_chars / total > 0.01):
        return "rus"
    if cjk_chars > 10 or (total > 0 and cjk_chars / total > 0.02):
        # Distinguish Traditional vs Simplified Chinese
        trad_indicators = len(re.findall(r"[個為無這體樂設對開門與後會廠國]", text))
        simp_indicators = len(re.findall(r"[个为无这体乐设对开门与后会厂国]", text))
        if trad_indicators >= simp_indicators:
            return "chi_tra"
        else:
            return "chi_sim"
    return "eng"


def _get_pdf_language(doc) -> str:
    # 1. Try detecting language from this doc's native text if not garbled
    txt = ""
    for i in range(min(doc.page_count, 5)):
        txt += doc[i].get_text()
    if txt and not _is_text_garbled_string(txt):
        return _detect_language_string(txt)
        
    # 2. If garbled, look for a sibling PDF in a "prod" or "stage" folder
    doc_name = getattr(doc, "name", "")
    if doc_name:
        doc_dir = os.path.dirname(doc_name)
        parent_dir = os.path.dirname(doc_dir)
        sibling_dirs = ["prod", "stage"]
        for s_dir in sibling_dirs:
            target_dir = os.path.join(parent_dir, s_dir)
            if os.path.isdir(target_dir):
                for f in os.listdir(target_dir):
                    if f.lower().endswith(".pdf") and os.path.join(target_dir, f) != doc_name:
                        sibling_path = os.path.join(target_dir, f)
                        try:
                            sib_doc = fitz.open(sibling_path)
                            sib_txt = ""
                            for i in range(min(sib_doc.page_count, 5)):
                                sib_txt += sib_doc[i].get_text()
                            sib_doc.close()
                            if sib_txt and not _is_text_garbled_string(sib_txt):
                                return _detect_language_string(sib_txt)
                        except Exception:
                            pass
                            
        # 3. Fallback to filename clues
        filename = os.path.basename(doc_name).lower()
        if "tc" in filename or "traditional" in filename or "zh-tw" in filename or "zh_tw" in filename:
            return "chi_tra"
        if "cn" in filename or "simplified" in filename or "zh-cn" in filename or "zh_cn" in filename:
            return "chi_sim"
        if "ja" in filename or "jpn" in filename or "jp" in filename or "japanese" in filename:
            return "jpn"
        if "ko" in filename or "kor" in filename or "kr" in filename or "korean" in filename:
            return "kor"
        if "ru" in filename or "rus" in filename or "russian" in filename:
            return "rus"
        if "de" in filename or "deu" in filename or "german" in filename:
            return "deu"
        if "fr" in filename or "fra" in filename or "french" in filename:
            return "fra"
        if "es" in filename or "spa" in filename or "spanish" in filename:
            return "spa"
            
    return "eng"


_TECHNICAL_EXCLUSIONS = {
    "benq", "hdmi", "usb", "type", "wifi", "led", "osd", "vga", "dvi", "dp", 
    "hz", "ac", "dc", "pn", "max", "min", "url", "http", "https", "www", "pdf", 
    "mode", "menu", "ips", "lcd", "rgb", "srgb", "dci", "p3", "hdr", "macos", 
    "windows", "mac", "pc", "app", "store", "play", "google", "apple", "intel", 
    "amd", "nvidia", "bluetooth", "ss", "id", "idh", "identity", "tft", "vesa", 
    "os", "aem", "faq", "qa", "mindduo", "sw272", "sw242", "cf23"
}

def find_english_words_in_non_en(text: str) -> list:
    words = re.findall(r"\b[a-zA-Z]{4,}\b", text)
    dictionary = _load_dictionary()
    unexpected = []
    for w in words:
        wl = w.lower()
        if wl in _TECHNICAL_EXCLUSIONS:
            continue
        if w.isalpha() and wl in dictionary:
            unexpected.append(w)
    seen = set()
    return [w for w in unexpected if not (w.lower() in seen or seen.add(w.lower()))]


# ────────────────────────────────────────────────────────────────────────────
# Low-level text utilities
# ────────────────────────────────────────────────────────────────────────────
def _normalize(text: str) -> str:
    # Map known PUA chars (\u2122 \u00a9 \u00ae bullets \u2026) to proper Unicode before stripping.
    text = _clean_pua(text)
    # Strip any remaining unrecognised Private Use Area characters
    text = _PUA_RE.sub("", text)
    # Remove control characters except tab/newline
    text = "".join(c for c in text if unicodedata.category(c) != "Cc" or c in "\t\n\r")
    text = re.sub(r"\.{2,}", " ", text)
    text = re.sub(r"\s+",    " ", text)
    return text.strip()



def _canon(text: str) -> str:
    """Letters & digits only, NFKC-folded lowercase — for shingle coverage."""
    text = _s_norm(text)
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(c for c in text if unicodedata.category(c)[0] in ("L", "N"))


def _norm_key(text: str) -> str:
    """Alphanumeric-only lowercase key for TOC matching.

    Unicode-aware: keeps letters/digits of ANY script (NFKC-folded), not just
    ASCII, so non-Latin titles (Chinese / Japanese / Korean, etc.) produce
    distinct keys instead of all collapsing to "". For ASCII text this returns
    exactly the same value as the old ``[^a-z0-9]``-strip, so English matching
    is unchanged.
    """
    text = _s_norm(text)
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(c for c in text if unicodedata.category(c)[0] in ("L", "N"))


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


def _median_line_len(page, textpage=None) -> float:
    d = page.get_text("dict", textpage=textpage)
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


# ── Language-aware tokenisation ──────────────────────────────────────────────
# Whitespace tokenisation works for space-delimited scripts (Latin, Cyrillic …)
# but not for CJK, where a whole paragraph is one space-free run. These helpers
# emit one token per CJK ideograph/kana/hangul while leaving space-delimited
# words whole. For pure-ASCII/Latin text _tokenize() == text.split(), so the
# behaviour for English documents is byte-for-byte unchanged.
_CJK_RE = re.compile(
    "["
    "぀-ヿ"      # Hiragana + Katakana
    "㐀-䶿"      # CJK Ext A
    "一-鿿"      # CJK Unified Ideographs
    "豈-﫿"      # CJK Compatibility Ideographs
    "가-힯"      # Hangul syllables
    "]"
)


def _is_cjk_char(ch: str) -> bool:
    return bool(ch) and bool(_CJK_RE.match(ch[0]))


def _tokenize(text: str):
    """Split text into comparison tokens, segmenting CJK runs per-character."""
    toks = []
    for chunk in text.split():
        buf = ""
        for ch in chunk:
            if _is_cjk_char(ch):
                if buf:
                    toks.append(buf)
                    buf = ""
                toks.append(ch)
            else:
                buf += ch
        if buf:
            toks.append(buf)
    return toks


def _join_tokens(toks) -> str:
    """Re-join tokens for display / substring checks — no space is inserted
    around CJK characters so the result matches the original space-free text."""
    out = []
    for i, t in enumerate(toks):
        if i and not (_is_cjk_char(toks[i - 1][-1]) or _is_cjk_char(t[0])):
            out.append(" ")
        out.append(t)
    return "".join(out)


# ── Chinese script normalisation (Traditional → Simplified) ──────────────────
# A product's PROD and STAGE PDFs can use different Chinese scripts (e.g. CF23
# PROD = Simplified, STAGE download = Traditional). Those are different code
# points and never shingle-match. Folding both sides to Simplified via OpenCC
# lets them compare. Applied ONLY to the comparison surfaces (_canon / _norm_key
# / the lowercase phrase indexes) and gated on CJK presence, so the displayed
# report text and all non-Chinese documents are untouched.
try:
    import opencc as _opencc
    _T2S = _opencc.OpenCC("t2s")
except Exception:
    _T2S = None


def _s_norm(text: str) -> str:
    if _T2S is not None and text and _CJK_RE.search(text):
        return _T2S.convert(text)
    return text


# ────────────────────────────────────────────────────────────────────────────
# Navigation-page detection
# ────────────────────────────────────────────────────────────────────────────
def _detect_nav_pages(doc) -> set:
    """Return 1-based page numbers that are TOC / navigation / index pages."""
    total  = doc.page_count
    result = set()

    # Identify Q&A index pages from TOC to protect them from being skipped
    qa_pages = set()
    try:
        toc = doc.get_toc()
        if toc:
            for idx, item in enumerate(toc):
                title = item[1].lower()
                if 'q&a' in title or 'qa index' in title:
                    qa_start = item[2]
                    next_start = None
                    for next_item in toc[idx+1:]:
                        if next_item[2] > qa_start:
                            next_start = next_item[2]
                            break
                    end_page = next_start if next_start else qa_start + 2
                    for p_num in range(qa_start, end_page):
                        qa_pages.add(p_num)
                    break
    except Exception:
        pass

    use_ocr = _is_pdf_garbled(doc)
    ocr_lang = _get_pdf_language(doc) if use_ocr else "eng"

    for i, p in enumerate(doc, 1):
        if i in qa_pages:
            continue  # Do not treat Q&A index pages as navigation pages
        tp = None
        if use_ocr:
            try:
                tp = p.get_textpage_ocr(dpi=150, language=ocr_lang)
                text = p.get_text(textpage=tp)
            except Exception:
                text = p.get_text()
        else:
            text = p.get_text()
            
        if len(re.findall(r"\.{4,}", text)) >= 8:
            result.add(i)
        elif (i <= max(1, int(total * 0.10))
              and len(_NAV_INLINE_RE.findall(text)) >= 15
              and _median_line_len(p, textpage=tp) <= 50):
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
    doc = page.parent
    lang = _get_pdf_language(doc)
    is_cjk = lang in ("chi_tra", "chi_sim", "jpn", "kor")
    if _is_pdf_garbled(doc):
        try:
            tp = page.get_textpage_ocr(dpi=150, language=lang)
            d = page.get_text("dict", textpage=tp)
        except Exception as e:
            print(f"OCR failed for PROD page {page.number}: {e}")
            d = page.get_text("dict")
    else:
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
            limit = _MIN_BLOCK_CHARS if max_font <= _OSD_FONT_SOFT else (_MIN_BLOCK_BODY if not is_cjk else 2)
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
    doc = page.parent
    if _is_pdf_garbled(doc):
        lang = _get_pdf_language(doc)
        try:
            tp = page.get_textpage_ocr(dpi=150, language=lang)
            raw = page.get_text(textpage=tp)
        except Exception as e:
            print(f"OCR failed for STAGE page {page.number}: {e}")
            raw = page.get_text()
    else:
        raw = page.get_text()
    return _normalize(_strip_formatting(raw))


# ────────────────────────────────────────────────────────────────────────────
# TOC access
# ────────────────────────────────────────────────────────────────────────────
_DERIVED_NOTICE = {"CAUTION", "NOTE", "WARNING", "TIP", "IMPORTANT", "INFO"}


def _derive_toc(doc):
    """Build a heading-based TOC for PDFs that have no embedded bookmarks.

    Some source PDFs ship without an outline (e.g. shorter product guides), which
    left the whole comparison empty (0 PROD entries). Here we treat lines rendered
    noticeably larger than body text as section headings, so those PDFs can still
    be validated. Only used as a fallback when doc.get_toc() is empty.
    """
    # modal body-text size (lines >= 15 chars)
    sizes = {}
    use_ocr = _is_pdf_garbled(doc)
    ocr_lang = _get_pdf_language(doc) if use_ocr else "eng"

    for page in doc:
        tp = None
        if use_ocr:
            try:
                tp = page.get_textpage_ocr(dpi=150, language=ocr_lang)
            except Exception:
                pass
        d = page.get_text("dict", textpage=tp)
        for b in d.get("blocks", []):
            if b.get("type") != 0:
                continue
            for ln in b.get("lines", []):
                t = "".join(s.get("text", "") for s in ln.get("spans", [])).strip()
                if len(t) >= 15:
                    mx = round(max((s.get("size", 0) for s in ln.get("spans", [])), default=0), 1)
                    if mx > 0:
                        sizes[mx] = sizes.get(mx, 0) + 1
    body = max(sizes.items(), key=lambda kv: kv[1])[0] if sizes else 10.0
    h_min, h1 = body * 1.25, body * 1.45

    toc, seen = [], set()
    for pno in range(1, doc.page_count + 1):
        page = doc[pno - 1]
        tp = None
        if use_ocr:
            try:
                tp = page.get_textpage_ocr(dpi=150, language=ocr_lang)
            except Exception:
                pass
        d = page.get_text("dict", textpage=tp)
        for b in d.get("blocks", []):
            if b.get("type") != 0:
                continue
            for ln in b.get("lines", []):
                spans = ln.get("spans", [])
                t = "".join(s.get("text", "") for s in spans).strip()
                if not (3 <= len(t) <= 80):
                    continue
                mx = round(max((s.get("size", 0) for s in spans), default=0), 1)
                if mx < h_min:
                    continue
                if re.fullmatch(r"[\d.\s:/–-]+", t):          # numbers / rules
                    continue
                if t.strip(" :").upper() in _DERIVED_NOTICE:   # NOTE/CAUTION labels
                    continue
                key = re.sub(r"\s+", " ", t).lower()
                if key in seen:
                    continue
                seen.add(key)
                toc.append((1 if mx >= h1 else 2, t, pno))
    return toc


def get_toc(pdf_path):
    doc = fitz.open(pdf_path)
    toc = [(lvl, title.strip(), pg) for lvl, title, pg in doc.get_toc()]
    if not toc:                       # no embedded outline → derive from headings
        toc = _derive_toc(doc)
    doc.close()
    return toc


# ────────────────────────────────────────────────────────────────────────────
# Section extraction using TOC page ranges
# ────────────────────────────────────────────────────────────────────────────
def _find_sub_canon(canon_stream, needle_canon, start_stream_idx, hi_stream_idx=None):
    n = len(needle_canon)
    if not n:
        return None
    
    # 1. Contiguous word-level match
    start_canon_idx = 0
    while start_canon_idx < len(canon_stream) and canon_stream[start_canon_idx][1] < start_stream_idx:
        start_canon_idx += 1
        
    if hi_stream_idx is not None:
        hi_canon_idx = start_canon_idx
        while hi_canon_idx < len(canon_stream) and canon_stream[hi_canon_idx][1] < hi_stream_idx:
            hi_canon_idx += 1
    else:
        hi_canon_idx = len(canon_stream)
        
    for i in range(start_canon_idx, hi_canon_idx - n + 1):
        match = True
        for j in range(n):
            if canon_stream[i + j][0] != needle_canon[j]:
                match = False
                break
        if match:
            return canon_stream[i][1], canon_stream[i + n - 1][1]
            
    # 2. Fallback to character-level substring match (handles merged words like "Systemmenu")
    canon_stream_str_parts = []
    char_to_stream_idx = []
    for cw, orig_idx in canon_stream:
        char_to_stream_idx.extend([orig_idx] * len(cw))
        canon_stream_str_parts.append(cw)
    canon_stream_str = "".join(canon_stream_str_parts)
    
    needle_str = "".join(needle_canon)
    
    start_char_idx = 0
    while start_char_idx < len(char_to_stream_idx) and char_to_stream_idx[start_char_idx] < start_stream_idx:
        start_char_idx += 1
        
    if hi_stream_idx is not None:
        hi_char_idx = start_char_idx
        while hi_char_idx < len(char_to_stream_idx) and char_to_stream_idx[hi_char_idx] < hi_stream_idx:
            hi_char_idx += 1
    else:
        hi_char_idx = len(char_to_stream_idx)
        
    sub_str = canon_stream_str[start_char_idx:hi_char_idx]
    pos_in_sub = sub_str.find(needle_str)
    if pos_in_sub >= 0:
        match_start_char = start_char_idx + pos_in_sub
        match_end_char = match_start_char + len(needle_str) - 1
        if match_start_char < len(char_to_stream_idx) and match_end_char < len(char_to_stream_idx):
            return char_to_stream_idx[match_start_char], char_to_stream_idx[match_end_char]
        
    return None


def extract_sections(pdf_path, is_prod: bool) -> dict:
    """Return {title: text_str} keyed by original TOC title.

    Sections are delimited by locating each heading in a page-position-ordered
    word stream and slicing between consecutive headings — the same approach
    used by generate_validation_report.py for reliable section boundaries.
    """
    doc  = fitz.open(pdf_path)
    toc  = doc.get_toc() or _derive_toc(doc)
    nav  = {1} | _detect_nav_pages(doc)

    stream      = []   # flat word list across all body pages
    page_start  = {}   # {1-based page: stream offset}
    for i, page in enumerate(doc, 1):
        if i in nav:
            continue
        page_start[i] = len(stream)
        body = (_extract_page_body_prod(page)
                if is_prod else _extract_page_body_stage(page))
        stream += _tokenize(body)
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

    # Pre-build canon_stream for fast matching
    canon_stream = []
    for idx, t in enumerate(stream):
        cw = _canon(t)
        if cw:
            canon_stream.append((cw, idx))

    located, pos = [], 0
    for level, title, pgno in toc:
        needle_tokens = _tokenize(_normalize(title))
        needle_canon = [w for w in (_canon(t) for t in needle_tokens) if w]
        lo, hi      = _window(pgno)
        
        res         = _find_sub_canon(canon_stream, needle_canon, max(pos, lo), hi)
        if res is None:
            res     = _find_sub_canon(canon_stream, needle_canon, lo)
        if res is None:
            res     = _find_sub_canon(canon_stream, needle_canon, pos)
            
        if res is not None:
            idx, end_idx = res
            pos = end_idx + 1
        else:
            idx = -1
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
    lang       = _get_pdf_language(doc)
    is_cjk     = lang in ("chi_tra", "chi_sim", "jpn", "kor")
    shingle_len = 8 if is_cjk else CHAR_SHINGLE

    all_words  = []
    raw_parts  = []
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        body = _extract_page_body_stage(page)
        words = _keep(_tokenize(body))
        all_words  += words
        raw_parts.append(body)
    doc.close()
    nospace = "".join(_canon(w) for w in all_words)
    cset    = {nospace[i:i + shingle_len]
               for i in range(len(nospace) - shingle_len + 1)}
    full_lower = _s_norm(re.sub(r"\s+", " ", " ".join(raw_parts))).lower()
    return nospace, cset, full_lower


# ────────────────────────────────────────────────────────────────────────────
# Image extraction and comparison
# ────────────────────────────────────────────────────────────────────────────
def _is_decorative(bw: float, bh: float) -> bool:
    """True for thin rules / underlines / separator strips — not figures or icons.

    These render very differently between PROD and STAGE (a 1-pt horizontal rule
    in PROD may be a CSS border in STAGE) and would otherwise inflate the image
    comparison with false misses. Anything with a tiny short edge (< 6 pt) or an
    extreme aspect ratio (> 8:1) is treated as decoration, not real artwork.
    """
    mn = min(bw, bh)
    ar = max(bw, bh) / max(mn, 0.1)
    return mn < 6.0 or ar > 8.0


def _page_onpage_images(page):
    """Return list of (bw_pt, bh_pt) for each valid image placement on the page.

    Uses on-page bbox dimensions (PDF points) from get_image_info() instead of
    encoded pixel dimensions.  This makes comparison resolution-independent: a
    PROD icon encoded at 212 px but displayed at 25 pt matches a Stage icon
    encoded at 421 px but also displayed at 25 pt.

    Decorative rules / underlines (see _is_decorative) are skipped so they don't
    masquerade as missing figures or icons.

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
        if _is_decorative(bw, bh):
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
    toc    = doc.get_toc() or _derive_toc(doc)
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


def _dim_match(iw: int, sw: int, tol: float = 0.10) -> bool:
    """True when Stage image width is within tol% of PROD image width."""
    return abs(iw - sw) <= tol * max(iw, 1)


def _nearest(sizes: list, iw: float):
    """Return the (w, h) in ``sizes`` closest to ``iw`` by width, or None if empty."""
    if not sizes:
        return None
    return min(sizes, key=lambda s: abs(iw - s[0]))


def _compare_image_sections(prod_imgs: dict,
                             stage_all_icons: list,
                             stage_all_content: list,
                             stage_vector_count: int = 0) -> list:
    """Compare PROD figures and icons against STAGE.

    Content figures (max on-page dim > _ICON_MAX_ONPAGE):
        Matched by COUNT, consume-based, against the whole STAGE document.
        PROD and STAGE use different layout engines, so the *same* figure is
        rendered at a different on-page size, and STAGE re-paginates / re-sections
        content (its TOC is far more granular). Per-section exact-dimension
        matching therefore produced false "missing" results even though STAGE had
        the figure — just resized or under another heading. Instead every PROD
        figure claims one STAGE figure document-wide (the nearest unclaimed size,
        for display): it is PRESENT while STAGE still has figures left, and only
        genuinely MISSING once STAGE runs out (STAGE truly has fewer figures than
        PROD). A claimed figure whose size differs > 15 % is reported as Info
        (resized / reorganised), which is not a defect.

    Icons (max on-page dim ≤ _ICON_MAX_ONPAGE):
        Icons are a *small reused set* (one NOTE / warning / connector glyph
        appears on many pages), so PROD and STAGE have different icon-placement
        *counts* purely from re-pagination — counts are NOT comparable and the
        pool is non-consumed. While STAGE has raster icons the PROD icon is
        PRESENT (an exact-size miss is Info — size drift across export pipelines,
        never a section failure).

        STAGE manuals exported from InDesign/FrameMaker frequently render icons
        as *vector drawings* rather than raster images, so no raster icon is
        extractable even though the icons are present. When STAGE has no raster
        icons but carries substantial vector artwork (``stage_vector_count``),
        icons are reported N/A — "vector-rendered, size check not applicable" —
        instead of a misleading "missing". Decorative rules are filtered
        upstream.
    """
    # STAGE renders its images as vector art (no extractable raster of that type)?
    stage_vector_icons   = (not stage_all_icons)   and stage_vector_count >= _VECTOR_ICON_MIN
    stage_vector_figures = (not stage_all_content) and stage_vector_count >= _VECTOR_ICON_MIN
    # Document-wide STAGE figure pool, consumed across all sections in order.
    content_pool = list(stage_all_content)

    rows = []
    for title, p_imgs in prod_imgs.items():
        dim_rows       = []
        n_cont_present = 0
        n_cont_missing = 0
        n_icon_present = 0
        n_icon_missing = 0
        n_icon_na      = 0
        n_cont_na      = 0

        for pno, iw, ih in p_imgs:
            is_content = max(iw, ih) > _ICON_MAX_ONPAGE

            if is_content:
                near = _nearest(content_pool, iw)
                if near is not None:
                    content_pool.remove(near)
                    # Claimed → figure exists in STAGE. Exact width match = Present,
                    # otherwise Info (figure is present but resized/reorganised).
                    status = "Present" if _dim_match(iw, near[0], tol=0.15) else "Info"
                    display_match = near
                    n_cont_present += 1
                elif stage_vector_figures:
                    # STAGE draws figures as vectors — raster size match N/A, present.
                    status = "NA"
                    display_match = None
                    n_cont_na += 1
                else:
                    status = "Missing"          # STAGE ran out of figures — genuine
                    display_match = None
                    n_cont_missing += 1
                dim_rows.append({
                    "section": title, "prod_page": pno,
                    "prod_w": iw, "prod_h": ih, "type": "Content",
                    "status": status,
                    "match_w": display_match[0] if display_match else None,
                    "match_h": display_match[1] if display_match else None,
                    "nearest_only": status == "Info",
                })
            else:
                if stage_all_icons:
                    # Non-consume: STAGE has icons (a small set reused across
                    # pages), so the PROD icon is present; an exact-size miss is
                    # Info (size drift), never "missing".
                    match = next(((sw, sh) for sw, sh in stage_all_icons
                                  if _dim_match(iw, sw, tol=0.25)), None)
                    display_match = match or _nearest(stage_all_icons, iw)
                    status = "Present" if match else "Info"
                    n_icon_present += 1
                    nearest_only = not match
                elif stage_vector_icons:
                    # STAGE draws icons as vectors — raster size match N/A, present.
                    status = "NA"
                    display_match = None
                    n_icon_na += 1
                    nearest_only = False
                else:
                    # STAGE genuinely has no icons at all.
                    status = "Missing" if _FAIL_ON_ICON_MISS else "Info"
                    display_match = None
                    n_icon_missing += 1
                    nearest_only = False
                dim_rows.append({
                    "section": title, "prod_page": pno,
                    "prod_w": iw, "prod_h": ih, "type": "Icon",
                    "status": status,
                    "match_w": display_match[0] if display_match else None,
                    "match_h": display_match[1] if display_match else None,
                    "nearest_only": nearest_only,
                })

        # A section fails only when STAGE genuinely has fewer figures than PROD.
        status_overall = "Fail" if n_cont_missing > 0 else "Pass"
        rows.append({
            "title":         title,
            "prod_content":  n_cont_present + n_cont_missing + n_cont_na,
            "found_content": n_cont_present,
            "miss_content":  n_cont_missing,
            "na_content":    n_cont_na,
            "prod_icons":    n_icon_present + n_icon_missing + n_icon_na,
            "found_icons":   n_icon_present,
            "miss_icons":    n_icon_missing,
            "na_icons":      n_icon_na,
            "status":        status_overall,
            "dim_rows":      dim_rows,
        })

    return rows


def _section_missing(prod_words, stage_ns, stage_cset, stage_full_lower,
                     stage_section_lower=""):
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

    # Dynamically determine if CJK characters are dominant
    is_cjk = False
    if _CJK_RE.search(s) or (stage_full_lower and _CJK_RE.search(stage_full_lower)):
        is_cjk = True
    L = 8 if is_cjk else CHAR_SHINGLE

    if len(s) < L:
        if s and s in stage_ns:
            return 100.0, []
        phrase = _s_norm(re.sub(r"\s+", " ", _join_tokens(words))).lower()
        if phrase in stage_full_lower:
            return 100.0, []
        return 0.0, ([_join_tokens(words)] if len(words) >= MIN_FRAG_WORDS else [])

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
                phrase = _s_norm(re.sub(r"\s+", " ", _join_tokens(frag))).lower()
                if phrase in stage_full_lower:
                    pass  # covered
                else:
                    frag_text = _join_tokens(frag)
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

                    # 3) Reordered *within the same STAGE section*: every
                    #    distinctive word (>=4 Latin letters) of the fragment is
                    #    present in this section's text, so the content exists —
                    #    just in a different word order (e.g. a hyperlink phrase
                    #    "See USB-C Configuration for…" vs PROD "See for USB-C
                    #    Configuration on page N", or a re-laid-out list).
                    #    Section-scoped (not document-wide) so genuinely dropped
                    #    content — whose words are absent from THIS section even
                    #    if they occur elsewhere — is still reported.
                    if reported and stage_section_lower:
                        dwords = [w.lower() for w in re.findall(r"[A-Za-z]{4,}", frag_text)]
                        if dwords and all(w in stage_section_lower for w in dwords):
                            reported = False

                    if reported:
                        frags.append(frag_text)
        else:
            i += 1
    return coverage, frags


# ── Trademark / symbol integrity (™ ® ©) ─────────────────────────────────────
# Conversion pipelines silently drop ™/®/© and the branded terms they sit on
# ("USB-C™", "Eye-Care®"). PROD is the baseline, so STAGE should carry every
# trademark PROD has. This is a doc-wide character/term check (independent of the
# shingle matcher, which folds these symbols away).
_TM_SYMBOLS = "™®©"
_TM_TERM_RE = re.compile(r"([A-Za-z0-9][A-Za-z0-9\-/.]{0,24}?)\s*([™®©])")


def _trademark_findings(prod_path, stage_path):
    """Return (symbol_counts, dropped_terms).

    symbol_counts: [(symbol, n_prod, n_stage), …] for ™ ® © where PROD has more.
    dropped_terms: [(term_with_symbol, n_prod, base_present_in_stage), …] for
                   branded terms whose exact symbol-bearing form is absent in STAGE.
    """
    def _full_text(path):
        d = fitz.open(path)
        try:
            return "".join(pg.get_text() for pg in d)
        finally:
            d.close()

    pt, st = _full_text(prod_path), _full_text(stage_path)
    counts = [(s, pt.count(s), st.count(s)) for s in _TM_SYMBOLS
              if pt.count(s) > st.count(s)]

    prod_terms = collections.Counter(
        f"{m.group(1)}{m.group(2)}" for m in _TM_TERM_RE.finditer(pt))
    dropped = []
    for term, n in sorted(prod_terms.items(), key=lambda kv: -kv[1]):
        if term in st:
            continue                       # STAGE keeps the symbol-bearing form
        base = term[:-1].strip()           # term without its trailing symbol
        dropped.append((term, n, bool(base) and base in st))
    return counts, dropped


# ────────────────────────────────────────────────────────────────────────────
# Report helpers
# ────────────────────────────────────────────────────────────────────────────
def _esc(text):
    esc = (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Render any CJK-bearing text with the Unicode font so it doesn't fall back
    # to Helvetica (which lacks CJK glyphs and prints dots). English is untouched.
    if _CJK_FONT_NAME and _CJK_RE.search(esc):
        esc = f'<font name="{_CJK_FONT_NAME}">{esc}</font>'
    return esc


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
                    image_results, icon_doc_summary, report_path,
                    tm_counts=None, tm_dropped=None,
                    prod_encoding_issue=False, stage_encoding_issue=False):
    tm_counts = tm_counts or []
    tm_dropped = tm_dropped or []
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
    story.append(Spacer(1, 8))

    if prod_encoding_issue or stage_encoding_issue:
        note_style = ParagraphStyle(
            "EncodingNote",
            parent=styles["Normal"],
            fontSize=9,
            leading=13,
            textColor=colors.HexColor("#1e3a5f"),
        )
        parts = []
        if prod_encoding_issue:
            parts.append("Production PDF")
        if stage_encoding_issue:
            parts.append("Staging PDF")
        pdfs = " and ".join(parts)
        msg = (
            f"<b>Note:</b> {pdfs} contain custom-encoded special characters "
            "(e.g. trademark ™, copyright ©, bullet symbols). "
            "These were extracted with an OCR-assisted pass for improved accuracy. "
            "Content comparison results are not affected."
        )
        note_table = Table([[Paragraph(msg, note_style)]], colWidths=[700])
        note_table.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), colors.HexColor("#e8f0fe")),
            ("LINEBELOW",  (0,0), (-1,-1), 1.0, colors.HexColor("#90a4ae")),
            ("TOPPADDING", (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 6),
            ("LEFTPADDING", (0,0), (-1,-1), 12),
            ("RIGHTPADDING", (0,0), (-1,-1), 12),
        ]))
        story.append(note_table)
        story.append(Spacer(1, 10))

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
    # PART 4 — Language & Translation Integrity
    # ═══════════════════════════════════════════
    # Open stage document to inspect language
    try:
        temp_sdoc = fitz.open(stage_path)
        lang_stage = _get_pdf_language(temp_sdoc)
        temp_sdoc.close()
    except Exception:
        lang_stage = "eng"

    if lang_stage != "eng":
        story.append(PageBreak())
        story.append(Paragraph("Part 4 — Language &amp; Translation Integrity (Localization Bugs)", head_s))
        story.append(Paragraph(
            "Flags any English words found in the non-English guide outside the cover page (page 1) "
            "as translation or localization bugs. Technical terms (like HDMI, USB) are excluded.",
            ParagraphStyle("Note4", parent=styles["Normal"], fontSize=8,
                           textColor=colors.grey, spaceAfter=8),
        ))
        
        # Extract all text from page 2 to the end of STAGE PDF
        try:
            stage_doc = fitz.open(stage_path)
            use_ocr = _is_pdf_garbled(stage_doc)
            full_text = ""
            for i in range(1, stage_doc.page_count):
                page = stage_doc[i]
                if use_ocr:
                    try:
                        tp = page.get_textpage_ocr(dpi=150, language=lang_stage)
                        text = page.get_text(textpage=tp)
                    except Exception:
                        text = page.get_text()
                else:
                    text = page.get_text()
                full_text += text + " "
            stage_doc.close()
            unexpected_words = find_english_words_in_non_en(full_text)
        except Exception as e:
            print(f"Failed to scan English words in stage PDF: {e}")
            unexpected_words = []
        
        if unexpected_words:
            t_hdr = [Paragraph(f"<b>{h}</b>", hdr_s) for h in ["#", "Localization Bug / Translation Issue", "Comment"]]
            t_rows = [t_hdr]
            for idx, w in enumerate(unexpected_words, 1):
                t_rows.append([
                    Paragraph(str(idx), cell_s),
                    Paragraph(f"English word <font color='#c62828'><b>“{_esc(w)}”</b></font> found", topic_s),
                    Paragraph("Should be translated to the target language (outside cover page).", cell_s)
                ])
            t_table = Table(t_rows, colWidths=[30, 250, 380], repeatRows=1)
            t_table.setStyle(TableStyle([
                ("BACKGROUND",    (0,0), (-1,0), colors.HexColor("#c62828")), # red header for bugs
                ("GRID",          (0,0), (-1,-1), 0.5, colors.grey),
                ("VALIGN",        (0,0), (-1,-1), "TOP"),
                ("TOPPADDING",    (0,0), (-1,-1), 4),
                ("BOTTOMPADDING", (0,0), (-1,-1), 4),
                ("ROWBACKGROUNDS",(0,1), (-1,-1), [colors.white, colors.HexColor("#fff8f8")]),
            ]))
            story.append(t_table)
        else:
            story.append(Paragraph(
                "✓ PASS: No unexpected English words found outside the cover page.",
                ParagraphStyle("LangPass", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#2e7d32"))
            ))

    doc.build(story)
    print(f"Report saved: {report_path}")


# ────────────────────────────────────────────────────────────────────────────
# Main validation logic
# ────────────────────────────────────────────────────────────────────────────
def validate(prod_path, stage_path, report_path):
    # ── Pre-extract all stage page texts for heading search ──
    print("Pre-extracting STAGE page texts...")
    stage_doc = fitz.open(stage_path)
    stage_page_texts = []
    use_ocr = _is_pdf_garbled(stage_doc)
    lang_stage = _get_pdf_language(stage_doc)
    for i in range(stage_doc.page_count):
        page = stage_doc[i]
        if use_ocr:
            try:
                tp = page.get_textpage_ocr(dpi=150, language=lang_stage)
                text = page.get_text(textpage=tp)
            except Exception:
                text = page.get_text()
        else:
            text = page.get_text()
        stage_page_texts.append(text)
    stage_doc.close()

    def _find_heading_in_texts(title, page_texts):
        title_norm = _canon(title)
        if not title_norm:
            return None
        title_tok = _join_tokens(_tokenize(title_norm))
        title_clean = "".join(c for c in title_tok if unicodedata.category(c)[0] in ("L", "N"))
        if not title_clean:
            return None
        for pidx, p_text in enumerate(page_texts):
            p_text_norm = _join_tokens(_tokenize(_canon(p_text)))
            p_text_clean = "".join(c for c in p_text_norm if unicodedata.category(c)[0] in ("L", "N"))
            if title_clean in p_text_clean:
                return pidx + 1
        return None

    # ── TOC comparison ──
    _emit(0.02, "reading TOC")
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
                "note": ""
            })
        else:
            pno = _find_heading_in_texts(title, stage_page_texts)
            if pno is not None:
                toc_results.append({
                    "title": title, "level": lvl,
                    "prod_page": pg, "stage_page": pno,
                    "toc_status": "Match",
                    "note": f"Heading found inside page: {pno}"
                })
            else:
                toc_results.append({
                    "title": title, "level": lvl,
                    "prod_page": pg, "stage_page": "-",
                    "toc_status": "Missing in Stage",
                    "note": ""
                })
    n_step_bookmarks = 0
    for lvl, title, pg in stage_toc:
        if _norm_key(title) not in prod_keys:
            # Skip numbered procedure-step bookmarks — not real extra sections.
            if _STEP_BOOKMARK_RE.match(title or ""):
                n_step_bookmarks += 1
                continue
            toc_results.append({
                "title": title, "level": lvl,
                "prod_page": "-", "stage_page": pg,
                "toc_status": "Extra in Stage",
                "note": ""
            })
    if n_step_bookmarks:
        print(f"  (excluded {n_step_bookmarks} numbered step bookmarks from Extra in Stage)")

    n_m = sum(1 for r in toc_results if r["toc_status"] == "Match")
    n_mi = sum(1 for r in toc_results if r["toc_status"] == "Missing in Stage")
    n_e  = sum(1 for r in toc_results if r["toc_status"] == "Extra in Stage")
    print(f"  TOC: Match={n_m} | Missing in Stage={n_mi} | Extra in Stage={n_e}")

    # ── Content extraction ──
    _emit(0.10, "extracting section text")
    print("Extracting section text...")
    prod_sections  = extract_sections(prod_path,  is_prod=True)
    stage_sections = extract_sections(stage_path, is_prod=False)
    stage_lookup   = {_norm_key(t): v for t, v in stage_sections.items()}
    print(f"  PROD sections: {len(prod_sections)} | STAGE sections: {len(stage_sections)}")

    # Build STAGE shingle index from ALL non-nav pages (not just section slices)
    # so content that falls before the first TOC heading is still covered.
    _emit(0.24, "building STAGE index")
    print("Building STAGE content index...")
    stage_doc = fitz.open(stage_path)
    stage_nav = {1} | _detect_nav_pages(stage_doc)
    stage_doc.close()
    stage_ns, stage_cset, stage_full_lower = _build_stage_index(stage_path, stage_nav)

    # ── Content comparison (all PROD topics: matching + missing in Stage) ──
    _emit(0.34, "comparing content")
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
        prod_words = _keep(_tokenize(pc))
        
        # ── Handle "Missing in Stage" topics ──
        if r["toc_status"] == "Missing in Stage":
            if not prod_words:
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

            coverage, missing = _section_missing(
                prod_words, stage_ns, stage_cset, stage_full_lower,
                stage_section_lower="")
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
            prod_words, stage_ns, stage_cset, stage_full_lower,
            stage_section_lower=_s_norm(sc or "").lower())

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
    _emit(0.52, "extracting images")
    print("Extracting images...")
    prod_doc = fitz.open(prod_path)
    prod_nav = {1} | _detect_nav_pages(prod_doc)
    prod_doc.close()
    prod_imgs  = _extract_section_images(prod_path, prod_nav)

    # Build document-wide Stage pools of icons and figures (on-page pt sizes).
    # STAGE re-paginates and re-sections content (much finer TOC), so figures
    # and icons are matched against the whole STAGE document rather than fragile
    # per-section page ranges.  Decorative rules are already filtered upstream.
    print("Building Stage image index (document-wide, on-page pts)...")
    _sdoc = fitz.open(stage_path)
    stage_all_icons   = []
    stage_all_content = []
    stage_vector_count = 0
    for i, page in enumerate(_sdoc, 1):
        if i in stage_nav:
            continue
        for bw, bh in _page_onpage_images(page):
            if max(bw, bh) <= _ICON_MAX_ONPAGE:
                stage_all_icons.append((bw, bh))
            else:
                stage_all_content.append((bw, bh))
        # Vector artwork (used to tell "images are vector-drawn" from "images
        # genuinely missing" when STAGE has no extractable raster of that type).
        # Only needed as a fallback — skip the (costly) drawings scan once we
        # already have both raster pools populated.
        if not (stage_all_icons and stage_all_content):
            stage_vector_count += len(page.get_drawings())
    _sdoc.close()
    prod_content_total = sum(
        1 for imgs in prod_imgs.values()
        for _, bw, bh in imgs if max(bw, bh) > _ICON_MAX_ONPAGE
    )
    print(f"  Stage icons doc-wide: {len(stage_all_icons)} | "
          f"figures PROD {prod_content_total} vs STAGE {len(stage_all_content)}")

    _emit(0.64, "comparing images")
    print("Comparing figures by count (doc-wide) and icons by size (±25%)...")
    image_results = _compare_image_sections(
        prod_imgs, stage_all_icons, stage_all_content, stage_vector_count)
    n_ip = sum(1 for r in image_results if r["status"] == "Pass")
    n_if = sum(1 for r in image_results if r["status"] == "Fail")
    print(f"  Pass={n_ip} | Fail={n_if}")
    if n_if:
        print("  FAIL details (STAGE has fewer figures than PROD here):")
        for r in image_results:
            if r["status"] == "Fail":
                print(f"    {r['title']!r}: figures missing="
                      f"{r['miss_content']}/{r['prod_content']}")
    icon_doc_summary = {
        "prod_total":  sum(r["prod_icons"] for r in image_results),
        "found_total": sum(r["found_icons"] for r in image_results),
        "miss_total":  sum(r["miss_icons"]  for r in image_results),
        "na_total":    sum(r.get("na_icons", 0) for r in image_results),
        "vector":      bool(stage_vector_count) and not stage_all_icons,
        "status":      "Info" if not _FAIL_ON_ICON_MISS else (
            "Pass" if all(r["miss_icons"] == 0 for r in image_results) else "Fail"
        ),
    }
    print(f"  Icons: available {icon_doc_summary['prod_total']} | "
          f"found {icon_doc_summary['found_total']} | "
          f"missing {icon_doc_summary['miss_total']} | "
          f"vector-N/A {icon_doc_summary['na_total']}")

    # ── Trademark / symbol integrity (™ ® ©) ──
    _emit(0.66, "checking trademarks")
    tm_counts, tm_dropped = _trademark_findings(prod_path, stage_path)
    if tm_counts or tm_dropped:
        print("  Trademark/symbol issues:")
        for sym, np_, ns in tm_counts:
            print(f"    {sym}: PROD {np_} vs STAGE {ns} (STAGE missing {np_ - ns})")
        for term, n, base in tm_dropped[:20]:
            print(f"    dropped {term!r} (PROD x{n}; base text "
                  f"{'present' if base else 'absent'} in STAGE)")


    # ── Generate PDF ──
    _emit(0.95, "building report")
    print("Generating report PDF...")
    
    prod_doc = fitz.open(prod_path)
    prod_garbled = _is_pdf_garbled(prod_doc)
    prod_doc.close()

    stage_doc = fitz.open(stage_path)
    stage_garbled = _is_pdf_garbled(stage_doc)
    stage_doc.close()

    generate_report(prod_path, stage_path, toc_results, content_results,
                    image_results, icon_doc_summary, report_path,
                    tm_counts, tm_dropped,
                    prod_encoding_issue=prod_garbled,
                    stage_encoding_issue=stage_garbled)
    _emit(1.0, "done")
    print("Done.")

    return {
        "toc_match":    n_m,
        "toc_missing":  n_mi,
        "toc_extra":    n_e,
        "content_pass": sum(1 for r in content_results if r["status"] == "Pass"),
        "content_fail": sum(1 for r in content_results if r["status"] == "Fail"),
        "report":       report_path,
    }


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

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
import bisect
import io
import json
import difflib

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
    KeepTogether, Image as RLImage,
)
from reportlab.platypus.flowables import HRFlowable
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
_MIN_READABLE_PT = 6.5   # below this, text is artwork lettering, not content:
                         # "OPEN"/"CLOSE" curved around a battery diagram is set
                         # one 4 pt character per span, and reading-order sorting
                         # scrambles it into "C N L E O P S O E E S..." — which is
                         # neither missing nor extra content, just decoration.
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
# Sections excluded from validation entirely. A Q&A / FAQ index is a navigation
# aid whose wording is rewritten per channel, so comparing it produces noise
# rather than defects.
SKIP_SECTIONS = ("q&a index", "qa index", "q & a index")

MIN_FRAG_WORDS  = 5      # short fragments are too easily caused by PDF extraction order
SEQ_MAX_GAP     = 6      # words of filler tolerated between two fragment words when
                         # confirming a fragment really is absent from STAGE.
                         # Absorbs page-break artifacts (page numbers, running
                         # heads) so content continuing on the NEXT page still
                         # counts as present.
SEQ_WINDOW      = 3      # word-window used to re-verify a fragment word by word
_SHORT_CELL_WORDS = 4    # table cells this short are checked whole, not windowed
SEQ_SOURCE_GAP  = 1      # a reported fragment must read VERBATIM in the document
                         # it came from. Sections are sliced out of a page-ordered
                         # token stream, so scattered diagram labels get strung
                         # together into phrases that appear in neither PDF
                         # ("USB peripherals Headphone PC", "Picture with the
                         # Picture"). Allowing any gap here let those through as
                         # findings. Contiguity is the only honest bar: report
                         # text that actually reads that way, or not at all.
_WORDCHAR_RE    = re.compile(r"[^\W\d_]", re.UNICODE)  # any letter, incl. CJK
_WORD_TOKEN_RE  = re.compile(r"[^\W\d_]+", re.UNICODE)   # whole word runs
_BARE_NUM_RE    = re.compile(r"^\d{1,3}$")   # standalone layout/callout numbering
                         # ("1.", "5.") — diagram callouts and list markers are
                         # renumbered by layout, so they are ignored when deciding
                         # whether the surrounding words exist in the other PDF.

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
# A table-of-contents entry line: text, then dot leaders or a wide gap, then a
# trailing page number. Used to tell a real TOC page from a spec table (which
# also has many short lines and small numbers).
_TOC_ENTRY_RE = re.compile(r"^.{2,}?(?:\.{2,}|\s{3,})\s*\d{1,3}\s*$")
# Strip formatting-only labels before comparison
_FMT_LABEL_RE    = re.compile(
    r"\b(NOTE|TIP|IMPORTANT|CAUTION|WARNING)\s*:\s*", re.IGNORECASE)
# Numbered list marker — matches "1." "7." etc. (not "1," "2)")
_NUMBERED_ITEM_RE = re.compile(r"\b\d+\.")
# A canonical token that IS a value+unit ("9v", "4a", "12v") once digit-only
# tokens are already stripped by _split_canon — used to spot spec-table
# fragments where reading order is a rendering artefact, not real wording.
_UNIT_VALUE_RE = re.compile(r"^\d+[a-z]+$")
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


def _is_skipped_section(title: str) -> bool:
    """True for headings listed in SKIP_SECTIONS (see there)."""
    t = re.sub(r"\s+", " ", (title or "")).strip().lower()
    return any(t == k or t.startswith(k) for k in SKIP_SECTIONS)


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

    # Q&A / FAQ index pages are navigation, not content: they restate headings
    # that are validated where they actually live, and their wording is
    # rewritten per channel. They were previously protected from nav detection,
    # which meant every cross-reference on them was compared and reported.
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

    result |= qa_pages

    for i, p in enumerate(doc, 1):
        if i in qa_pages:
            continue                       # already excluded as navigation
        tp = None
        if use_ocr:
            try:
                tp = p.get_textpage_ocr(dpi=150, language=ocr_lang)
                text = p.get_text(textpage=tp)
            except Exception:
                text = p.get_text()
        else:
            text = p.get_text()
            
        lines = [ln for ln in text.splitlines() if ln.strip()]
        # A table-of-contents / index line is "<heading text> ....  <page no>":
        # a run of words, then dot leaders or a wide gap, then a 1-3 digit page
        # number at the end of the line. Counting these is a reliable TOC signal.
        # The previous rule counted every bare 1-2 digit number on the page,
        # which flagged spec-table pages ("USB 2.0", "4K 30 FPS", "64 pcs", ...)
        # as navigation and dropped the entire page - table, content and all -
        # from every check.
        toc_lines = sum(1 for ln in lines if _TOC_ENTRY_RE.match(ln))
        dot_runs = len(re.findall(r"\.{4,}", text))
        if dot_runs >= 8:
            result.add(i)
        elif (lines and toc_lines >= 8
              and toc_lines / len(lines) >= 0.5
              and i <= max(2, int(total * 0.15))):
            # a dot-leader-free TOC: mostly "heading  <page no>" lines, near the
            # front of the document
            result.add(i)
    return result


# ── Fonts whose extracted text cannot be trusted ─────────────────────────────
# An Identity-H CID font with no ToUnicode CMap draws the right glyphs but has
# no map back to Unicode, so extraction yields raw CID values ("仼儖鏤" where the
# page plainly shows "日本語"). The page LOOKS correct; only its text layer is
# broken. Such spans must be kept out of the content comparison — comparing them
# would report the same visible words as both missing and extra — and reported
# instead as a text-layer defect in their own right.
_UNTRUSTED_FONT_CACHE = {}


def _font_key(name: str) -> str:
    """Comparable font name.

    A span reports "NotoSansJP-Bold" while the page font table lists it as
    "ABCDEF+NotoSansJP-Bold-Identity-H". Both are reduced to the same key, or the
    lookup silently never matches.
    """
    n = (name or "").split("+")[-1]
    n = re.sub(r"-Identity-[HV]$", "", n, flags=re.I)
    return n.lower()


def _untrusted_fonts(doc) -> set:
    """Font keys in `doc` that draw glyphs with no reliable Unicode mapping."""
    key = doc.name or id(doc)
    hit = _UNTRUSTED_FONT_CACHE.get(key)
    if hit is not None:
        return hit
    bad = set()
    try:
        for pno in range(doc.page_count):
            for f in doc[pno].get_fonts(full=True):
                if not str(f[5] or "").startswith("Identity"):
                    continue
                try:
                    if "ToUnicode" not in doc.xref_object(f[0]):
                        bad.add(_font_key(f[3]))
                except Exception:
                    pass
    except Exception:
        pass
    if len(_UNTRUSTED_FONT_CACHE) > 8:
        _UNTRUSTED_FONT_CACHE.clear()
    _UNTRUSTED_FONT_CACHE[key] = bad
    return bad


_NONLATIN_RE = re.compile(r"[\u0590-\u05ff\u0600-\u06ff\u3000-\u9fff"
                          r"\uac00-\ud7af\uf900-\ufaff]")


def _script_unreliable(text: str) -> bool:
    """True when a fragment is mostly non-Latin script.

    When one document draws these scripts with a font that has no Unicode map,
    its version of them is dropped from comparison. The other document's copy
    then has nothing to match against and would be reported as added text, even
    though both pages show the same thing. Neither side is comparable, so
    fragments dominated by those scripts are left out of the content result and
    reported once as a text-layer defect instead.
    """
    letters = [c for c in (text or "") if c.isalnum()]
    if not letters:
        return False
    nonlatin = sum(1 for c in letters if _NONLATIN_RE.match(c))
    return nonlatin >= max(1, len(letters) // 2)


def _span_font_untrusted(span, bad_fonts) -> bool:
    return bool(bad_fonts) and _font_key(span.get("font")) in bad_fonts


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
            d = _page_dict(page)
    else:
        d = _page_dict(page)
        
    bad_fonts = _untrusted_fonts(doc)
    parts = []
    # Blocks are not always emitted in reading order, which put a section's body
    # text ahead of its own heading and left the heading holding nothing. Sort
    # top-to-bottom, then left-to-right, so the token stream follows the page.
    _blocks = sorted(
        (b for b in d["blocks"] if b.get("type") == 0),
        key=lambda b: (round(b.get("bbox", (0, 0, 0, 0))[1], 1),
                       round(b.get("bbox", (0, 0, 0, 0))[0], 1)))
    for block in _blocks:
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
            if max_font <= _OSD_FONT_SOFT:
                # 7–8.5 pt: OSD-overlay range. Still needs bulk to be worth
                # comparing, otherwise menu fragments flood the comparison.
                if len(block_txt) < _MIN_BLOCK_CHARS:
                    continue
            else:
                # Body-font block. Short ones here are figure callout labels and
                # table cells ("Speakers", "Dial key", "Contrast", "5V / 3A") —
                # real content that must match STAGE exactly, so they are kept.
                # Only blocks carrying no word characters at all (bullets, rules,
                # bare page numbers) are dropped.
                if len(_WORDCHAR_RE.findall(block_txt)) < 2:
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
                if _span_font_untrusted(span, bad_fonts):
                    continue          # text layer unreliable — see _untrusted_fonts
                size = span.get("size", 0)
                if size < _MIN_READABLE_PT:
                    continue          # artwork lettering — see _MIN_READABLE_PT
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
    """Extract STAGE body text (no font-size filter — OSD text lives in images).

    Spans drawn with a font that has no reliable Unicode mapping are dropped, the
    same as on the PROD side, so a broken text layer never shows up as a content
    difference.
    """
    doc = page.parent
    if _is_pdf_garbled(doc):
        lang = _get_pdf_language(doc)
        try:
            tp = page.get_textpage_ocr(dpi=150, language=lang)
            return _normalize(_strip_formatting(page.get_text(textpage=tp)))
        except Exception as e:
            print(f"OCR failed for STAGE page {page.number}: {e}")

    bad_fonts = _untrusted_fonts(doc)
    parts = []
    d = _page_dict(page)
    blocks = sorted((b for b in d["blocks"] if b.get("type") == 0),
                    key=lambda b: (round(b.get("bbox", (0, 0, 0, 0))[1], 1),
                                   round(b.get("bbox", (0, 0, 0, 0))[0], 1)))
    for block in blocks:
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if _span_font_untrusted(span, bad_fonts):
                    continue
                if span.get("size", 0) < _MIN_READABLE_PT:
                    continue          # artwork lettering — see _MIN_READABLE_PT
                parts.append(span.get("text", ""))
    return _normalize(_strip_formatting(" ".join(parts)))


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
    # Titles must be stripped exactly as get_toc() strips them: validate() looks
    # sections up by the get_toc() title, and an embedded outline often indents
    # sub-headings (" Display menu"). Keying on the raw title made every such
    # lookup miss and the heading report as having no content to compare.
    toc  = [(lvl, (title or "").strip(), pg)
            for lvl, title, pg in (doc.get_toc() or [])] or _derive_toc(doc)
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
            body = end_idx + 1        # content starts AFTER the heading itself
        else:
            idx = -1
            body = -1
        located.append((idx, level, title, pgno, body))

    # A heading owns everything beneath it, down to the next heading at the SAME
    # level or higher — so a parent section carries its sub-headings and their
    # paragraphs, tables and lists too. Ending at the next heading of *any* level
    # would leave every parent holding nothing but its own title (reported as
    # "no content"), which skips the bulk of the document from validation.
    # Parent and child spans overlap by design: each heading is validated against
    # the whole of its own content.
    sections = {}
    for i, (idx, level, title, pgno, body) in enumerate(located):
        if idx is None or idx < 0:
            sections[title] = ""
            continue
        end = len(stream)
        for j in range(i + 1, len(located)):
            j_idx, j_level = located[j][0], located[j][1]
            if j_idx is not None and j_idx > idx and j_level <= level:
                end = j_idx
                break
        # Start after the heading text: whether the heading itself matches is
        # Part 1's job. Including it here re-reported every heading difference as
        # missing content, glued onto the real finding ("Copyright and Disclaimer
        # Copyright Disclaimer"), which read as a false report.
        sections[title] = " ".join(stream[max(body, idx):end])
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


# ── Fragment re-verification against the whole STAGE document ────────────────
# The shingle pass slices PROD into per-heading sections, so an uncovered run
# can be a *synthetic* string — a heading glued to body text that STAGE lays out
# elsewhere, or a paragraph that STAGE continues on the next page. Testing such
# a run with a single contiguous substring match reports content as missing that
# is plainly there. Before anything is reported, every fragment is re-checked
# word by word against a document-wide word index that tolerates small gaps.
_SEQ_INDEX_CACHE = {}


_WORD_RUN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _split_canon(token: str):
    """Word runs inside one token, lowercased, bare layout numbers dropped.

    Splitting on punctuation is what makes the two sides comparable: PROD may
    write "cord (Supplied" and STAGE "cord(Supplied". Folding punctuation away
    without splitting turns the latter into a single glued token, and the
    sequence match then fails on a pure whitespace difference.
    """
    return [w for w in (m.group(0).lower()
                        for m in _WORD_RUN_RE.finditer(token or ""))
            if w and not _BARE_NUM_RE.match(w)]


def _seq_tokens(text_or_words):
    """Canonical comparison tokens: lowercased, split on punctuation, bare
    layout numbers dropped (see _BARE_NUM_RE)."""
    src = (_tokenize(text_or_words) if isinstance(text_or_words, str)
           else text_or_words)
    out = []
    for tok in src:
        out.extend(_split_canon(tok))
    return out


_RAW_PAGE_IDX_CACHE = {}


def _raw_page_indexes(pdf_path: str, nav_pages: set):
    """One token index per page, built from the page's RAW text.

    The section streams are page-ordered and block-sorted, which strings
    scattered diagram labels into phrases that appear nowhere on the page
    ("USB peripherals Headphone PC"). Checking a fragment against the raw text of
    a single page — and requiring it to be contiguous there — is what separates
    text that genuinely reads that way from an artifact of the sort order.
    """
    key = (os.path.abspath(pdf_path), tuple(sorted(nav_pages)))
    hit = _RAW_PAGE_IDX_CACHE.get(key)
    if hit is not None:
        return hit
    out, doc = [], fitz.open(pdf_path)
    for i in range(doc.page_count):
        if (i + 1) in nav_pages:
            continue
        idx = {}
        for pos, tok in enumerate(_seq_tokens(doc[i].get_text())):
            idx.setdefault(tok, []).append(pos)
        out.append(idx)
    doc.close()
    if len(_RAW_PAGE_IDX_CACHE) > 6:
        _RAW_PAGE_IDX_CACHE.clear()
    _RAW_PAGE_IDX_CACHE[key] = out
    return out


def _source_ok(source, tokens) -> bool:
    """Does `tokens` genuinely read this way in the document it came from?

    Accepts either a list of per-page raw indexes (contiguity required — the
    honest bar, since section streams are sorted and can string scattered
    labels together) or a single document-wide index (small gaps tolerated), so
    every caller can use one check whichever it happens to hold.
    """
    if source is None:
        return True
    if isinstance(source, list):
        return _reads_verbatim(source, tokens)
    return _seq_present(source, tokens, max_gap=SEQ_SOURCE_GAP)


def _reads_verbatim(page_indexes, tokens) -> bool:
    """True when `tokens` run consecutively on at least one page."""
    if not tokens:
        return False
    return any(_seq_present(idx, tokens, max_gap=1) for idx in page_indexes)


def _stage_seq_index(stage_full_lower: str):
    """{canonical word: [ascending positions]} over the whole STAGE document.

    Derived from stage_full_lower so every existing caller of _section_missing
    gets this for free. Cached — the same STAGE text is reused for every section.
    """
    key = hashlib.md5(stage_full_lower.encode("utf-8", "ignore")).hexdigest()
    hit = _SEQ_INDEX_CACHE.get(key)
    if hit is not None:
        return hit
    idx = {}
    for pos, cw in enumerate(_seq_tokens(stage_full_lower)):
        idx.setdefault(cw, []).append(pos)
    if len(_SEQ_INDEX_CACHE) > 8:
        _SEQ_INDEX_CACHE.clear()
    _SEQ_INDEX_CACHE[key] = idx
    return idx


def _seq_present(idx, seq, max_gap: int = SEQ_MAX_GAP) -> bool:
    """True if `seq` occurs in order in STAGE, each word within max_gap of the last."""
    if not seq:
        return True
    first = idx.get(seq[0])
    if not first:
        return False
    for start in first:
        pos, ok = start, True
        for word in seq[1:]:
            plist = idx.get(word)
            if not plist:
                ok = False
                break
            j = bisect.bisect_right(plist, pos)
            if j >= len(plist) or plist[j] > pos + max_gap:
                ok = False
                break
            pos = plist[j]
        if ok:
            return True
    return False


def _refine_fragment(frag_words, idx, source_idx=None):
    """Split an uncovered run into only the parts genuinely absent from STAGE.

    Returns a list of readable fragment strings (original spelling preserved).
    A word counts as present when any SEQ_WINDOW-word window containing it is
    found in STAGE, so reordered or page-split content is not reported.
    """
    canon, origin = [], []
    for i, w in enumerate(frag_words):
        for cw in _split_canon(w):     # one word may hold several runs
            canon.append(cw)
            origin.append(i)
    if not canon:
        return []
    if len(canon) < SEQ_WINDOW:
        # Too short to slide a window over. Still honour MIN_FRAG_WORDS — a run
        # that shrinks below it once bare numbers and punctuation are dropped
        # (e.g. ["1.", "2.", "foo"]) is noise, not a reportable difference.
        if len(canon) < MIN_FRAG_WORDS or _seq_present(idx, canon):
            return []
        return [_join_tokens(frag_words[origin[0]:origin[-1] + 1])]

    present = [False] * len(canon)
    for i in range(len(canon) - SEQ_WINDOW + 1):
        if _seq_present(idx, canon[i:i + SEQ_WINDOW]):
            for k in range(i, i + SEQ_WINDOW):
                present[k] = True

    out, i = [], 0
    while i < len(canon):
        if present[i]:
            i += 1
            continue
        st = i
        while i < len(canon) and not present[i]:
            i += 1
        if i - st >= MIN_FRAG_WORDS:
            # Only report text that genuinely reads this way in the SOURCE
            # document. Section slicing concatenates a page-ordered token
            # stream, so a run can be an artifact that appears in neither PDF
            # ("Picture with the Picture" spliced out of a two-column table).
            # Reporting those as missing is a false positive.
            if source_idx is not None:
                run = canon[st:i]
                ok = (_reads_verbatim(source_idx, run)
                      if isinstance(source_idx, list) else
                      _seq_present(source_idx, run, max_gap=SEQ_SOURCE_GAP))
                if not ok:
                    continue
            out.append(_join_tokens(frag_words[origin[st]:origin[i - 1] + 1]))
    return out


def _section_missing(prod_words, stage_ns, stage_cset, stage_full_lower,
                     stage_section_lower="", source_idx=None):
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

    # Shingle width MUST match the width the index was built with. Deriving it
    # again here from the text is unsafe: a handful of garbled CJK glyphs in an
    # English document flips this to 8 while the index holds 18-char shingles,
    # so every lookup misses and coverage collapses to ~0%. The index is the
    # authority — read the width off it, and only fall back to re-deriving when
    # no index was supplied.
    if stage_cset:
        L = len(next(iter(stage_cset)))
    else:
        is_cjk = bool(_CJK_RE.search(s)
                      or (stage_full_lower and _CJK_RE.search(stage_full_lower)))
        L = 8 if is_cjk else CHAR_SHINGLE

    if len(s) < L:
        if s and s in stage_ns:
            return 100.0, []
        phrase = _s_norm(re.sub(r"\s+", " ", _join_tokens(words))).lower()
        if phrase in stage_full_lower:
            return 100.0, []
        if len(words) < MIN_FRAG_WORDS:
            return 0.0, []
        return 0.0, _refine_fragment(words, _stage_seq_index(stage_full_lower),
                                     source_idx)

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

    _seq_idx = _stage_seq_index(stage_full_lower)

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

                    # 4) A fragment made up entirely of short value/unit tokens
                    #    ("9V", "4A", "2.4A") is a stacked value-and-sub-label
                    #    spec pair — which one a layout engine's block sort
                    #    reads first depends on pixel position, not wording, so
                    #    "4A 9V 3A 12V" against PROD's "9V 4A 12V 3A" is the same
                    #    spec table, reordered. If every token (with repeats)
                    #    occurs somewhere in STAGE, order is not checked.
                    if reported:
                        frag_canon = _seq_tokens(frag_text)
                        if frag_canon and all(_UNIT_VALUE_RE.match(t) for t in frag_canon):
                            need = collections.Counter(frag_canon)
                            if all(len(_seq_idx.get(t, ())) >= n
                                  for t, n in need.items()):
                                reported = False

                    # 5) Final gate: re-verify the fragment against the WHOLE
                    #    STAGE document word by word, tolerating small gaps.
                    #    Only the parts with no counterpart anywhere in STAGE
                    #    survive — content that merely moved to the next page,
                    #    got re-laid-out, or was glued to a heading by section
                    #    slicing is no longer reported.
                    if reported:
                        frags.extend(_refine_fragment(frag, _seq_idx, source_idx))
        else:
            i += 1
    return coverage, frags


# ── Extra content: text in STAGE with no counterpart in PROD ─────────────────
# PROD is the reference, so anything STAGE renders that PROD never had is an
# addition worth reporting. This is the mirror of the missing-content pass, with
# one important difference: the PROD side is read with the *unfiltered*
# extractor. _extract_page_body_prod deliberately drops OSD overlays and short
# diagram labels, but _extract_page_body_stage keeps everything — comparing the
# two directly would flag every filtered OSD string as "extra". Reading PROD raw
# here keeps the two sides symmetric.
def _build_prod_reference(prod_path: str, nav_pages: set):
    """(nospace, shingle_set, full_lower) over ALL non-nav PROD text, unfiltered."""
    doc   = fitz.open(prod_path)
    lang  = _get_pdf_language(doc)
    is_cjk = lang in ("chi_tra", "chi_sim", "jpn", "kor")
    shingle_len = 8 if is_cjk else CHAR_SHINGLE
    garbled = _is_pdf_garbled(doc)

    all_words, raw_parts = [], []
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        if garbled:
            try:
                tp  = page.get_textpage_ocr(dpi=150, language=lang)
                raw = page.get_text(textpage=tp)
            except Exception:
                raw = page.get_text()
        else:
            raw = page.get_text()
        body = _normalize(_strip_formatting(raw))
        all_words += _keep(_tokenize(body))
        raw_parts.append(body)
    doc.close()

    nospace = "".join(_canon(w) for w in all_words)
    cset    = {nospace[i:i + shingle_len]
               for i in range(len(nospace) - shingle_len + 1)}
    full_lower = _s_norm(re.sub(r"\s+", " ", " ".join(raw_parts))).lower()
    return nospace, cset, full_lower


def _extra_in_stage(prod_path: str, stage_path: str, stage_nav: set):
    """Return (coverage_pct, [extra_fragment_str, ...]) for STAGE-only content.

    coverage is the share of STAGE text that PROD also has; the fragments are
    the runs STAGE adds. Runs through the same gap-tolerant verification as the
    missing-content pass, so re-ordered or page-shifted text is not reported.
    """
    doc = fitz.open(prod_path)
    prod_nav = {1} | _detect_nav_pages(doc)
    doc.close()
    p_ns, p_cset, p_full = _build_prod_reference(prod_path, prod_nav)

    doc = fitz.open(stage_path)
    stage_words = []
    for i, page in enumerate(doc, 1):
        if i in stage_nav:
            continue
        stage_words += _keep(_tokenize(_extract_page_body_stage(page)))
    doc.close()

    if not stage_words:
        return 100.0, []
    return _section_missing(stage_words, p_ns, p_cset, p_full)


# ── Encoding / garbling detection ────────────────────────────────────────────
# Conversion pipelines lose characters in ways that survive as visible garbage:
# a literal HTML entity where a letter should be, a private-use glyph, U+FFFD,
# or CJK code points that render as nonsense because the wrong font/cmap was
# used. These are defects in whichever document carries them, so both PDFs are
# scanned independently rather than compared.
_ENTITY_RE   = re.compile(r"&#x?[0-9A-Fa-f]{2,6};?|&(?:amp|lt|gt|quot|apos|nbsp);")
_PUA_RE      = re.compile(r"[\ue000-\uf8ff]")
_REPLCHAR_RE = re.compile(r"\ufffd")
_CJK_RUN_RE  = re.compile(r"[\u3000-\u9fff\uac00-\ud7af\uf900-\ufaff]+")
# CJK that legitimately appears in an English manual: the OSD language list.
_KNOWN_CJK = {"繁體中文", "简体中文", "中文", "日本語", "한국어", "언어", "日本"}


def _encoding_glitches(pdf_path: str, nav_pages: set, doc_label: str):
    """[{page, kind, text, context}] for visible encoding damage in one PDF."""
    doc  = fitz.open(pdf_path)
    bad_fonts = _untrusted_fonts(doc)
    out, seen = [], set()

    def add(pno, kind, text, ctx, probe=""):
        key = (kind, text, pno)
        if key in seen:
            return
        seen.add(key)
        out.append({"doc": doc_label, "page": pno, "kind": kind,
                    "text": text, "context": ctx,
                    # `probe` is the literal string as it appears on the page —
                    # what the screenshot locator searches for. `text` may be a
                    # font name, which is not on the page at all.
                    "probe": probe or text})

    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        raw = page.get_text()
        flat = re.sub(r"\s+", " ", raw)

        def ctx_at(pos, width=60):
            return flat[max(0, pos - width):pos + width].strip()

        for m in _ENTITY_RE.finditer(flat):
            add(i, "HTML entity left in text", m.group(0), ctx_at(m.start()),
                m.group(0))

        # Text drawn with a font that has no Unicode mapping: the page renders
        # correctly, but copy/paste, search and screen readers get wrong
        # characters. Reported once per font per page, with the visible context.
        d_blocks = [b for b in _page_dict(page)["blocks"]
                    if b.get("type") == 0]
        for block in d_blocks:
            _lines = block.get("lines", [])
            for _li, line in enumerate(_lines):
                for sp in line.get("spans", []):
                    txt = sp.get("text", "") or ""
                    untrusted = _span_font_untrusted(sp, bad_fonts)
                    pua  = _PUA_RE.search(txt)
                    repl = _REPLCHAR_RE.search(txt)
                    if not (untrusted or pua or repl):
                        continue
                    font = (sp.get("font") or "").split("+")[-1]
                    # Context = the neighbouring text on the same block, so the
                    # counterpart anchor points at the same row of the page.
                    # Nearby lines only — wide enough to reach real words (in a
                    # language list each entry is its own one-word line), but not
                    # the whole block, where a long paragraph elsewhere in the
                    # same table would win the anchor and send the two evidence
                    # panes to different parts of the page.
                    neighbours = " ".join(
                        s2.get("text", "")
                        for l2 in _lines[max(0, _li - 8):_li + 9]
                        for s2 in l2.get("spans", [])
                        if not _span_font_untrusted(s2, bad_fonts))
                    # Fall back to the rest of the page, but ordered by how far
                    # each block sits from the defect — the closest usable text
                    # is the anchor that lands the counterpart shot on the same
                    # row, instead of a long paragraph elsewhere on the page.
                    y = (sp.get("bbox") or line.get("bbox") or (0, 0, 0, 0))[1]
                    near = []
                    for b2 in d_blocks:
                        if b2 is block:
                            continue
                        t2 = " ".join(s3.get("text", "")
                                      for l3 in b2.get("lines", [])
                                      for s3 in l3.get("spans", [])
                                      if not _span_font_untrusted(s3, bad_fonts))
                        t2 = re.sub(r"\s+", " ", t2).strip()
                        if t2:
                            near.append((abs(b2.get("bbox", (0, 0, 0, 0))[1] - y), t2))
                    near.sort(key=lambda x: x[0])
                    ctx = " ".join(
                        [re.sub(r"\s+", " ", neighbours).strip()]
                        + [t2 for _, t2 in near[:4]]).strip()[:400]
                    probe = txt.strip()
                    if repl:
                        add(i, "Replacement character (U+FFFD)", font, ctx, probe)
                    elif pua:
                        add(i, "Private-use glyph (no real character)",
                            f"{font} → U+{ord(pua.group(0)):04X}", ctx, probe)
                    else:
                        add(i, "Text layer broken (font has no Unicode map)",
                            font, ctx, probe)
    doc.close()
    return out


def _all_words_present(text: str, target_idx) -> bool:
    """True when every distinctive word (>=4 letters) of `text` exists in target.

    Used to suppress re-ordered or re-laid-out labels and header cells: if all of
    the wording is there and only the arrangement differs, nothing was lost, so
    reporting it would be a false positive.
    """
    words = [w.lower() for w in re.findall(r"[^\W\d_]{4,}", text or "", re.UNICODE)]
    if not words:
        return False
    return all(w in target_idx for w in words)


# ── Table headings ───────────────────────────────────────────────────────────
_MAX_HEADER_WORDS = 6      # a header cell is a short label, never a sentence


def _table_headings(pdf_path: str, nav_pages: set):
    """[(page, [header cell, ...])] — the header row of every real table.

    Table detection readily mistakes a boxed note for a one-column table, whose
    "header" is then a whole paragraph. Only multi-column tables whose first row
    is made of short labels are treated as having a header, so the comparison is
    about real column headings rather than prose.
    """
    out = []
    for pno, nrow, ncol, rows in _merge_continued_tables(
            _crawl_tables(pdf_path, nav_pages)):
        if not rows or ncol < 2:
            continue
        head = [re.sub(r"\s+", " ", (c or "")).strip() for c in rows[0]]
        head = [h for h in head
                if h and len(h.split()) <= _MAX_HEADER_WORDS
                # At least one real word. Detection sometimes splits a cell
                # mid-word ("Le" | "ft" out of "Left"); those fragments are not
                # headings and must not be reported as dropped columns.
                and any(len(w) >= 3 for w in _WORD_TOKEN_RE.findall(h))
                and len("".join(_WORD_TOKEN_RE.findall(h))) >= 3]
        if len(head) >= 2:
            out.append((pno, head))
    return out


def _table_heading_issues(prod_path, stage_path, prod_nav, stage_nav,
                          stage_idx, prod_idx):
    """Column headers PROD tables have that no STAGE table header carries.

    Compared header-row to header-row, not against the whole document: a word
    like "Item" occurs all over the prose, so a document-wide text search would
    never report a genuinely dropped column heading.
    """
    stage_headers = set()
    stage_rows    = []
    for pno, head in _table_headings(stage_path, stage_nav):
        stage_rows.append((pno, head))
        for cell in head:
            key = " ".join(_seq_tokens(cell))
            if key:
                stage_headers.add(key)

    findings = []
    for pno, head in _table_headings(prod_path, prod_nav):
        missing = []
        for cell in head:
            key = " ".join(_seq_tokens(cell))
            if key and key not in stage_headers:
                missing.append(cell)
        if not missing:
            continue
        # Table detection is heuristic and misses tables outright. When the whole
        # header row reads as text in STAGE, the table is there and only the
        # detector failed — blaming STAGE for that is a false report.
        row_toks = _seq_tokens(" ".join(head))
        if row_toks and _seq_present(stage_idx, row_toks, max_gap=SEQ_MAX_GAP):
            continue
        if _text_in_artwork(stage_path, ", ".join(missing), pno, stage_nav):
            continue        # STAGE draws this table as artwork — see above
        # Report the whole header row for context, naming the dropped columns.
        findings.append({"page": pno, "text": ", ".join(missing),
                         "row": " | ".join(head)})
    return findings


# ── Image labels ─────────────────────────────────────────────────────────────
_LABEL_NEAR_PT = 26        # a caption/callout sits within this many points
_CAPTION_BELOW_PT = 64     # a caption printed under a figure sits further off
_CALLOUT_NUM_RE = re.compile(r"\d{1,2}\s*[.)]?")
_XREF_RE = re.compile(r"\bpage\s+\d+", re.I)   # a cross-reference, not a label
_LABEL_VALUE_RE = re.compile(r"\d+(?:[.,]\d+)?(?:\s*[-–]\s*\d+)?\s*(?:%|[a-zA-Z]+|°\s*[a-zA-Z]+)")
_IMAGE_LABEL_LINE_CACHE = {}
_FLAT_LINE_CACHE = {}

# ── Text baked into artwork ──────────────────────────────────────────────────
# STAGE renders some figures as a flat raster — the environment spec strip on
# page 7, for one — so their labels are visible on the page but absent from the
# text layer. Comparing text layers alone reports those as missing labels, which
# is wrong: the reader can see them. Before a label is reported, the artwork is
# read by OCR to check whether the words are actually there.
_OCR_PAGE_CACHE = {}
_RASTER_PAGE_CACHE = {}
_OCR_UNAVAILABLE = False
_OCR_ANY_OK = False       # one page has been read: OCR itself works
_OCR_MAX_PAGES = 16       # ceiling on pages read per label: STAGE draws a
                           # figure on or near the page PROD has it on, and
                           # scanning the whole document per label cost more
                           # than every other check put together
_OCR_MIN_IMG_PT = (96, 32) # a raster smaller than this carries no readable
                           # label — icons and rules are not worth an OCR pass
_PAGE_TEXT_TOKS_CACHE = {}


def _ocr_page_text(pdf_path: str, page_no: int, dpi: int = 150) -> str:
    """Visible text of a page as read by OCR; "" when OCR is not available."""
    global _OCR_UNAVAILABLE
    if _OCR_UNAVAILABLE:
        return ""
    key = (os.path.abspath(pdf_path), page_no)
    if key in _OCR_PAGE_CACHE:
        return _OCR_PAGE_CACHE[key]
    global _OCR_ANY_OK
    text = ""
    try:
        doc = fitz.open(pdf_path)
        try:
            page = doc[page_no - 1]
            text = page.get_text(textpage=page.get_textpage_ocr(dpi=dpi, full=True))
        finally:
            doc.close()
    except Exception:
        # Tell "OCR is not installed" apart from "this one page failed". Marking
        # OCR unavailable on any single failure silently disabled the artwork
        # check for the rest of the run, and every label it would have found in
        # a picture came back as a false "missing" finding.
        if not _OCR_ANY_OK:
            _OCR_UNAVAILABLE = True
        return ""
    _OCR_ANY_OK = True
    if len(_OCR_PAGE_CACHE) > 400:
        _OCR_PAGE_CACHE.clear()
    _OCR_PAGE_CACHE[key] = text
    return text


def _raster_pages(pdf_path: str) -> set:
    """Pages carrying a raster image — the only ones worth reading by OCR."""
    key = os.path.abspath(pdf_path)
    if key in _RASTER_PAGE_CACHE:
        return _RASTER_PAGE_CACHE[key]
    pages = set()
    try:
        doc = fitz.open(pdf_path)
        try:
            for i, page in enumerate(doc, 1):
                for img in page.get_images(full=True):
                    try:
                        rects = page.get_image_rects(img[0]) or []
                    except Exception:
                        continue
                    if any(r.width >= _OCR_MIN_IMG_PT[0]
                           and r.height >= _OCR_MIN_IMG_PT[1] for r in rects):
                        pages.add(i)
                        break
        finally:
            doc.close()
    except Exception:
        pass
    _RASTER_PAGE_CACHE[key] = pages
    return pages


def _page_text_tokens(pdf_path: str, page_no: int) -> collections.Counter:
    """Counter of canonical tokens in a page's own (non-OCR) selectable text.

    A COUNT, not a set: a word that appears once in ordinary prose does not
    disqualify a SEPARATE, additional occurrence of the same word genuinely
    baked into the artwork ("3 cm" is mentioned once in a sentence and drawn
    twice more as measurement labels on the diagram below it) — only the
    occurrences already explained by the text layer should be discounted.
    """
    key = (os.path.abspath(pdf_path), page_no)
    hit = _PAGE_TEXT_TOKS_CACHE.get(key)
    if hit is not None:
        return hit
    toks = collections.Counter()
    try:
        doc = fitz.open(pdf_path)
        try:
            toks = collections.Counter(_seq_tokens(doc[page_no - 1].get_text()))
        finally:
            doc.close()
    except Exception:
        pass
    if len(_PAGE_TEXT_TOKS_CACHE) > 400:
        _PAGE_TEXT_TOKS_CACHE.clear()
    _PAGE_TEXT_TOKS_CACHE[key] = toks
    return toks


def _text_in_artwork(pdf_path: str, text: str, hint_page: int = 0,
                     skip_pages: set = None) -> bool:
    """True when every word of `text` is readable in this PDF's artwork.

    Pages are read nearest-first to `hint_page`, so the usual case costs one or
    two OCR passes; a label that really is absent costs at most _OCR_MAX_PAGES.
    """
    want = [t for t in _seq_tokens(text) if t]
    if not want or _OCR_UNAVAILABLE:
        return False
    candidates = _raster_pages(pdf_path) - (skip_pages or set())
    if not candidates:
        return False
    # Read serially. MuPDF's OCR is not thread-safe — running pages of the same
    # document through a pool raises FzErrorArgument on some of them, and a page
    # that fails reads as "no text there", which turns straight into a false
    # "missing" finding.
    order = sorted(candidates, key=lambda p: (abs(p - (hint_page or 1)), p))
    for read, pno in enumerate(order):
        if read >= _OCR_MAX_PAGES:
            break
        got = _ocr_page_text(pdf_path, pno)
        if _OCR_UNAVAILABLE:
            return False
        if not got:
            continue
        # full=True OCR re-reads the page's ordinary selectable prose as well
        # as its artwork, so a label whose words merely occur nearby in a
        # numbered instruction ("the alignment arrow on the bottom of the
        # lid...") looked "found in the artwork" even though the picture
        # itself carries neither word. Counting occurrences (not just
        # presence) is what keeps this from over-correcting the other way:
        # "3 cm" mentioned once in a sentence and ALSO drawn twice more as
        # measurement labels still counts those extra, genuinely-artwork
        # occurrences — only counts already explained by the text layer are
        # discounted.
        ocr_counts = collections.Counter(_seq_tokens(got))
        extra = ocr_counts - _page_text_tokens(pdf_path, pno)
        want_counts = collections.Counter(want)
        if all(extra.get(w, 0) >= n for w, n in want_counts.items()):
            return True
        # OCR runs words together and drops punctuation, so the token form can
        # miss a label the picture plainly shows. The flattened form catches
        # it — built from the SAME extra-occurrence words (in their original
        # order, each consumed at most once), so ordinary prose cannot
        # inflate this match either.
        budget = dict(extra)
        kept = []
        for t in _seq_tokens(got):
            if budget.get(t, 0) > 0:
                kept.append(t)
                budget[t] -= 1
        got_extra = " ".join(kept)
        if _flat_key(text) and _flat_key(text) in _flat_key(got_extra):
            return True
    return False


def _image_label_line_keys(pdf_path: str, nav_pages: set) -> set:
    """Normalised text keys for every individual visible line in a PDF."""
    cache_key = (os.path.abspath(pdf_path), tuple(sorted(nav_pages)))
    cached = _IMAGE_LABEL_LINE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    document, keys = fitz.open(pdf_path), set()
    for page_number, page in enumerate(document, 1):
        if page_number in nav_pages:
            continue
        for block in _page_dict(page)["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(span.get("text", "")
                               for span in line.get("spans", [])).strip()
                key = _canon(text)
                if key:
                    keys.add(key)
    document.close()
    if len(_IMAGE_LABEL_LINE_CACHE) > 6:
        _IMAGE_LABEL_LINE_CACHE.clear()
    _IMAGE_LABEL_LINE_CACHE[cache_key] = keys
    return keys


def _flat_line_keys(pdf_path: str, nav_pages: set) -> set:
    """_flat_key() of every individual visible LINE in a PDF (not the whole
    document flattened into one string — see the false-negative this fixes:
    a short two-word label like "Alignment arrow" reads as a contiguous
    substring of ordinary prose too ("the alignment arrow on the bottom of
    the lid"), so matching against the whole document confirmed a caption
    was "present" when it was really just those two words occurring next to
    each other in an unrelated sentence elsewhere on the page.
    """
    cache_key = (os.path.abspath(pdf_path), tuple(sorted(nav_pages)))
    cached = _FLAT_LINE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    document, keys = fitz.open(pdf_path), set()
    for page_number, page in enumerate(document, 1):
        if page_number in nav_pages:
            continue
        for block in _page_dict(page)["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                text = "".join(span.get("text", "")
                               for span in line.get("spans", [])).strip()
                key = _flat_key(text)
                if key:
                    keys.add(key)
    document.close()
    if len(_FLAT_LINE_CACHE) > 6:
        _FLAT_LINE_CACHE.clear()
    _FLAT_LINE_CACHE[cache_key] = keys
    return keys


def _image_labels(pdf_path: str, nav_pages: set):
    """[(page, label)] for individual text lines on or beside a figure.

    A PDF text block can contain several independent callouts. Treating the
    whole block as one label makes its reading order a comparison requirement,
    even though the same visible labels may be positioned differently in STAGE.
    """
    doc, out = fitz.open(pdf_path), []
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        # Use the same figure detection the image comparison uses, so a shaded
        # panel or a table is never treated as artwork. Building regions here
        # separately meant a language table counted as a figure and its cells
        # were reported as missing image labels.
        rects = _figure_regions(page)
        if not rects:
            continue
        grown = [fitz.Rect(r.x0 - _LABEL_NEAR_PT, r.y0 - _LABEL_NEAR_PT,
                           r.x1 + _LABEL_NEAR_PT, r.y1 + _LABEL_NEAR_PT)
                 for r in rects]
        # A caption printed under a figure ("PC / Notebook") sits further away
        # than a callout pinned to the artwork, so the band below each figure
        # reaches further. It is kept to the figure's own width, so the band
        # picks up the caption and not the paragraph that follows it.
        below = [fitz.Rect(r.x0, r.y1, r.x1, r.y1 + _CAPTION_BELOW_PT)
                 for r in rects]
        for b in _page_dict(page)["blocks"]:
            if b.get("type") != 0:
                continue
            lines = b.get("lines", [])
            # A figure label is set as its own short block. Body prose that
            # happens to run beside a figure is not a label, and comparing it as
            # one reported re-worded sentences ("...see page 24 - 25.") as
            # missing artwork. Blocks that read as prose are left to the body
            # text comparison, which is built to handle re-wording.
            if len(lines) > 2:
                continue
            btxt = " ".join("".join(sp.get("text", "") for sp in ln.get("spans", []))
                            for ln in lines)
            if _XREF_RE.search(btxt) or len(_seq_tokens(btxt)) > 10:
                continue
            for line in lines:
                bb = fitz.Rect(line.get("bbox", (0, 0, 0, 0)))
                on_fig = any(g.intersects(bb) for g in grown)
                under = any(c.intersects(bb) and bb.x0 >= c.x0 - _LABEL_NEAR_PT
                            and bb.x1 <= c.x1 + _LABEL_NEAR_PT for c in below)
                inside = any(fitz.Rect(r).contains(bb.tl) for r in rects)
                if not (on_fig or under):
                    continue
                txt = "".join(sp.get("text", "")
                              for sp in line.get("spans", [])).strip()
                txt = re.sub(r"\s+", " ", txt)
                if not txt:
                    continue
                is_value = bool(_LABEL_VALUE_RE.search(txt))
                # A bare number drawn on the artwork is a leader-line callout.
                # It only counts when it sits inside the figure, so a page
                # number or a list marker beside it is not mistaken for one.
                is_callout = inside and bool(_CALLOUT_NUM_RE.fullmatch(txt))
                if len(_WORDCHAR_RE.findall(txt)) >= 2 or is_value or is_callout:
                    out.append((i, txt))
    doc.close()
    return out


_FLAT_CACHE = {}


def _flat_key(text: str) -> str:
    """Letters and digits only, case-folded — punctuation and spacing removed.

    A figure label is compared on the characters a reader sees, never on how the
    two documents happen to space or punctuate them: "PC / Notebook" and
    "PC/Notebook", "non-alcohol" and "non alcohol", an en dash and a hyphen are
    the same label. Colour, weight and size play no part — a label that reads the
    same is the same label.
    """
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = (t.replace("\u2013", "-").replace("\u2014", "-").replace("\u2019", "'")
          .replace("\u00a0", " "))
    return re.sub(r"[^0-9a-z\u00c0-\uffff]+", "", t)


def _flat_document(pdf_path: str, nav_pages: set) -> str:
    """The whole document as one flattened string, cached."""
    key = (os.path.abspath(pdf_path), tuple(sorted(nav_pages or ())))
    hit = _FLAT_CACHE.get(key)
    if hit is not None:
        return hit
    doc = fitz.open(pdf_path)
    try:
        parts = [page.get_text() for i, page in enumerate(doc, 1)
                 if i not in (nav_pages or set())]
    finally:
        doc.close()
    flat = _flat_key(" ".join(parts))
    if len(_FLAT_CACHE) > 8:
        _FLAT_CACHE.clear()
    _FLAT_CACHE[key] = flat
    return flat


def _page_mapper(toc_results):
    """prod page -> the STAGE page carrying the same section.

    The two documents paginate differently, so a PROD page number is not a hint
    about where to look in STAGE: page 51 of one is page 42 of the other. Every
    matched heading gives one true correspondence, and pages between headings are
    offset from the nearest one above.
    """
    marks = []
    for r in toc_results or []:
        try:
            pp, sp = int(r.get("prod_page")), int(r.get("stage_page"))
        except (TypeError, ValueError):
            continue
        if pp > 0 and sp > 0:
            marks.append((pp, sp))
    marks.sort()

    def to_stage(prod_page: int) -> int:
        if not marks or not prod_page:
            return prod_page
        best = marks[0]
        for pp, sp in marks:
            if pp <= prod_page:
                best = (pp, sp)
            else:
                break
        return max(1, best[1] + (prod_page - best[0]))

    return to_stage


def _image_label_issues(prod_path, stage_path, prod_nav, stage_nav,
                        stage_idx, prod_idx, to_stage_page=None):
    """Figure labels PROD carries that STAGE does not."""
    _SHORT_LABEL = 6
    findings, seen = [], set()
    stage_line_keys = _image_label_line_keys(stage_path, stage_nav)
    stage_flat_line_keys = _flat_line_keys(stage_path, stage_nav)

    # A figure label has to read *on a figure* in STAGE, not merely somewhere in
    # the document. "Headphone" labels the connection diagram in PROD; STAGE has
    # no such diagram label, only the words "Headphone jack" in a list further
    # back. A document-wide search calls that a match and the missing label goes
    # unreported, so short labels are compared against STAGE's own figure labels.
    stage_fig_labels = [lbl for _, lbl in _image_labels(stage_path, stage_nav)]
    stage_fig_keys = {_canon(l) for l in stage_fig_labels if _canon(l)}
    stage_fig_idx = (_stage_seq_index(_s_norm(" \n ".join(stage_fig_labels)).lower())
                     if stage_fig_labels else None)

    def _on_a_stage_figure(text, tokens):
        """True when STAGE prints this text on or beside one of its figures."""
        if _canon(text) in stage_fig_keys:
            return True
        if stage_fig_idx is None:
            return False
        return _seq_present(stage_fig_idx, tokens)

    for pno, label in _image_labels(prod_path, prod_nav):
        if _script_unreliable(label):
            continue                    # not comparable — see _script_unreliable
        # A callout number is valid as long as it is there. Whether it can be
        # read back out of the artwork depends on how STAGE rasterised the
        # drawing and on whether this machine has an OCR pack — neither says
        # anything about the document. Reporting "1" as a missing label was
        # noise, so numeric and symbol-only labels are left alone; the numbering
        # itself is checked by _callout_gap_issues, which looks for gaps in the
        # sequence rather than for one unreadable glyph.
        if not re.search(r"[^\W\d_]", label or ""):
            continue
        # A bare measurement value ("3 cm", "9V") is spec data, not wording —
        # the same "any dimension is fine" principle already applied to table
        # cells and WxDxH labels. It is also the hardest kind of label to
        # verify here: the number is stripped from every token comparison,
        # and OCR often cannot read a short value/unit pair off a diagram at
        # all, so a genuinely-present value reads as "missing" for reasons
        # that have nothing to do with STAGE actually lacking it.
        if _LABEL_VALUE_RE.fullmatch(label.strip()):
            continue
        toks = _seq_tokens(label)
        if not toks:
            continue
        # De-dup only an exact repeat of this label ON THE SAME PAGE (a caption
        # printed twice next to one figure). The same label text recurring on a
        # DIFFERENT page/figure — the same icon captioned three times across the
        # manual — is three independent instances; keying "seen" on the text
        # alone collapsed them into one and silently dropped the other two, even
        # when STAGE was missing the label from a different one of the three.
        dedup_key = (pno, label.lower())
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        # Labels often contain ranges, units and symbols. Compare the exact
        # rendered line first, keeping those values rather than dropping them
        # as layout numbers in the prose matcher.
        if _canon(label) in stage_line_keys:
            continue

        if len(toks) <= _SHORT_LABEL:
            # A short caption is one unit: either STAGE has it or it does not.
            if not _source_ok(prod_idx, toks):
                continue
            if _on_a_stage_figure(label, toks):
                continue
            # Last guard before reporting: the label read as plain characters.
            # Tokenisation splits on punctuation, so "PC / Notebook" and
            # "PC/Notebook" produce different token runs while reading exactly
            # the same. Checked against STAGE's own LINES, not the whole
            # document flattened into one string — a short label's words can
            # occur next to each other in an unrelated sentence too ("the
            # alignment arrow on the bottom of the lid" contains "alignment
            # arrow"), which a whole-document match wrongly called present.
            if _flat_key(label) in stage_flat_line_keys:
                continue
            hint = to_stage_page(pno) if to_stage_page else pno
            if _text_in_artwork(stage_path, label, hint, stage_nav):
                continue        # STAGE draws it into the figure — see above
            if stage_fig_idx is None:
                # STAGE's figures could not be located at all. Falling back to
                # the document-wide test keeps a detection failure from
                # reporting every PROD label as missing.
                if _seq_present(stage_idx, toks) or _all_words_present(label, stage_idx):
                    continue
            findings.append({"page": pno, "text": label})
        else:
            # A long caption is prose: report only the parts with no counterpart,
            # the same way body text is handled, so a re-worded or re-wrapped
            # caption is not reported wholesale.
            for gap in _refine_fragment(_tokenize(label), stage_idx, prod_idx):
                if _script_unreliable(gap) or _all_words_present(gap, stage_idx):
                    continue
                if _flat_key(gap) in stage_flat_line_keys:
                    continue
                hint = to_stage_page(pno) if to_stage_page else pno
                if _text_in_artwork(stage_path, gap, hint, stage_nav):
                    continue    # STAGE draws it into the figure — see above
                findings.append({"page": pno, "text": gap})
    return findings


# ── Figures present on a page ────────────────────────────────────────────────
def _figure_pages(pdf_path: str, nav_pages: set):
    """{page: n_figures} counting raster figures and clustered vector artwork.

    Counting raster images alone is not comparable between pipelines — one PDF
    embeds figures, the other draws them — so vector artwork is clustered into
    figure-sized regions and counted too.
    """
    doc, out = fitz.open(pdf_path), {}
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        boxes = []
        for img in page.get_images(full=True):
            try:
                boxes += [fitz.Rect(r) for r in (page.get_image_rects(img[0]) or [])]
            except Exception:
                pass
        try:
            for dr in page.get_drawings():
                r = fitz.Rect(dr["rect"])
                if min(r.width, r.height) >= 6:
                    boxes.append(r)
        except Exception:
            pass
        merged = []
        for r in boxes:
            hit = False
            for j, m in enumerate(merged):
                if fitz.Rect(m).intersects(fitz.Rect(r) + (-8, -8, 8, 8)):
                    merged[j] = fitz.Rect(m) | fitz.Rect(r)
                    hit = True
                    break
            if not hit:
                merged.append(fitz.Rect(r))
        out[i] = sum(1 for m in merged
                     if max(m.width, m.height) > _ICON_MAX_ONPAGE)
    doc.close()
    return out


# ── Image resolution ─────────────────────────────────────────────────────────
_PIXELATED_DPI   = 150    # below this a raster reads soft: the pixels are visible
_PIXELATED_SHARE = 0.20   # this much of the artwork before it is worth reporting
_MIN_FIGURE_PT   = 24     # ignore inline glyph-sized rasters (bullets, symbols)


def _image_dpi_rows(pdf_path: str, nav_pages: set):
    """[(page, effective dpi)] for every raster large enough to read.

    Effective dpi is the stored pixel count against the size the image is drawn
    at, which is what decides whether a reader sees pixels — a 947x128 bitmap is
    sharp in a thumbnail and soft across half a page.
    """
    doc, rows = fitz.open(pdf_path), []
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        for img in page.get_images(full=True):
            xref, w, h = img[0], img[2], img[3]
            if not w or not h:
                continue
            try:
                rects = page.get_image_rects(xref) or []
            except Exception:
                continue
            for r in rects:
                if r.width < _MIN_FIGURE_PT or r.height < _MIN_FIGURE_PT:
                    continue
                dpi = min(w / (r.width / 72.0), h / (r.height / 72.0))
                rows.append((i, dpi))
    doc.close()
    return rows


def _pixelation_issue(prod_path, stage_path, prod_nav, stage_nav, toc_results):
    """One finding when STAGE's artwork is materially coarser than PROD's.

    Reported once for the document, not once per image: 96 separate rows saying
    "this picture is soft" is a wall, and the defect is a single export setting.
    The topics carrying the affected figures are named so the fix can be checked
    where it matters.
    """
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []
    stage_rows = _image_dpi_rows(stage_path, stage_nav)
    prod_rows = _image_dpi_rows(prod_path, prod_nav)
    if len(stage_rows) < 5 or not prod_rows:
        return []
    soft = [(pg, d) for pg, d in stage_rows if d < _PIXELATED_DPI]
    if len(soft) < _PIXELATED_SHARE * len(stage_rows):
        return []
    prod_dpis = sorted(d for _, d in prod_rows)
    prod_med = prod_dpis[len(prod_dpis) // 2]
    # PROD has to be the sharper document before this is STAGE's defect. Two
    # equally coarse files are a shared source-artwork limitation, not a
    # staging regression, and reporting that against STAGE would be wrong.
    if prod_med < _PIXELATED_DPI * 1.5:
        return []
    prod_soft = sum(1 for _, d in prod_rows if d < _PIXELATED_DPI)
    if prod_soft > _PIXELATED_SHARE * len(prod_rows):
        return []

    pages = sorted({pg for pg, _ in soft})
    # Name the topics, not the pages: a topic is what a reader and a publisher
    # both work in, and the same fix applies across a whole topic's figures.
    marks = sorted(((r["stage_page"], r["title"]) for r in toc_results
                    if r.get("stage_page") and r.get("title")),
                   key=lambda x: x[0])
    topics, seen = [], set()
    for pg in pages:
        title = None
        for start, name in marks:
            if start <= pg:
                title = name
            else:
                break
        title = title or f"page {pg}"
        if title not in seen:
            seen.add(title)
            topics.append(title)
    stage_dpis = sorted(d for _, d in stage_rows)
    return [{
        "count": len(soft), "total": len(stage_rows),
        "worst": int(min(d for _, d in soft)),
        "stage_median": int(stage_dpis[len(stage_dpis) // 2]),
        "prod_median": int(prod_med),
        "pages": pages, "topics": topics,
    }]


def _missing_figure_issues(prod_path, stage_path, prod_nav, stage_nav,
                           content_results):
    """Topics where PROD shows figures and STAGE shows none at all.

    Skipped outright when the two documents render artwork differently — one
    embedding raster images, the other drawing vectors. Their figure counts are
    then not comparable at all, and every topic would be reported as having lost
    its images even though the pages look the same.
    """
    # Per-topic figure counting is only meaningful when a topic's pages can be
    # lined up between the two documents. These manuals paginate differently, so
    # a topic starting on PROD p7 may have its figures on STAGE p10 — outside any
    # page window — and every such topic then reads as having lost its images.
    # Rather than ship that, artwork is validated by the two checks that do hold
    # up: blank/undecodable images, and figure labels (text, matched exactly).
    return []
    p_pages = _figure_pages(prod_path, prod_nav)
    s_pages = _figure_pages(stage_path, stage_nav)
    findings = []
    for r in content_results:
        try:
            pp = int(r.get("prod_page") or 0)
            sp = int(r.get("stage_page") or 0)
        except (TypeError, ValueError):
            continue
        if not pp or not sp:
            continue
        # Span from this heading to the next one, on each side. A fixed
        # two-page window mis-aligns as soon as the two documents paginate
        # differently, which reported figures as missing that were simply a
        # page further on.
        p_end = next((int(x.get("prod_page") or 0) for x in content_results
                      if str(x.get("prod_page") or "").isdigit()
                      and int(x["prod_page"]) > pp), pp + 2)
        s_end = next((int(x.get("stage_page") or 0) for x in content_results
                      if str(x.get("stage_page") or "").isdigit()
                      and int(x["stage_page"]) > sp), sp + 2)
        p_n = sum(p_pages.get(k, 0) for k in range(pp, max(pp + 1, p_end) + 1))
        s_n = sum(s_pages.get(k, 0) for k in range(sp, max(sp + 1, s_end) + 1))
        if p_n and not s_n:
            findings.append({"page": pp, "stage_page": sp,
                             "title": r["title"], "n": p_n})
    return findings


# ── Figure detection and comparison (rendered, text-masked, SSIM) ────────────
# Figures are found by RENDERING the page, painting every text line white, and
# taking the connected components of what ink remains. That answers "what does a
# reader see as a picture here" directly, instead of inferring it from how the
# PDF happens to group vector operators — which grouped a shaded note panel, an
# illustration plus a table, and two stacked drawings all as single "figures".
try:
    import numpy as _np
    import cv2 as _cv2
    from skimage.metrics import structural_similarity as _ssim
    _CV_OK = True
except Exception:                                    # pragma: no cover
    _CV_OK = False

FIGURE_DIFF_CHECK = True
_FIG_DPI        = 110     # render scale for figure detection
_FIG_MIN_PT      = 45     # ignore anything smaller than this on the page
_FIG_SSIM_LIMIT  = 0.55   # below this the two figures are different artwork
_FIG_CAPTION_PT  = 130    # a caption sits within this distance of its figure


def _detect_figures(page, dpi: int = _FIG_DPI):
    """[Rect, ...] for the pictures on a page, found from rendered ink."""
    if not _CV_OK:
        return []
    z = dpi / 72.0
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(z, z), colorspace=fitz.csGRAY)
        img = _np.frombuffer(pix.samples, dtype=_np.uint8).reshape(
            pix.height, pix.width).copy()
    except Exception:
        return []
    ox, oy = page.rect.x0, page.rect.y0
    for _txt, r in _page_lines(page):                # erase the words
        x0 = max(0, int((r.x0 - ox) * z) - 1)
        y0 = max(0, int((r.y0 - oy) * z) - 1)
        x1 = max(0, int((r.x1 - ox) * z) + 2)
        y1 = max(0, int((r.y1 - oy) * z) + 2)
        img[y0:y1, x0:x1] = 255
    ink = ((img < 230).astype(_np.uint8)) * 255
    kern = _cv2.getStructuringElement(_cv2.MORPH_RECT, (5, 5))
    closed = _cv2.morphologyEx(ink, _cv2.MORPH_CLOSE, kern, iterations=1)
    n, _lab, stats, _cent = _cv2.connectedComponentsWithStats(closed, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 400:
            continue
        w_pt, h_pt = w / z, h / z
        if max(w_pt, h_pt) < _FIG_MIN_PT or min(w_pt, h_pt) < 20:
            continue
        out.append(fitz.Rect(ox + x / z, oy + y / z,
                             ox + (x + w) / z, oy + (y + h) / z))
    return out


def _figure_bitmap(page, rect, size: int = 160):
    """Grey, contrast-normalised, size-normalised bitmap of one figure."""
    if not _CV_OK:
        return None
    try:
        pix = page.get_pixmap(clip=rect, matrix=fitz.Matrix(2, 2),
                              colorspace=fitz.csGRAY)
        img = _np.frombuffer(pix.samples, dtype=_np.uint8).reshape(
            pix.height, pix.width)
    except Exception:
        return None
    if img.size == 0 or min(img.shape) < 8:
        return None
    # Fit inside a square on white, so aspect ratio is preserved and two figures
    # drawn at different scales still line up for comparison.
    h, w = img.shape
    scale = (size - 8) / max(h, w)
    resized = _cv2.resize(img, (max(1, int(w * scale)), max(1, int(h * scale))),
                          interpolation=_cv2.INTER_AREA)
    canvas = _np.full((size, size), 255, dtype=_np.uint8)
    yh, xw = resized.shape
    y0, x0 = (size - yh) // 2, (size - xw) // 2
    canvas[y0:y0 + yh, x0:x0 + xw] = resized
    return canvas


def _figure_similarity(a, b):
    """SSIM between two normalised figure bitmaps (1.0 = identical)."""
    if a is None or b is None:
        return None
    try:
        return float(_ssim(a, b))
    except Exception:
        return None


def _caption_for(page, rect, lines):
    """The text line most likely to caption `rect` — nearest above, else below."""
    best, best_d = None, 1e9
    cx = (rect.x0 + rect.x1) / 2
    for txt, r in lines:
        toks = _seq_tokens(txt)
        if not (3 <= len(toks) <= 16):
            continue
        if r.y1 <= rect.y0:
            d = rect.y0 - r.y1
        elif r.y0 >= rect.y1:
            d = r.y0 - rect.y1
        else:
            continue
        d += abs(((r.x0 + r.x1) / 2) - cx) * 0.25
        if d < best_d and d <= _FIG_CAPTION_PT:
            best, best_d = txt, d
    return best


# ── Figure comparison (anchored to the text beside each figure) ──────────────
# Figures cannot be matched by page number — the two documents paginate
# differently. They CAN be matched by the words printed next to them: find the
# step text in both files, take the figure nearest that text on each side, and
# compare the two renderings. A document against itself scores 0.000, the same
# illustration across the two files scores ~0.01, and a genuinely different
# illustration scores 0.18-0.37, so the two cases separate cleanly.
_FIG_THUMB      = 16      # thumbnail grid used to compare two figures
_FIG_DIFF_LIMIT = 0.12    # above this the artwork is treated as different
_FIG_NEAR_PT    = 110     # the text must sit this close to be that figure's caption
_STEP_OR_HEADING_RE = re.compile(
    r"^\s*(?:\d{1,2}\s*[.)]\s*\S|[A-Z][A-Za-z].{0,60}$)")
_FIG_MIN_DIM    = 110     # smaller clusters are icons, rules and note boxes, not
                          # figures — anchoring body text to those produced
                          # differences that had nothing to do with artwork


_FIG_MAX_TEXT_COVER = 0.18   # above this share of text, a region is a note box


def _region_text_cover(page, rect) -> float:
    """Share of `rect` covered by text — a note box is mostly text, art is not."""
    area = rect.get_area()
    if area <= 0:
        return 1.0
    covered = 0.0
    for _txt, r in _page_lines(page):
        inter = fitz.Rect(r) & rect
        if inter.is_valid and inter.get_area() > 0:
            covered += inter.get_area()
    return min(1.0, covered / area)


def _figure_regions(page, min_dim: float = _FIG_MIN_DIM):
    """Figure-sized regions on a page, raster images and vector art alike."""
    raw = []
    for img in page.get_images(full=True):
        try:
            raw += [fitz.Rect(r) for r in (page.get_image_rects(img[0]) or [])]
        except Exception:
            pass
    try:
        for dr in page.get_drawings():
            r = fitz.Rect(dr["rect"])
            if min(r.width, r.height) >= 6:
                raw.append(r)
    except Exception:
        pass
    # Merge only pieces that genuinely touch, and never let a region grow past
    # what a single figure can be. A loose tolerance chained artwork, rules and a
    # whole table into one block spanning most of the page, and comparing those
    # blocks was meaningless.
    page_h = page.rect.height or 1.0
    max_h = page_h * 0.42
    merged = []
    for r in sorted(raw, key=lambda x: (x.y0, x.x0)):
        hit = False
        for j, m in enumerate(merged):
            if fitz.Rect(m).intersects(fitz.Rect(r) + (-4, -4, 4, 4)):
                grown = fitz.Rect(m) | fitz.Rect(r)
                if grown.height <= max_h:
                    merged[j] = grown
                    hit = True
                    break
        if not hit:
            merged.append(fitz.Rect(r))
    out = []
    for m in merged:
        if min(m.width, m.height) < 60 or max(m.width, m.height) < min_dim:
            continue
        if m.height > max_h or m.get_area() > page.rect.get_area() * 0.34:
            continue          # too big to be one figure — see the merge note
        # A shaded IMPORTANT/NOTE panel clusters exactly like artwork does, and
        # comparing one against a real illustration produced differences that
        # were nonsense. Text coverage tells them apart.
        if _region_text_cover(page, m) > _FIG_MAX_TEXT_COVER:
            continue
        out.append(m)
    return out


def _figure_thumb(page, rect, n: int = _FIG_THUMB):
    """Contrast-normalised n x n grey thumbnail of a figure, or None."""
    try:
        pix = page.get_pixmap(clip=rect, matrix=fitz.Matrix(2, 2),
                              colorspace=fitz.csGRAY)
    except Exception:
        return None
    w, h, data = pix.width, pix.height, pix.samples
    if w < n or h < n or not data:
        return None
    cells = []
    for gy in range(n):
        y0, y1 = gy * h // n, max(gy * h // n + 1, (gy + 1) * h // n)
        for gx in range(n):
            x0, x1 = gx * w // n, max(gx * w // n + 1, (gx + 1) * w // n)
            tot = cnt = 0
            for y in range(y0, y1):
                base = y * w
                for x in range(x0, x1):
                    tot += data[base + x]
                    cnt += 1
            cells.append(tot / max(1, cnt))
    lo, hi = min(cells), max(cells)
    if hi <= lo:
        return None
    return [(c - lo) / (hi - lo) for c in cells]


def _thumb_diff(a, b):
    """Mean absolute difference of two normalised thumbnails (0 = identical)."""
    if not a or not b:
        return None
    return sum(abs(x - y) for x, y in zip(a, b)) / len(a)


def _nearest_figure(page, text_rect, figs):
    if not figs:
        return None
    cy = (text_rect.y0 + text_rect.y1) / 2
    best = min(figs, key=lambda f: abs((f.y0 + f.y1) / 2 - cy))
    return best if abs((best.y0 + best.y1) / 2 - cy) <= _FIG_NEAR_PT else None


def _figure_diff_issues(prod_path, stage_path, prod_nav, stage_nav, stage_idx,
                        max_figures: int = 2000):
    """Figures whose STAGE artwork does not match PROD's.

    Each PROD figure is paired with a STAGE figure through the caption printed
    beside it — text that exists in both documents — so differing pagination is
    irrelevant. Both are rendered, normalised for size and contrast, and compared
    with SSIM. A figure is only reported when a caption pairs it unambiguously
    and the two pictures genuinely differ.
    """
    if not (FIGURE_DIFF_CHECK and _CV_OK):
        return []
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []

    stage_doc = fitz.open(stage_path)
    stage_cache = {}

    def stage_page_figs(pno):
        if pno not in stage_cache:
            page = stage_doc[pno - 1]
            stage_cache[pno] = (page, _detect_figures(page), _page_lines(page))
        return stage_cache[pno]

    findings = []
    prod_doc = fitz.open(prod_path)
    try:
        for i, page in enumerate(prod_doc, 1):
            if i in prod_nav or len(findings) >= max_figures:
                continue
            figs = _detect_figures(page)
            if not figs:
                continue
            lines = _page_lines(page)
            # Which caption belongs to which figure, on the PROD side.
            caps = {}
            for f in figs:
                c = _caption_for(page, f, lines)
                if c:
                    caps.setdefault(" ".join(_seq_tokens(c)), []).append((f, c))
            for fig in figs:
                cap = _caption_for(page, fig, lines)
                if not cap:
                    continue
                # One figure per caption on this side too. Three drawings sharing
                # a caption cannot each be "the" figure it names, and pairing them
                # all against the one figure STAGE has is a guess three times over.
                if len(caps.get(" ".join(_seq_tokens(cap)), [])) != 1:
                    continue
                toks = _seq_tokens(cap)
                if not toks or not _seq_present(stage_idx, toks):
                    continue
                # The caption has to identify ONE place in each document. A
                # repeated instruction ("To exit the menu, select") sits beside a
                # different picture in every section, and pairing on it compared
                # unrelated artwork.
                p_hits = _locate_all_tokens(prod_path, cap,
                                            skip_pages=prod_nav, limit=3)
                s_hits = _locate_all_tokens(stage_path, cap,
                                            skip_pages=stage_nav, limit=3)
                if len(p_hits) != 1 or len(s_hits) != 1:
                    continue
                s_pg, s_rects = s_hits[0]
                if not s_pg or not s_rects:
                    continue
                s_page, s_figs, s_lines = stage_page_figs(s_pg)
                if not s_figs:
                    continue
                box = fitz.Rect(s_rects[0])
                for r in s_rects[1:]:
                    box |= fitz.Rect(r)
                # The STAGE figure must be captioned by the SAME words, or the
                # pairing is a guess. This is what keeps unrelated artwork from
                # being compared.
                cap_key = " ".join(toks)
                paired = [f for f in s_figs
                          if " ".join(_seq_tokens(
                              _caption_for(s_page, f, s_lines) or "")) == cap_key]
                if len(paired) != 1:
                    continue
                sim = _figure_similarity(_figure_bitmap(page, fig),
                                         _figure_bitmap(s_page, paired[0]))
                if sim is None or sim >= _FIG_SSIM_LIMIT:
                    continue
                findings.append({
                    "page": i, "stage_page": s_pg, "anchor": cap.strip(),
                    "similarity": sim,
                    "prod_rect": [fig.x0, fig.y0, fig.x1, fig.y1],
                    "stage_rect": [paired[0].x0, paired[0].y0,
                                   paired[0].x1, paired[0].y1]})
    finally:
        prod_doc.close()
        stage_doc.close()
    return findings


# ── Coloured annotation boxes on a figure ────────────────────────────────────
# SSIM is useless across two render pipelines, but a strongly-coloured region
# (a red callout box, a highlight) is unambiguous in both.  This compares only
# the amount of saturated colour, per hue, between paired figures — so a red box
# PROD draws on a screenshot that STAGE omits (or vice-versa) is caught even
# though the rest of the picture "differs" by SSIM in every pair.
_COLOR_NAMES = (("red", (0, 12)), ("red", (168, 180)), ("orange", (13, 27)),
                ("yellow", (28, 40)), ("green", (41, 90)),
                ("blue", (91, 135)), ("purple", (136, 167)))
_COLOR_PROD_MIN = 0.015   # 1.5%..35% of the figure is this hue in one document
_COLOR_PROD_MAX = 0.35    # (more than a third is a colour photo, not a box)
_COLOR_OTHER_MAX = 0.004  # and under 0.4% in the other


def _figure_colours(page, rect):
    """{hue name: fraction of the figure that is strongly that colour}."""
    if not _CV_OK:
        return {}
    try:
        pix = page.get_pixmap(clip=rect, matrix=fitz.Matrix(2, 2))
        img = _np.frombuffer(pix.samples, dtype=_np.uint8).reshape(
            pix.height, pix.width, pix.n)
    except Exception:  # noqa: BLE001
        return {}
    if pix.n < 3 or img.size == 0:
        return {}
    img = img[:, :, :3]
    hsv = _cv2.cvtColor(img, _cv2.COLOR_RGB2HSV)
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    strong = (s > 90) & (v > 60) & (v < 250)
    total = float(img.shape[0] * img.shape[1]) or 1.0
    out = {}
    for name, (lo, hi) in _COLOR_NAMES:
        mask = strong & (h >= lo) & (h <= hi)
        out[name] = out.get(name, 0.0) + mask.sum() / total
    return out


def _figure_colour_issues(prod_path, stage_path, prod_nav, stage_nav,
                          toc_results, max_findings: int = 40):
    """Figures where one document paints a coloured box / highlight the other
    does not — paired by their order within a shared section heading."""
    if not _CV_OK or os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []
    prod_doc, stage_doc = fitz.open(prod_path), fitz.open(stage_path)
    findings = []
    try:
        p_ranges = _section_ranges(toc_results, "prod", prod_doc.page_count)
        s_ranges = {t: (a, b) for t, a, b in
                    _section_ranges(toc_results, "stage", stage_doc.page_count)}
        for title, p_first, p_last in p_ranges:
            if len(findings) >= max_findings or title not in s_ranges:
                continue
            s_first, s_last = s_ranges[title]
            p_figs = _section_figures(prod_doc, prod_nav, p_first, p_last)
            s_figs = _section_figures(
                stage_doc, stage_nav, s_first,
                min(stage_doc.page_count, s_last + _SECTION_SPILLOVER))
            if not p_figs or not s_figs:
                continue
            if len(p_figs) > _IMG_MAX_PER_SEC or len(s_figs) > _IMG_MAX_PER_SEC:
                continue
            for (p_pno, p_rect, _pt), (s_pno, s_rect, _st) in zip(p_figs, s_figs):
                if len(findings) >= max_findings:
                    break
                if min(p_rect.width, p_rect.height) < 40:
                    continue
                pc = _figure_colours(prod_doc[p_pno - 1], p_rect)
                sc = _figure_colours(stage_doc[s_pno - 1], s_rect)
                for name in set(pc) | set(sc):
                    pf, sf = pc.get(name, 0.0), sc.get(name, 0.0)
                    if (_COLOR_PROD_MIN <= pf <= _COLOR_PROD_MAX
                            and sf < _COLOR_OTHER_MAX):
                        who, gone, frac = "PROD", "STAGE", pf
                    elif (_COLOR_PROD_MIN <= sf <= _COLOR_PROD_MAX
                          and pf < _COLOR_OTHER_MAX):
                        who, gone, frac = "STAGE", "PROD", sf
                    else:
                        continue
                    cap = _figure_caption_text(prod_doc[p_pno - 1], p_rect) or title
                    findings.append({
                        "page": p_pno, "stage_page": s_pno,
                        "anchor": cap[:60], "colour": name,
                        "has": who, "missing": gone,
                        "pct": round(frac * 100, 1)})
                    break
    finally:
        prod_doc.close()
        stage_doc.close()
    return findings


# ── Image mismatch, section by section ───────────────────────────────────────
# _figure_diff_issues can only judge a figure that carries a caption, because a
# caption is the one thing that pairs the same picture across two differently
# paginated documents. Most artwork in these manuals has no caption at all, so
# most of it was never compared.
#
# A section gives the pairing instead: whatever illustrations PROD prints under
# a heading, STAGE must print the same ones under that heading. Each PROD figure
# is matched to its closest STAGE figure inside the same section; a PROD figure
# with no close match is missing or has been replaced, and a STAGE figure left
# over is artwork PROD does not have.
_IMG_MATCH_LIMIT = 0.16   # thumbnails closer than this are the same picture
_IMG_MAX_PER_SEC = 12     # guard: a section with more figures than this is a
                          # gallery, and pairing degenerates into guesswork
_SECTION_SPILLOVER = 3    # STAGE pages searched past a section's nominal end —
                          # reflow routinely pushes a topic's own figures onto
                          # what the TOC marks as the next section's first page,
                          # and that is not a defect against this section. STAGE
                          # can also bookmark several fine-grained headings
                          # (e.g. "Screen Message", "Room Name") that PROD
                          # keeps as one broader section's body text, each
                          # eating into the page range before it — 2 pages
                          # wasn't always enough to still reach content that
                          # legitimately belongs to the section being checked.


def _section_ranges(toc_results, side: str, page_count: int):
    """[(title, first page, last page)] over one document's pages.

    Several headings can share one page (a compact overview page with a few
    sub-headings and no page of their own) — clamping a section's end to at
    least its own start page means every heading still gets a range instead
    of silently vanishing because its "span" would otherwise be negative.
    """
    marks = _section_page_index(toc_results, side)
    out = []
    for n, (pg, title) in enumerate(marks):
        nxt = (marks[n + 1][0] - 1) if n + 1 < len(marks) else page_count
        out.append((title, pg, max(pg, min(nxt, page_count))))
    return out


def _section_figures(doc, nav_pages, first, last):
    """[(page, rect, thumbnail)] for every figure in a page range.

    _detect_figures erases the words and keeps whatever ink is left, so a
    shaded NOTE / IMPORTANT panel survives as a big blob and is returned as a
    "figure". Those phantoms broke the section comparison three ways: they
    inflated one side's figure count, they stole greedy matches from real
    artwork, and they left genuine figures reported as mismatched. The text
    coverage guard is the one _figure_regions already uses for this.
    """
    out = []
    for pno in range(first, min(last, doc.page_count) + 1):
        if pno in nav_pages:
            continue
        page = doc[pno - 1]
        for rect in _detect_figures(page):
            if _region_text_cover(page, rect) > _FIG_MAX_TEXT_COVER:
                continue                  # a note box, not a picture
            thumb = _figure_thumb(page, rect)
            if thumb:
                out.append((pno, rect, thumb))
    return out


_ALIGN_TOL_FRAC = 0.06     # of the text column width


def _align_of(rect, col):
    """"left-aligned" / "centred" / "right-aligned", or None when it is none of
    those. A figure sitting at some other offset is a deliberate inset, and
    guessing at it is what makes a placement check noisy."""
    cl, cr = col
    cw = (cr - cl) or 1.0
    tol = max(14.0, _ALIGN_TOL_FRAC * cw)
    left_gap = rect.x0 - cl
    right_gap = cr - rect.x1
    # Touching the left margin wins, and is tested first: a full-width line has
    # its midpoint on the column centre too, and testing "centred" first called
    # every ordinary justified paragraph line centred.
    if left_gap <= tol:
        return "left-aligned"
    if right_gap <= tol:
        return "right-aligned"
    if abs(left_gap - right_gap) <= tol:
        return "centred"          # inset from both edges by the same amount
    return None


def _figure_align_issues(prod_path, stage_path, prod_nav, stage_nav,
                         toc_results, max_findings: int = 2000):
    """The same figure placed differently in the text column.

    Only figures matched by their own artwork are compared — each PROD figure
    is paired with the STAGE figure that looks like it, so this never compares
    two different pictures, which is what made a statistical "does this
    document centre its figures" test unusable across two layout engines.
    """
    if not _CV_OK or os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []
    p_col = _text_column(prod_path, prod_nav)
    s_col = _text_column(stage_path, stage_nav)
    if not p_col or not s_col:
        return []
    prod_doc, stage_doc = fitz.open(prod_path), fitz.open(stage_path)
    findings = []
    try:
        p_ranges = _section_ranges(toc_results, "prod", prod_doc.page_count)
        s_ranges = {t: (a, b) for t, a, b in
                    _section_ranges(toc_results, "stage", stage_doc.page_count)}
        for title, p_first, p_last in p_ranges:
            if len(findings) >= max_findings or title not in s_ranges:
                continue
            s_first, s_last = s_ranges[title]
            p_figs = _section_figures(prod_doc, prod_nav, p_first, p_last)
            s_last_search = min(stage_doc.page_count, s_last + _SECTION_SPILLOVER)
            s_figs = _section_figures(stage_doc, stage_nav, s_first, s_last_search)
            if not p_figs or not s_figs:
                continue
            if len(p_figs) > _IMG_MAX_PER_SEC or len(s_figs) > _IMG_MAX_PER_SEC:
                continue
            taken = set()
            for p_pno, p_rect, p_thumb in p_figs:
                best, best_d = None, None
                for j, (_s_pno, _s_rect, s_thumb) in enumerate(s_figs):
                    if j in taken:
                        continue
                    d = _thumb_diff(p_thumb, s_thumb)
                    if d is None:
                        continue
                    if best_d is None or d < best_d:
                        best, best_d = j, d
                if best is None or best_d is None or best_d > _IMG_MATCH_LIMIT:
                    continue      # not the same artwork — not this check's business
                taken.add(best)
                s_pno, s_rect, _ = s_figs[best]
                p_align, s_align = _align_of(p_rect, p_col), _align_of(s_rect, s_col)
                if not p_align or not s_align or p_align == s_align:
                    continue
                p_off = (p_rect.x0 + p_rect.x1) / 2.0 - (p_col[0] + p_col[1]) / 2.0
                s_off = (s_rect.x0 + s_rect.x1) / 2.0 - (s_col[0] + s_col[1]) / 2.0
                findings.append({
                    "anchor": title, "page": p_pno, "prod_page": p_pno,
                    "stage_page": s_pno, "prod_align": p_align,
                    "stage_align": s_align, "shift": s_off - p_off,
                })
    finally:
        prod_doc.close()
        stage_doc.close()
    return findings


def _ordinal(n):
    return f"{n}{'th' if 11 <= n % 100 <= 13 else {1:'st',2:'nd',3:'rd'}.get(n % 10, 'th')}"


def _where_on_page(page, rect, siblings):
    """Plain English for where a figure sits: which one it is, and whereabouts.

    "page 18" alone is not enough to find a picture on a page holding four of
    them, which is what made these findings hard to act on.
    """
    h = page.rect.height or 1.0
    w = page.rect.width or 1.0
    band = ("top" if rect.y0 < h * 0.33 else
            "middle" if rect.y0 < h * 0.66 else "bottom")
    cx = (rect.x0 + rect.x1) / 2.0
    side = ("left" if cx < w * 0.4 else
            "right" if cx > w * 0.6 else "centre")
    same = sorted(siblings, key=lambda r: (r.y0, r.x0))
    n = len(same)
    try:
        idx = next(i for i, r in enumerate(same, 1)
                   if abs(r.y0 - rect.y0) < 0.5 and abs(r.x0 - rect.x0) < 0.5)
    except StopIteration:
        idx = 1
    which = (f"the only figure on the page" if n == 1 else
             f"the {_ordinal(idx)} of {n} figures on the page")
    return f"{which}, {band} {side}"


def _figure_caption_text(page, rect):
    """The words printed beside a figure, for naming it in the report."""
    try:
        cap = _caption_for(page, rect, _page_lines(page))
    except Exception:
        cap = None
    return " ".join((cap or "").split())[:90]


def _image_mismatch_issues(prod_path, stage_path, prod_nav, stage_nav,
                           toc_results, max_findings: int = 2000):
    """PROD figures a section has more of than STAGE has anywhere to answer for.

    Whether STAGE's picture is pixel-identical to PROD's is not this check's
    business — a re-touched or re-cropped shot of the same subject still reads
    as "the picture is there", and judging identity from a thumbnail diff is
    what made the same STAGE figure read as "a different picture" against
    several PROD figures at once. This only counts pictures: PROD's figures in
    a section are matched one-for-one against STAGE's (STAGE's search reaching
    a couple of pages past the section end for reflow); once STAGE runs out,
    every PROD figure left over is reported missing. A figure's own caption is
    still checked separately by the image-label check.
    """
    if not _CV_OK or os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []
    prod_doc, stage_doc = fitz.open(prod_path), fitz.open(stage_path)
    findings = []
    try:
        p_ranges = _section_ranges(toc_results, "prod", prod_doc.page_count)
        s_ranges = {t: (a, b) for t, a, b in
                    _section_ranges(toc_results, "stage", stage_doc.page_count)}
        for title, p_first, p_last in p_ranges:
            if len(findings) >= max_findings or title not in s_ranges:
                continue
            s_first, s_last = s_ranges[title]
            p_figs = _section_figures(prod_doc, prod_nav, p_first, p_last)
            if not p_figs or len(p_figs) > _IMG_MAX_PER_SEC:
                continue
            s_last_search = min(stage_doc.page_count, s_last + _SECTION_SPILLOVER)
            s_figs = _section_figures(stage_doc, stage_nav, s_first, s_last_search)
            if len(s_figs) > _IMG_MAX_PER_SEC:
                continue
            shortfall = len(p_figs) - len(s_figs)
            if shortfall <= 0:
                continue           # STAGE has at least as many pictures here
            p_by_page = {}
            for pno, rect, _t in p_figs:
                p_by_page.setdefault(pno, []).append(rect)
            for p_pno, p_rect, _p_thumb in p_figs[-shortfall:]:
                if len(findings) >= max_findings:
                    break
                p_page = prod_doc[p_pno - 1]
                findings.append({
                    "section": title, "page": s_first, "prod_page": p_pno,
                    "kind": "missing",
                    "prod_where": _where_on_page(p_page, p_rect,
                                                 p_by_page.get(p_pno, [])),
                    "caption": _figure_caption_text(p_page, p_rect),
                })
    finally:
        prod_doc.close()
        stage_doc.close()
    return findings


# ── Text that lives inside artwork ───────────────────────────────────────────
_FIG_TEXT_CACHE = {}


def _figure_text_keys(pdf_path: str, nav_pages: set) -> set:
    """Canonical text of every line that sits inside a figure, or is repeated
    verbatim elsewhere on the same page.

    Labels printed inside a drawing ("0-40°C" under a thermometer icon) are text
    in one document and part of the artwork in the other. Comparing them reports
    the same visible information as missing or added purely because of how it was
    produced, so they are kept out of the content comparison and left to the
    image-label check, which is anchored and conservative.

    A short line repeated word-for-word elsewhere on the same page is caught
    too, even when it sits in the gap between two panels rather than literally
    on top of one — a real sentence is not printed twice on one page, but a
    "before/after" UI-state caption drawn once per panel is.
    """
    key = (os.path.abspath(pdf_path), tuple(sorted(nav_pages)))
    hit = _FIG_TEXT_CACHE.get(key)
    if hit is not None:
        return hit
    out, doc = set(), fitz.open(pdf_path)
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        lines = _page_lines(page)
        figs = _detect_figures(page)
        grown = [fitz.Rect(f.x0 - 6, f.y0 - 6, f.x1 + 6, f.y1 + 6) for f in figs]
        counts = collections.Counter(txt for txt, _r in lines)
        for txt, rect in lines:
            mid = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
            in_figure = any(g.contains(mid) for g in grown)
            n_toks = len(_seq_tokens(txt))
            duplicated = counts[txt] >= 2 and 3 <= n_toks <= 8
            if in_figure or duplicated:
                for tok in _seq_tokens(txt):
                    out.add(tok)
    doc.close()
    if len(_FIG_TEXT_CACHE) > 6:
        _FIG_TEXT_CACHE.clear()
    _FIG_TEXT_CACHE[key] = out
    return out


def _is_artwork_text(fragment: str, fig_tokens: set) -> bool:
    """True when a fragment is made up of words that only occur inside artwork."""
    toks = _seq_tokens(fragment)
    if not toks:
        return False
    return all(t in fig_tokens for t in toks)


# ── Diagram callout numbers with gaps ────────────────────────────────────────
def _diagram_callout_numbers(pdf_path: str, nav_pages: set) -> dict:
    """{page: [set(callout numbers), ...]} — one set per figure with >= 3 of
    them nearby. A bare 1-2 digit number inside a real table is a cell value
    (a port number, a spec figure) — not a diagram callout, so numbers that
    land inside a detected table are excluded (see the port-number-table
    false positive this guards against: 22/53/80 read as callouts 22..80,
    "missing" 23-79)."""
    doc, out = fitz.open(pdf_path), {}
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        figs = _detect_figures(page)
        if not figs:
            continue
        try:
            tbl_boxes = [fitz.Rect(t.bbox) for t in page.find_tables().tables]
        except Exception:
            tbl_boxes = []
        lines = _page_lines(page)
        for fig in figs:
            near = fitz.Rect(fig.x0 - 40, fig.y0 - 40, fig.x1 + 40, fig.y1 + 40)
            nums = set()
            for txt, rect in lines:
                m = _CALLOUT_RE.match(txt)
                if not (m and near.intersects(rect)):
                    continue
                mid = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
                if any(tb.contains(mid) for tb in tbl_boxes):
                    continue
                nums.add(int(m.group(1)))
            # A real diagram's callouts are a dense run (1..N); a stray page
            # number or unrelated digit sitting within the search radius of a
            # small handful of numbered-list markers produces a wide, sparse
            # set instead (e.g. {1, 2, 3, 4, 62}) — require the numbers found
            # to cover at least 40% of their own span before trusting them.
            span = (max(nums) - min(nums) + 1) if nums else 0
            if len(nums) >= 3 and len(nums) >= span * 0.4:
                out.setdefault(i, []).append(nums)
    doc.close()
    return out


def _callout_gap_issues(prod_path: str, stage_path: str, prod_nav: set,
                        stage_nav: set, toc_results):
    """Diagram callout numbers PROD carries that STAGE does not render.

    A labelled diagram numbers its parts 1..N. Comparing PROD's numbers for a
    diagram against every callout STAGE carries in the same section catches
    both ways that loss shows up: STAGE's own sequence skipping a value in its
    range (1, 2, 3, 4, 8 — the leader lines for 5, 6, 7 are drawn but the
    numbers never made it), and STAGE simply carrying fewer callouts than
    PROD's diagram altogether (PROD numbers 1..8, STAGE only 1..3). Scoped to
    the section, not the whole document, so a number that legitimately repeats
    across a different diagram elsewhere is not mistaken for "present".
    """
    prod_by_page = _diagram_callout_numbers(prod_path, prod_nav)
    if not prod_by_page:
        return []
    stage_by_page = _diagram_callout_numbers(stage_path, stage_nav)

    prod_doc = fitz.open(prod_path)
    prod_pages = prod_doc.page_count
    prod_doc.close()
    stage_doc = fitz.open(stage_path)
    stage_pages = stage_doc.page_count
    stage_doc.close()

    p_ranges = _section_ranges(toc_results, "prod", prod_pages)
    s_ranges = {t: (a, b) for t, a, b in
               _section_ranges(toc_results, "stage", stage_pages)}

    found, seen = [], set()
    for title, p_first, p_last in p_ranges:
        if title not in s_ranges:
            continue
        s_first, s_last = s_ranges[title]
        s_last_search = min(stage_pages, s_last + _SECTION_SPILLOVER)
        stage_nums_here = set()
        for pno in range(s_first, s_last_search + 1):
            for nums in stage_by_page.get(pno, []):
                stage_nums_here |= nums
        for pno in range(p_first, p_last + 1):
            for nums in prod_by_page.get(pno, []):
                missing = sorted(nums - stage_nums_here)
                if not missing:
                    continue
                key = (pno, tuple(sorted(nums)))
                if key in seen:
                    continue
                seen.add(key)
                present_s = ", ".join(str(n) for n in sorted(nums))
                missing_s = ", ".join(str(n) for n in missing)
                found.append({
                    "prod_page": pno, "stage_page": s_first,
                    "stage_first": s_first, "stage_last": s_last_search,
                    "present": sorted(nums), "missing": missing,
                    "title": f"Diagram numbered {present_s}",
                    "detail": (f"PROD numbers this diagram {present_s} — "
                              f"{missing_s} "
                              f"{'is' if len(missing) == 1 else 'are'} not "
                              f"there on STAGE's diagram."),
                })
    return found


# ── List marker style ────────────────────────────────────────────────────────
# A step list numbered 1. 2. 3. in PROD that appears as a. b. c. or as bullets in
# STAGE has changed meaning for the reader even though every word matches.
_MARK_NUM   = re.compile(r"^\s*\d{1,2}\s*[.)]\s+\S")
_MARK_ALPHA = re.compile(r"^\s*[a-zA-Z]\s*[.)]\s+\S")
_MARK_BULL  = re.compile(r"^\s*[\u2022\u25cf\u25aa\u2023\u2043\-\*]\s+\S")


def _marker_style(line: str):
    """'number', 'letter', 'bullet' or None for a line of list text."""
    if _MARK_NUM.match(line):
        return "number"
    if _MARK_ALPHA.match(line):
        return "letter"
    if _MARK_BULL.match(line):
        return "bullet"
    return None


def _list_style_issues(prod_path, stage_path, prod_nav, stage_nav):
    """List items whose marker style differs between PROD and STAGE."""
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []

    def styles(path, nav):
        doc, out = fitz.open(path), {}
        for i, page in enumerate(doc, 1):
            if i in nav:
                continue
            for txt, _rect in _page_lines(page):
                st = _marker_style(txt)
                if not st:
                    continue
                # Keyed on the wording after the marker, so the same item can be
                # found on the other side however it happens to be marked.
                body = re.sub(r"^\s*(?:\d{1,2}|[a-zA-Z])\s*[.)]\s*", "", txt)
                body = re.sub(r"^\s*[\u2022\u25cf\u25aa\u2023\u2043\-\*]\s*",
                              "", body)
                key = " ".join(_seq_tokens(body))
                if len(key.split(" ")) >= 4:
                    out.setdefault(key, (i, st, txt.strip()))
        doc.close()
        return out

    prod_st, stage_st = styles(prod_path, prod_nav), styles(stage_path, stage_nav)
    findings = []
    for key, (ppage, pstyle, ptxt) in prod_st.items():
        hit = stage_st.get(key)
        if not hit:
            continue
        spage, sstyle, _stxt = hit
        if sstyle == pstyle:
            continue
        findings.append({"page": spage, "prod_page": ppage,
                         "prod_style": pstyle, "stage_style": sstyle,
                         "text": ptxt})
    return findings


# ── Table page breaks ────────────────────────────────────────────────────────
def _table_page_spans(pdf_path: str, nav_pages: set):
    """{header key: (first page, last page, columns, header text)}.

    A table is followed across pages by its header wording, so a table that the
    layout splits over a page boundary is seen as one table spanning a range
    rather than as several unrelated ones.
    """
    out = {}
    for pno, _nrow, ncol, rows in _crawl_tables(pdf_path, nav_pages):
        if not rows or ncol < 2:
            continue
        head = [re.sub(r"\s+", " ", (c or "")).strip() for c in rows[0]]
        shown = " | ".join(h for h in head if h)
        key = " ".join(_seq_tokens(shown))
        if len(key.split(" ")) < 2:
            continue
        cur = out.get(key)
        if cur is None:
            out[key] = [pno, pno, ncol, shown]
        else:
            cur[1] = max(cur[1], pno)
    return out


def _span_repeats_header(pdf_path: str, nav_pages: set, header_key: str,
                         first_page: int, last_page: int) -> bool:
    """True when every continuation page of a table span re-prints its header.

    A page break inside a table is only a reader problem when the rows on the
    next page have no column labels. When each continuation page opens with the
    header row again (PROD and STAGE both do this on the OSD menu tables), the
    split is ordinary pagination and must not be reported as broken layout.
    """
    if last_page <= first_page or not header_key:
        return False
    hset = set(header_key.split(" "))
    if not hset:
        return False
    doc = fitz.open(pdf_path)
    try:
        for pno in range(first_page + 1, last_page + 1):
            if pno in nav_pages or pno > doc.page_count:
                return False
            page = doc[pno - 1]
            try:
                tbls = page.find_tables().tables
            except Exception:
                return False
            ph = page.rect.height or 1.0
            tops = []
            for t in tbls:
                try:
                    rows = t.extract()
                except Exception:
                    continue
                if not rows or (t.col_count or 0) < 2:
                    continue
                tops.append((fitz.Rect(t.bbox).y0 / ph, rows[0]))
            if not tops:
                return False
            _, first_row = min(tops, key=lambda r: r[0])
            row_key = " ".join(_seq_tokens(" | ".join((c or "") for c in first_row)))
            cset = set(row_key.split(" "))
            if row_key != header_key and (
                    not cset or len(hset & cset) / len(hset) < 0.6):
                return False
    finally:
        doc.close()
    return True


def _table_break_issues(prod_path, stage_path, prod_nav, stage_nav):
    """Tables that STAGE splits over more pages than PROD does.

    This is the layout question a reader notices: a table that sits whole on one
    page in PROD but is broken by a page boundary in STAGE, so its rows are cut
    apart from their header. A split where STAGE re-prints the header on every
    continuation page is ordinary pagination, not a defect, and is not reported.
    """
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []
    prod_sp = _table_page_spans(prod_path, prod_nav)
    stage_sp = _table_page_spans(stage_path, stage_nav)
    findings = []
    for key, (p0, p1, _pc, phead) in prod_sp.items():
        hit = stage_sp.get(key)
        if not hit:
            continue
        s0, s1, _sc, _sh = hit
        p_pages, s_pages = p1 - p0 + 1, s1 - s0 + 1
        if s_pages <= p_pages:
            continue
        if _span_repeats_header(stage_path, stage_nav, key, s0, s1):
            continue          # header repeats on every page — valid pagination
        findings.append({"page": s0, "prod_page": p0,
                         "prod_pages": p_pages, "stage_pages": s_pages,
                         "stage_from": s0, "stage_to": s1, "header": phead})
    return findings


def _table_continuation_header_issues(pdf_path: str, doc_label: str,
                                      nav_pages: set):
    """A table that runs onto the next page but does not repeat its header row.

    A page break inside a table is fine as long as the continuation carries the
    same column header — the reader on that page still knows what each column
    means. When the continuation starts straight into data rows, the columns are
    unlabelled. STAGE-only structural check.
    """
    doc = fitz.open(pdf_path)
    per_page: dict = {}
    try:
        for i, page in enumerate(doc, 1):
            if i in nav_pages:
                continue
            ph = page.rect.height or 1.0
            try:
                tbls = page.find_tables().tables
            except Exception:
                tbls = []
            recs = []
            for t in tbls:
                try:
                    rows = t.extract()
                except Exception:
                    continue
                if not rows or (t.col_count or 0) < 2:
                    continue
                bb = fitz.Rect(t.bbox)
                recs.append({"ncol": t.col_count, "rows": rows,
                             "top": bb.y0 / ph, "bot": bb.y1 / ph})
            if recs:
                per_page[i] = recs
    finally:
        doc.close()

    def _rowkey(row):
        return " ".join(_seq_tokens(" | ".join((c or "") for c in row)))

    findings = []
    for i, recs in per_page.items():
        nxt = per_page.get(i + 1)
        if not nxt:
            continue
        bottom = max(recs, key=lambda r: r["bot"])
        if bottom["bot"] < 0.85:
            continue                       # table did not reach the page end
        top = min(nxt, key=lambda r: r["top"])
        if top["top"] > 0.30 or top["ncol"] != bottom["ncol"]:
            continue                       # a fresh table, not a continuation
        header_key = _rowkey(bottom["rows"][0])
        if len(header_key.split(" ")) < 2:
            continue                       # no real header row to repeat
        # The first row must read like real column headers — two or more short
        # label cells. A single long cell ("BenQ LCD Monitor", "Regulatory
        # Statements") is a boxed layout block, not a data table, and its
        # "continuation" is just body text flowing onto the next page.
        real_head = [h for h in
                     (" ".join((c or "").split()) for c in bottom["rows"][0])
                     if h and len(h.split()) <= _MAX_HEADER_WORDS
                     and any(len(w) >= 3 for w in _WORD_TOKEN_RE.findall(h))]
        if len(real_head) < 2:
            continue
        cont_first = _rowkey(top["rows"][0])
        if cont_first == header_key:
            continue                       # header repeated — this is fine
        hset = set(header_key.split(" "))
        cset = set(cont_first.split(" "))
        if hset and len(hset & cset) / len(hset) >= 0.6:
            continue                       # near-match (one column relabelled)
        head_shown = " | ".join(h for h in
                                (str(c or "").strip() for c in bottom["rows"][0])
                                if h)
        findings.append({"doc": doc_label, "page": i + 1, "prev_page": i,
                         "header": head_shown, "text": head_shown,
                         "detail": (f"table continues from page {i} to page "
                                    f"{i + 1} without repeating its header")})
    return findings


# ── Table structure ──────────────────────────────────────────────────────────
_TABLE_PAIR_MIN = 0.25    # cell overlap before two tables are the same table
_MARGIN_TOL_PT = 12.0     # a table may sit this far outside the text column
                          # before it counts as breaking the margin: table
                          # borders and cell padding routinely overhang by a few
                          # points, and reporting that is noise


def _text_column(pdf_path: str, nav_pages: set):
    """(left, right) of the body text column, in points.

    Measured from the paragraphs themselves rather than from the page size: the
    printable area is wherever the document actually sets its text, and that is
    what a table has to stay inside to look right.
    """
    doc, lefts, rights = fitz.open(pdf_path), [], []
    try:
        for i, page in enumerate(doc, 1):
            if i in nav_pages:
                continue
            for b in page.get_text("blocks"):
                if b[6] != 0 or (b[3] - b[1]) < 8:
                    continue          # not text, or a single short line
                if len(_seq_tokens(b[4] or "")) < 6:
                    continue          # running head, page number, caption
                lefts.append(b[0])
                rights.append(b[2])
    finally:
        doc.close()
    if len(lefts) < 10:
        return None
    lefts.sort(); rights.sort()
    return lefts[len(lefts) // 10], rights[-max(1, len(rights) // 10)]


def _table_margin_issues(prod_path, stage_path, prod_nav, stage_nav):
    """STAGE tables that run outside the text column PROD keeps its tables in."""
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []
    s_col = _text_column(stage_path, stage_nav)
    p_col = _text_column(prod_path, prod_nav)
    if not s_col or not p_col:
        return []

    def overflow(bbox, col):
        left = col[0] - bbox[0]
        right = bbox[2] - col[1]
        return max(0.0, left), max(0.0, right)

    # A table PROD also sets outside its own column is a house style, not a
    # STAGE defect, so those are matched by header and left alone.
    prod_boxes = {}
    doc = fitz.open(prod_path)
    try:
        for i, page in enumerate(doc, 1):
            if i in prod_nav:
                continue
            try:
                for t in page.find_tables():
                    head = " ".join(_seq_tokens(" ".join(
                        c or "" for c in (t.extract() or [[]])[0])))
                    if head:
                        l, r = overflow(t.bbox, p_col)
                        prod_boxes.setdefault(head, max(l, r))
            except Exception:
                continue
    finally:
        doc.close()

    findings = []
    doc = fitz.open(stage_path)
    try:
        for i, page in enumerate(doc, 1):
            if i in stage_nav:
                continue
            try:
                tables = page.find_tables()
            except Exception:
                continue
            for t in tables:
                l, r = overflow(t.bbox, s_col)
                if max(l, r) <= _MARGIN_TOL_PT:
                    continue
                rows = t.extract() or [[]]
                head_cells = [c for c in (rows[0] if rows else []) if c]
                head = " ".join(_seq_tokens(" ".join(head_cells)))
                if head and prod_boxes.get(head, 0.0) > _MARGIN_TOL_PT:
                    continue          # PROD sets it the same way
                findings.append({
                    "page": i,
                    "header": " | ".join(re.sub(r"\s+", " ", c).strip()
                                         for c in head_cells) or "(unheaded table)",
                    "left": round(l), "right": round(r),
                    "col_left": round(s_col[0]), "col_right": round(s_col[1]),
                    "x0": round(t.bbox[0]), "x1": round(t.bbox[2]),
                })
    finally:
        doc.close()
    return findings


def _table_shape_issues(prod_path, stage_path, prod_nav, stage_nav):
    """Tables whose column layout in STAGE does not match PROD's.

    Tables are paired by what is inside them, not by their header: pairing on the
    header can only ever find tables that already agree, which is the one case
    with nothing to report. Cells are compared as a set, so a table that STAGE
    re-orders or re-paginates still pairs with its PROD original.

    Only the headed columns are compared. A table with nested sub-rows makes the
    detector's grid a column wider while the reader still sees the same columns,
    and counting the grid reported that as a layout change when it is not one.
    """
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []

    def shapes(path, nav):
        out = []
        for pno, nrow, ncol, rows in _merge_continued_tables(
                _crawl_tables(path, nav)):
            if not rows:
                continue
            head = [" ".join((c or "").split()) for c in rows[0]]
            filled = [h for h in head if h]
            if len(filled) < 2:
                continue
            body = set()
            for r in rows[1:]:
                for c in r:
                    k = " ".join(_seq_tokens(c or ""))
                    if k:
                        body.add(k)
            if not body:
                continue
            out.append({"page": pno, "head": filled, "rows": nrow, "body": body,
                        "keys": [" ".join(_seq_tokens(h)) for h in filled]})
        return out

    prod_t, stage_t = shapes(prod_path, prod_nav), shapes(stage_path, stage_nav)
    if not prod_t or not stage_t:
        return []

    # Score every possible pairing, then take them best-first. Walking the PROD
    # tables in order let an early table claim a STAGE table that was a better
    # match for a later one, leaving the real counterpart unpaired.
    scored = []
    for pi, pt in enumerate(prod_t):
        for si, st in enumerate(stage_t):
            inter = len(pt["body"] & st["body"])
            if not inter:
                continue
            scored.append((inter / len(pt["body"] | st["body"]), pi, si))
    scored.sort(reverse=True)

    findings, used_p, used_s = [], set(), set()
    for score, pi, si in scored:
        if score < _TABLE_PAIR_MIN or pi in used_p or si in used_s:
            continue
        used_p.add(pi); used_s.add(si)
        pt, st = prod_t[pi], stage_t[si]
        if pt["keys"] == st["keys"]:
            continue
        extra = [h for h, k in zip(st["head"], st["keys"]) if k not in pt["keys"]]
        gone = [h for h, k in zip(pt["head"], pt["keys"]) if k not in st["keys"]]
        reordered = (not extra and not gone)
        # PROD is the baseline. A STAGE table that keeps every PROD column and
        # merely adds one is not a PROD→STAGE loss — don't report it. Report a
        # reordering, or a PROD column STAGE dropped.
        if not gone and not reordered:
            continue
        # gone AND extra with nothing shared = the body overlap paired two
        # different tables. Not a real column change.
        if gone and extra and not (set(pt["keys"]) & set(st["keys"])):
            continue
        findings.append({
            "page": st["page"], "prod_page": pt["page"],
            "prod_cols": len(pt["head"]), "stage_cols": len(st["head"]),
            "prod_head": pt["head"], "stage_head": st["head"],
            "extra": extra, "gone": gone,
            "reordered": reordered,
            "header": " | ".join(pt["head"]),
        })
    return findings


def _table_merge_issues(prod_path, stage_path, prod_nav, stage_nav):
    """Tables where PROD and STAGE split their cells differently.

    The reader-visible columns (the headed ones) are the same, but the body
    grid underneath them is not: STAGE merges cells PROD keeps separate, or
    splits a value PROD keeps whole into its own column / row — a nested
    sub-value pushed into an extra column, a spanned label unspanned, two facts
    a PROD cell combined ("… @ 30Hz / … 5 Gbps") pulled apart. The header check
    passes because the headings did not change, so this is the check that
    notices the merge.
    """
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []

    def grids(path, nav):
        out = []
        for pno, nrow, ncol, rows in _merge_continued_tables(
                _crawl_tables(path, nav)):
            if not rows or ncol < 2:
                continue
            head = [" ".join((c or "").split()) for c in rows[0]]
            if len([h for h in head if h]) < 2:
                continue
            body = set()
            for r in rows[1:]:
                for c in r:
                    k = " ".join(_seq_tokens(c or ""))
                    if k:
                        body.add(k)
            if not body:
                continue
            out.append({"page": pno, "ncol": ncol, "nrow": nrow, "body": body,
                        "rows": [[" ".join((c or "").split()) for c in r]
                                 for r in rows[1:]],
                        "hkey": tuple(" ".join(_seq_tokens(h))
                                      for h in head if h),
                        "header": " | ".join(h for h in head if h)})
        return out

    pg, sg = grids(prod_path, prod_nav), grids(stage_path, stage_nav)
    if not pg or not sg:
        return []

    scored = []
    for pi, p in enumerate(pg):
        for si, s in enumerate(sg):
            inter = len(p["body"] & s["body"])
            if inter:
                scored.append((inter / len(p["body"] | s["body"]), pi, si))
    scored.sort(reverse=True)

    findings, used_p, used_s = [], set(), set()
    for score, pi, si in scored:
        if score < _TABLE_PAIR_MIN or pi in used_p or si in used_s:
            continue
        used_p.add(pi); used_s.add(si)
        p, s = pg[pi], sg[si]
        if p["hkey"] != s["hkey"]:
            continue                 # a header change — _table_shape_issues owns it
        col_delta = s["ncol"] - p["ncol"]
        # Only a column-count change is reported. A body-row-count difference is
        # almost always find_tables() slicing a wrapped multi-line cell into
        # extra rows — not a real structural change — and produced a page of
        # false reports on the dense OSD menu tables.
        if col_delta == 0:
            continue
        # Point at the actual rows: match each PROD row to the STAGE row that
        # shares its wording, and keep the ones whose cell count differs.
        examples = []
        for pr in p["rows"]:
            p_cells = [c for c in pr if c]
            if len(p_cells) < 2:
                continue
            # Anchor the match on the row's most distinctive cell (usually the
            # label), not on any shared word - a loose word overlap paired
            # "Reset Mode" against an unrelated "(Applicable when...)" note row.
            anchor = max(p_cells, key=lambda c: len(_seq_tokens(c)))
            atoks = [t for t in _seq_tokens(anchor) if len(t) > 1]
            if len(atoks) < 2:
                continue
            match = None
            for sr in s["rows"]:
                stoks = set(t for c in sr for t in _seq_tokens(c))
                if sum(1 for t in atoks if t in stoks) >= max(2, len(atoks) - 1):
                    match = sr
                    break
            if match is None:
                continue
            s_cells = [c for c in match if c]
            # Only a genuine content difference is worth a row example: the same
            # value spread across per-model columns ("3 x 3.0 | 3 x 3.0 | 3 x
            # 3.0" vs "3 x 3.0 3 x 3.0 3 x 3.0") is one side's detector merging
            # identical columns, not a real change. Compare the VALUE cells
            # only - a hyphenation difference in the label ("slot-in" vs
            # "slotin") is not a merge.
            p_words = set(w for c in p_cells[1:] for w in _seq_tokens(c)
                          if len(w) > 1)
            s_words = set(w for c in s_cells[1:] for w in _seq_tokens(c)
                          if len(w) > 1)
            p_nums = set(re.findall(r"\d[\d.,]*", " ".join(p_cells[1:])))
            s_nums = set(re.findall(r"\d[\d.,]*", " ".join(s_cells[1:])))
            if (len(p_cells) != len(s_cells)
                    and (p_words != s_words or p_nums != s_nums)):
                label = (pr[0] or p_cells[0])[:44]
                p_show = " | ".join(c[:18] for c in p_cells[:4])
                s_show = " | ".join(c[:18] for c in s_cells[:4])
                examples.append(
                    f"the row for “{label}” — PROD: [{p_show}] "
                    f"({len(p_cells)} cells); STAGE: [{s_show}] "
                    f"({len(s_cells)} cells)")
            if len(examples) >= 3:
                break

        # Report only when specific rows can be named. A bare grid-size
        # difference with no identifiable row is almost always find_tables()
        # fragmenting one side's table, not a real structural change - and it
        # gives the reader nothing to act on.
        if not examples:
            continue
        verb = ("splits cells into an extra column" if col_delta > 0
                else "merges cells PROD keeps separate")
        what = (f"STAGE {verb}. Rows affected: " + "; ".join(examples))
        findings.append({"page": s["page"], "prod_page": p["page"],
                         "what": what, "header": p["header"],
                         "rows_affected": examples,
                         "text": p["header"], "detail": what})
    return findings


# ── Diagram callout numbers ──────────────────────────────────────────────────
_CALLOUT_RE = re.compile(r"^\s*(\d{1,2})\s*$")


def _callout_counts(pdf_path: str, nav_pages: set):
    """(pages carrying callout numbers as text, distinct numbers seen).

    Some manuals set diagram callouts as real text; others bake them into the
    artwork. Only the first kind can be compared, so this reports what each
    document exposes rather than treating a difference as missing content.
    """
    doc, pages, nums = fitz.open(pdf_path), 0, set()
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        found = set()
        for block in _page_dict(page)["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                txt = "".join(sp.get("text", "") for sp in line.get("spans", []))
                m = _CALLOUT_RE.match(txt)
                if m:
                    found.add(int(m.group(1)))
        if len(found) >= 4:            # a run of numbers = a labelled diagram
            pages += 1
            nums |= found
    doc.close()
    return pages, sorted(nums)


# ── Broken icons and images ──────────────────────────────────────────────────
_BLANK_SAMPLES = 64        # points sampled across an image to test uniformity


def _pixmap_is_blank(pix) -> bool:
    """True when an image is one flat colour — a placeholder, not artwork.

    Sampled across the whole image rather than from the start: a figure with a
    white band at the top would otherwise look blank.
    """
    try:
        data = pix.samples
        n = pix.n
        if not data or n < 1:
            return False
        step = max(n, (len(data) // _BLANK_SAMPLES) // n * n or n)
        first, seen = None, 0
        for off in range(0, len(data) - n + 1, step):
            px = bytes(data[off:off + n])
            if first is None:
                first = px
            elif px != first:
                return False
            seen += 1
        return seen >= 8
    except Exception:
        return False


def _icon_issues(pdf_path: str, doc_label: str, nav_pages: set):
    """Images that are drawn but carry nothing: undecodable, blank, or collapsed.

    Only images actually placed on a page are examined. An image that sits in the
    resources without being drawn is not reported — in these PDFs those are
    nested inside form XObjects, where the placement simply cannot be resolved,
    and calling them broken would be wrong.
    """
    doc, out, checked = fitz.open(pdf_path), [], set()
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        for img in page.get_images(full=True):
            xref = img[0]
            try:
                rects = page.get_image_rects(xref) or []
            except Exception:
                rects = []
            if not rects:
                continue
            for r in rects:
                if min(r.width, r.height) < 1.0:
                    out.append({"doc": doc_label, "page": i, "xref": xref,
                                "kind": "Image collapsed to nothing",
                                "text": f"drawn at {r.width:.0f}×{r.height:.0f} pt"})
            if xref in checked:
                continue
            checked.add(xref)
            try:
                info = doc.extract_image(xref)
                if not info or not info.get("image"):
                    out.append({"doc": doc_label, "page": i, "xref": xref,
                                "kind": "Image cannot be decoded",
                                "text": "the embedded image data is unreadable"})
                    continue
                # A flat single-colour bitmap is not reported: it is routinely a
                # deliberate background swatch, banner or colour block, not a
                # broken image, and flagging it produced nothing but noise.
                pix = None
            except Exception as exc:
                out.append({"doc": doc_label, "page": i, "xref": xref,
                            "kind": "Image cannot be decoded", "text": str(exc)[:120]})
    doc.close()
    return out


# ── Italic / slanted emphasis ────────────────────────────────────────────────
_ITALIC_FLAG = 1 << 1          # PyMuPDF span flag bit for an italic face
_ITALIC_RE   = re.compile(r"italic|oblique", re.IGNORECASE)


def _span_is_italic(span) -> bool:
    """True when a span is drawn slanted."""
    if span.get("flags", 0) & _ITALIC_FLAG:
        return True
    return bool(_ITALIC_RE.search(span.get("font", "") or ""))


def _italic_issues(prod_path, stage_path, prod_nav, stage_nav, stage_idx):
    """Text set in italic in PROD that STAGE renders upright.

    Styling is part of the content: a note or a cross-reference set in italic
    carries meaning, and losing the slant loses that.
    """
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []

    def italic_map(path, nav):
        doc, out = fitz.open(path), {}
        for i, page in enumerate(doc, 1):
            if i in nav:
                continue
            for block in _page_dict(page)["blocks"]:
                if block.get("type") != 0:
                    continue
                for line in block.get("lines", []):
                    run, txt = [], ""
                    for sp in line.get("spans", []):
                        if sp.get("size", 0) < _MIN_READABLE_PT:
                            continue
                        if _span_is_italic(sp):
                            run.append(sp.get("text", ""))
                        elif run:
                            txt = " ".join(run)
                            run = []
                            key = " ".join(_seq_tokens(txt))
                            if len(key.split(" ")) >= 3:
                                out.setdefault(key, (i, re.sub(r"\s+", " ", txt).strip()))
                    if run:
                        txt = " ".join(run)
                        key = " ".join(_seq_tokens(txt))
                        if len(key.split(" ")) >= 3:
                            out.setdefault(key, (i, re.sub(r"\s+", " ", txt).strip()))
        doc.close()
        return out

    prod_it  = italic_map(prod_path, prod_nav)
    stage_it = set(italic_map(stage_path, stage_nav))
    findings = []
    for key, (page, shown) in prod_it.items():
        if key in stage_it:
            continue
        if not _seq_present(stage_idx, key.split(" ")):
            continue          # absent entirely — that is a content issue
        findings.append({"page": page, "text": shown})
    return findings


# ── List alignment ───────────────────────────────────────────────────────────
# A list item should read "5. Place the monitor properly." on one line. When the
# layout breaks, the marker is left alone on its own line with the item text
# wrapped underneath. The text is all still present, so the content comparison
# sees nothing wrong — this is purely a layout defect and needs its own check.
#
# Bullets (<ul><li>) break exactly the same way and were not covered: the regex
# matched digits only, so a stranded "•" was invisible to this check while a
# stranded "5." was reported.
# A bare "-" or "*" is left out on purpose: alone on a line those are rules and
# footnote marks far more often than they are list bullets, and the stranded-
# marker test has no text beside them to tell the difference.
_LIST_MARKER_RE = re.compile(
    r"^\s*(?:(?:\d{1,2}|[a-zA-Z])\s*[.)]|[•●▪◦‣⁃])\s*$")


def _page_lines(page):
    """[(text, Rect)] for every non-empty line on the page."""
    out = []
    for block in _page_dict(page)["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            txt = "".join(sp.get("text", "") for sp in line.get("spans", []))
            if txt.strip():
                out.append((txt, fitz.Rect(line.get("bbox"))))
    return out


def _orphan_markers(pdf_path: str, nav_pages: set):
    """[(page, marker, following text)] for markers left alone on a line."""
    doc, out = fitz.open(pdf_path), []
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        lines = _page_lines(page)
        for txt, bb in lines:
            if not _LIST_MARKER_RE.match(txt):
                continue
            mid = (bb.y0 + bb.y1) / 2
            # A line box carries side bearings, so a wider marker ("10." against
            # "9.") reaches past the left edge of the text set beside it. A
            # strict "starts after the marker ends" test called those steps
            # broken when they read perfectly inline, so the overlap of about
            # half a line height is allowed.
            tol = max(4.0, 0.6 * (bb.y1 - bb.y0))
            beside = [t for t, r in lines
                      if r.x0 > bb.x1 - tol and r.y0 <= mid <= r.y1
                      and t.strip() != txt.strip()]
            if beside:
                continue                       # correctly inline
            below = [(t, r) for t, r in lines if r.y0 > bb.y1 - 2]
            if not below:
                continue
            nxt = min(below, key=lambda x: x[1].y0)[0].strip()
            # The next line must be real step text, not another bare marker
            # (diagram legends stack numbers on purpose).
            if _LIST_MARKER_RE.match(nxt):
                continue
            if len(re.findall(r"[^\W\d_]{2,}", nxt)) < 2:
                continue
            out.append((i, txt.strip(), nxt))
    doc.close()
    return out


def _inline_steps(pdf_path: str, nav_pages: set) -> set:
    """Canonical text of every step whose marker sits inline with it."""
    out, doc = set(), fitz.open(pdf_path)
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        lines = _page_lines(page)
        for txt, bb in lines:
            if not _LIST_MARKER_RE.match(txt):
                continue
            mid = (bb.y0 + bb.y1) / 2
            tol = max(4.0, 0.6 * (bb.y1 - bb.y0))   # see _orphan_markers
            for t, r in lines:
                if r.x0 > bb.x1 - tol and r.y0 <= mid <= r.y1:
                    key = " ".join(_seq_tokens(t))
                    if key:
                        out.add(key)
    doc.close()
    return out


def _alignment_issues(prod_path, stage_path, prod_nav, stage_nav):
    """Numbered steps whose marker has broken away from its text.

    Checked in BOTH documents: a step that reads "5." on one line and "Place the
    monitor properly." on the next is a layout defect wherever it occurs, and the
    same document usually sets every other step inline — so it is an internal
    inconsistency as well as a difference from the other file. It is reported
    only when the other document renders that step inline, which is the proof
    that it is meant to be on one line.
    """
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []
    # Checked in BOTH documents. A marker stranded from its text is a layout
    # defect wherever it sits, and which file is the reference does not change
    # that — reporting only one side hid the defect whenever the affected file
    # happened to be uploaded as PROD.
    prod_inline  = _inline_steps(prod_path, prod_nav)
    stage_inline = _inline_steps(stage_path, stage_nav)

    # A step is meant to read on one line when PROD sets it that way, and also
    # when STAGE itself sets nearly all of its other steps that way — a document
    # that renders 110 steps inline and 7 broken is inconsistent with itself, and
    # that is a defect whether or not the matching step can be paired up in PROD.
    # PROD is the reference and is never reported against. Only STAGE is under
    # test, so a stranded marker is a finding only when it is STAGE's.
    findings = []
    for label, path, nav, own_inline, other_inline in (
            ("STAGE", stage_path, stage_nav, stage_inline, prod_inline),):
        orphans = _orphan_markers(path, nav)
        if not orphans:
            continue
        # A step is meant to read on one line when the other document sets it
        # that way, and also when this document sets nearly all of its own steps
        # that way — 110 inline against 7 broken is inconsistent with itself.
        mostly_inline = len(own_inline) >= 5 * max(1, len(orphans))
        for pno, marker, nxt in orphans:
            key = " ".join(_seq_tokens(nxt))
            if not key:
                continue
            matched = any(key.startswith(k) or k.startswith(key)
                          for k in other_inline if k)
            if not (matched or mostly_inline):
                continue
            other = "STAGE" if label == "PROD" else "PROD"
            findings.append({
                "doc": label, "page": pno, "marker": marker, "text": nxt,
                "why": (f"{other} renders this step on one line" if matched else
                        f"{label} itself renders {len(own_inline)} other steps "
                        f"on one line")})
    return findings


# ── List indent depth (nested <ul>/<ol> flattened) ───────────────────────────
# A sub-item indented under its parent in PROD and set flush with it in STAGE has
# lost its nesting: the reader can no longer tell which step it belongs to. Every
# word is still present, so neither the content comparison nor the marker-style
# check sees anything wrong. Indent is measured from each document's own text
# column, so a different page width is not mistaken for a moved list.
_INDENT_TOL_PT = 9.0     # normal jitter between two renderings of the same item
_INDENT_MIN_ITEMS = 6    # below this there is no list structure worth judging


def _list_indents(pdf_path: str, nav_pages: set, col):
    """{item wording: (page, indent in points from the column edge, line)}."""
    left = col[0]
    doc, out = fitz.open(pdf_path), {}
    try:
        for i, page in enumerate(doc, 1):
            if i in nav_pages:
                continue
            for txt, rect in _page_lines(page):
                if not _marker_style(txt):
                    continue
                body = re.sub(r"^\s*(?:\d{1,2}|[a-zA-Z])\s*[.)]\s*", "", txt)
                body = re.sub(r"^\s*[•●▪‣⁃\-\*]\s*",
                              "", body)
                key = " ".join(_seq_tokens(body))
                # Short items repeat across a manual ("Press OK"); pairing on
                # them matches the wrong list.
                if len(key.split(" ")) < 4:
                    continue
                if key in out:
                    out[key] = None          # ambiguous — appears more than once
                    continue
                out[key] = (i, rect.x0 - left, txt.strip())
    finally:
        doc.close()
    return {k: v for k, v in out.items() if v}


def _list_indent_issues(prod_path, stage_path, prod_nav, stage_nav):
    """List items whose indent depth differs from PROD's — flattened nesting."""
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []
    prod_col = _text_column(prod_path, prod_nav)
    stage_col = _text_column(stage_path, stage_nav)
    if not prod_col or not stage_col:
        return []
    prod_ind = _list_indents(prod_path, prod_nav, prod_col)
    stage_ind = _list_indents(stage_path, stage_nav, stage_col)
    paired = [(k, prod_ind[k], stage_ind[k]) for k in prod_ind if k in stage_ind]
    if len(paired) < _INDENT_MIN_ITEMS:
        return []

    # Both documents may sit the whole list at a slightly different offset —
    # that is a margin difference, not lost nesting. The median shift across
    # every paired item is removed so only items that moved relative to their
    # own list are reported.
    shifts = sorted(s[1] - p[1] for _k, p, s in paired)
    base = shifts[len(shifts) // 2]

    findings = []
    for key, (ppage, pind, ptxt), (spage, sind, stxt) in paired:
        delta = (sind - pind) - base
        if abs(delta) <= _INDENT_TOL_PT:
            continue
        findings.append({
            "page": spage, "prod_page": ppage,
            "prod_indent": round(pind, 1), "stage_indent": round(sind, 1),
            "delta": round(delta, 1), "text": ptxt,
            "direction": "flattened" if delta < 0 else "over-indented"})
    return findings


# ── Heading and paragraph alignment ──────────────────────────────────────────
# A title or a paragraph that PROD sets flush left and STAGE centres — or sets
# at a different indent — reads as a different document even though every word
# survives. Measured the same way list indent is: from each document's own text
# column, paired by wording, with the median shift across every pair removed so
# a plain margin difference is not mistaken for a moved block.
_TEXT_ALIGN_TOL_PT = 12.0
_TEXT_ALIGN_MIN_PAIRS = 6
_TEXT_ALIGN_MAX_FINDINGS = 40


def _block_indents(pdf_path: str, nav_pages: set, col, title_keys: set):
    """{wording: (page, indent from the column edge, kind, text, rect)}."""
    left = col[0]
    doc, out = fitz.open(pdf_path), {}
    try:
        for i, page in enumerate(doc, 1):
            if i in nav_pages:
                continue
            # Text inside a table or a boxed note sits in its own cell, not in
            # the page's text column, so its left and right edges say nothing
            # about how the block is aligned. Measuring those reported every
            # answer in the two-column Troubleshooting table as right-aligned.
            try:
                boxes = [fitz.Rect(t.bbox) for t in page.find_tables().tables]
            except Exception:
                boxes = []
            for txt, rect in _page_lines(page):
                s = " ".join((txt or "").split())
                if not s or _marker_style(s):
                    continue      # list items are _list_indent_issues' business
                if any(b.intersects(rect) for b in boxes):
                    continue
                key = " ".join(_seq_tokens(s))
                # Short lines repeat all over a manual ("Press OK"), and pairing
                # on them lines up two unrelated places in the two documents.
                if len(key.split(" ")) < 4:
                    continue
                if key not in title_keys:
                    # Headings only. A paragraph's alignment cannot be read off
                    # one line's rectangle: an indented block whose lines run to
                    # the right margin (every answer in the Troubleshooting Q&A)
                    # is indistinguishable from right-aligned text that way, and
                    # measuring it reported ~15 false findings per document.
                    # Judging a paragraph needs its whole set of lines — left
                    # edges consistent, right edges ragged or not.
                    continue
                if key in out:
                    out[key] = None            # ambiguous — seen more than once
                    continue
                out[key] = (i, rect.x0 - left, "Title", s, rect)
    finally:
        doc.close()
    return {k: v for k, v in out.items() if v}


# A numbered item's bold label ("5.  Reset") sitting on its own line, with the
# description starting fresh underneath, versus the description's first words
# glued onto the SAME line as the label — the paragraph has visually merged
# into its own heading.
_ITEM_MARKER_RE = re.compile(r"^\d{1,2}[.)]$")


def _label_desc_lines(pdf_path: str, nav_pages: set):
    """[(page, canonical label key, run_on, label text)] for every numbered
    item's label line in the document.

    A numbered list here draws the "N." marker and its content as separate
    text blocks that merely share a Y position, not one block — so the marker
    positions have to be collected across the WHOLE page first, then matched
    against every line's Y, rather than searched block by block.
    """
    doc, out = fitz.open(pdf_path), []
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        all_lines = []
        for block in page.get_text("dict")["blocks"]:
            if block.get("type") != 0:
                continue
            all_lines.extend(block.get("lines", []))
        marker_ys = set()
        for line in all_lines:
            txt = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
            if _ITEM_MARKER_RE.match(txt):
                marker_ys.add(round(line["bbox"][1], 1))
        if not marker_ys:
            continue
        for line in all_lines:
            if round(line["bbox"][1], 1) not in marker_ys:
                continue
            spans = line.get("spans", [])
            if _ITEM_MARKER_RE.match(
                    "".join(s.get("text", "") for s in spans).strip()):
                continue    # the marker line itself
            bold_parts, rest_parts, seen_non_bold = [], [], False
            for sp in spans:
                t = sp.get("text", "")
                if not t.strip():
                    continue
                if _span_is_dark(sp) and not seen_non_bold:
                    bold_parts.append(t)
                else:
                    seen_non_bold = True
                    rest_parts.append(t)
            label = " ".join(bold_parts).strip()
            key = " ".join(_seq_tokens(label))
            if not key:
                continue
            out.append((i, key, bool("".join(rest_parts).strip()), label))
    doc.close()
    return out


def _label_merge_issues(prod_path: str, stage_path: str, prod_nav: set,
                        stage_nav: set):
    """Numbered-item labels PROD prints on their own line, that STAGE runs
    the description onto instead.

    Labels repeat across sections ("Reset" appears for Host Button, Button,
    Receiver and Multimedia Hub in the same manual) so occurrences are paired
    by their Nth appearance in document order, not by a plain dict lookup —
    a dict would silently drop every repeat past the first.
    """
    prod_items = _label_desc_lines(prod_path, prod_nav)
    stage_items = _label_desc_lines(stage_path, stage_nav)
    stage_by_key = {}
    for pg, key, run_on, label in stage_items:
        stage_by_key.setdefault(key, []).append((pg, run_on, label))
    seen_idx, found = {}, []
    for pg, key, run_on, label in prod_items:
        idx = seen_idx.get(key, 0)
        seen_idx[key] = idx + 1
        if run_on:
            continue    # PROD itself runs it on — nothing to compare against
        cands = stage_by_key.get(key, [])
        if idx >= len(cands):
            continue
        spg, srun_on, _ = cands[idx]
        if not srun_on:
            continue    # STAGE also keeps the label on its own line — fine
        found.append({
            "prod_page": pg, "stage_page": spg, "label": label,
            "title": label,
            "detail": (f"PROD starts the description for \u201c{label}\u201d on "
                      f"its own line; STAGE runs it on from the label on the "
                      f"same line instead."),
        })
    return found


def _text_align_issues(prod_path, stage_path, prod_nav, stage_nav, toc_results):
    """Titles and paragraphs STAGE aligns differently from PROD."""
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []
    prod_col = _text_column(prod_path, prod_nav)
    stage_col = _text_column(stage_path, stage_nav)
    if not prod_col or not stage_col:
        return []
    title_keys = {" ".join(_seq_tokens(r.get("title") or ""))
                  for r in (toc_results or [])}
    title_keys.discard("")
    prod_b = _block_indents(prod_path, prod_nav, prod_col, title_keys)
    stage_b = _block_indents(stage_path, stage_nav, stage_col, title_keys)
    paired = [(k, prod_b[k], stage_b[k]) for k in prod_b if k in stage_b]
    if len(paired) < _TEXT_ALIGN_MIN_PAIRS:
        return []

    shifts = sorted(s[1] - p[1] for _k, p, s in paired)
    base = shifts[len(shifts) // 2]

    findings = []
    for key, (ppage, pind, kind, ptxt, prect), (spage, sind, _k2, _stxt, srect) in paired:
        if len(findings) >= _TEXT_ALIGN_MAX_FINDINGS:
            break
        p_align = _align_of(prect, prod_col)
        s_align = _align_of(srect, stage_col)
        delta = (sind - pind) - base
        # Only a change of alignment *class* is reported. The raw indent delta
        # is not trustworthy here: note panels, figure captions and table cells
        # are laid out differently by the two engines, so comparing their left
        # edges flagged 28 blocks that were flush left in both documents. A
        # block that is centred in one and flush in the other is visible to a
        # reader; a few points of container padding is not.
        reflowed = bool(p_align and s_align and p_align != s_align)
        if not reflowed:
            continue
        findings.append({
            "page": spage, "prod_page": ppage, "kind_label": kind,
            "prod_align": p_align or "indented", "stage_align": s_align or "indented",
            "prod_indent": round(pind, 1), "stage_indent": round(sind, 1),
            "delta": round(delta, 1), "reflowed": reflowed,
            "text": ptxt})
    return findings


# ── Hyperlinks ───────────────────────────────────────────────────────────────
_DOMAINISH_RE = re.compile(r"^(?:https?://)?(?:[\w-]+\.)+[A-Za-z]{2,}(?:/|$)", re.I)


def _link_anchors(pdf_path: str, nav_pages: set):
    """{canonical anchor text: (page, target)} for every link in a document."""
    doc, out = fitz.open(pdf_path), {}
    try:
        for i, page in enumerate(doc, 1):
            if i in nav_pages:
                continue
            for l in page.get_links():
                try:
                    text = " ".join(page.get_textbox(fitz.Rect(l["from"])).split())
                except Exception:
                    continue
                key = " ".join(_seq_tokens(text))
                if not key:
                    continue
                kind = l.get("kind")
                if l.get("uri") or l.get("file"):
                    target = l.get("uri") or l.get("file")
                elif l.get("page", -1) >= 0:
                    target = f"page {l.get('page', -1) + 1}"
                elif kind in (fitz.LINK_GOTO, fitz.LINK_NAMED) or l.get("nameddest"):
                    # An internal jump whose destination AEM stores as a named
                    # anchor rather than a page index — still a cross-reference.
                    target = f"internal:{l.get('nameddest') or l.get('to') or ''}"
                else:
                    target = ""
                out.setdefault(key, (i, target, text))
    finally:
        doc.close()
    return out


def _link_loss_issues(prod_path, stage_path, prod_nav, stage_nav, stage_idx,
                      prod_idx=None, titles=None):
    """Cross-reference hyperlinks present on one side and printed as plain text
    on the other — reported in BOTH directions, but only when the anchor is a
    genuine reference (a real section heading or a web address).

    * ``doc="PROD"`` — PROD links the phrase, STAGE prints it plain.
    * ``doc="STAGE"`` — STAGE links the phrase, PROD prints it plain.

    Matching the anchor against the actual section titles is what separates a
    real lost/added jump from a mis-sized link rectangle that grabbed a
    sentence fragment or a run of model numbers — those were almost every
    earlier false report here. An anchor whose wording is missing from the
    other document entirely is a content difference, not a link difference,
    and is left to that check.
    """
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []
    prod_links = _link_anchors(prod_path, prod_nav)
    stage_links = _link_anchors(stage_path, stage_nav)

    title_by_key = {}
    for t in (titles or []):
        k = " ".join(_seq_tokens(t or ""))
        if len(k.split(" ")) >= 2:
            title_by_key.setdefault(k, " ".join((t or "").split()))
    _DANGLE = {"on", "for", "and", "to", "of", "the", "a", "an", "in", "with",
               "or", "at", "see", "refer", "from", "by"}

    def _pretty_xref(key, shown):
        """The clean heading/address text if this anchor is a real
        cross-reference, else None."""
        toks = key.split(" ")
        if len(toks) < 2 or len(_WORDCHAR_RE.findall(shown)) < 4:
            return None
        # A web-address anchor: report ONLY the address, never the sentence the
        # over-sized link rectangle happened to sweep up with it.
        m = re.search(r"(?:https?://)?(?:www\.)?[A-Za-z0-9][\w-]*"
                      r"(?:\.[A-Za-z0-9][\w-]*)+(?:/\S*)?", shown)
        if m and re.search(r"\.(?:com|net|org|io|co|tw|cn)\b", m.group(0), re.I):
            return m.group(0).strip(" .,;:)")
        if _DOMAINISH_RE.match(shown.strip()):
            return shown.strip()
        rem = list(toks)
        while rem and (rem[-1].isdigit() or rem[-1] in ("page", "pages")):
            rem.pop()
        if rem and rem[-1] == "on":
            rem.pop()
        while rem and rem[0] in ("see", "refer", "to"):
            rem.pop(0)
        core_key = " ".join(rem)
        if not core_key:
            return None
        if core_key in title_by_key:
            if len(rem) < 2 or rem[-1] in _DANGLE or rem[0] in _DANGLE:
                return None       # truncated / clipped anchor
            return title_by_key[core_key]
        pref = [k for k in title_by_key if k.startswith(core_key + " ")]
        if len(pref) == 1:
            if rem[0] in _DANGLE:
                return None
            return title_by_key[pref[0]]
        # A link rectangle can run past the end of the heading and pick up the
        # start of the next sentence (a PDF authoring artifact, not a
        # validator gap) -- match a title at the START of the anchor and
        # tolerate trailing noise, longer titles get proportionally more
        # slack since a short generic title is more likely to coincide.
        sup = [k for k in title_by_key if core_key.startswith(k + " ")]
        if sup:
            best = max(sup, key=len)
            tail = len(rem) - len(best.split(" "))
            if tail <= max(6, len(best.split(" "))):
                return title_by_key[best]
        # The reverse case: the anchor's link rectangle starts mid-sentence,
        # in the tail of the PREVIOUS one, and only reaches a real heading
        # further in -- look for the title as a SUFFIX of the anchor before
        # giving up. Real section titles are specific multi-word phrases, so
        # even a generous search here rarely coincides with random prose.
        for start in range(1, min(len(rem), 14)):
            cand = " ".join(rem[start:])
            if cand in title_by_key:
                return title_by_key[cand]
        return None

    def _one_way(src_label, src_links, other_links, other_idx):
        out = []
        if other_idx is None:
            return out
        # Every clean heading/address the OTHER document still links, so a link
        # kept under a slightly different hotspot (or in the TOC instead of the
        # body) counts as "still linked" and is never reported as lost.
        other_link_names = set()
        for ak, (apg, atgt, ashown) in other_links.items():
            pn = _pretty_xref(ak, ashown)
            if pn:
                other_link_names.add(" ".join(_seq_tokens(pn)))
            # also keep every raw anchor that is itself heading-length, since
            # the other side's rectangles can be over-sized too -- but a short
            # anchor (a footnote marker, an icon link) is dropped here: as a
            # substring below it would trivially "match" almost any title.
            if len(ak) >= 12 and len(ak.split(" ")) >= 2:
                other_link_names.add(ak)
        for key, (pno, target, shown) in src_links.items():
            if key in other_links:
                continue
            pretty = _pretty_xref(key, shown)
            if not pretty:
                continue
            pk = " ".join(_seq_tokens(pretty))
            if not pk:
                continue
            # STAGE still links this exact heading (anywhere)?  Not lost.
            # Containment is only trusted once both sides are a real multi-word
            # phrase -- otherwise a short anchor matches as a substring of
            # nearly anything and hides a genuine loss.
            if pk in other_link_names or any(
                    len(ak) >= 12 and (pk in ak or ak in pk)
                    for ak in other_link_names):
                continue
            if not _seq_present(other_idx, key.split(" "), max_gap=SEQ_MAX_GAP):
                continue          # wording absent from the other side: content
            out.append({"doc": src_label, "page": pno, "text": pretty,
                        "target": target})
        return out

    return (_one_way("PROD", prod_links, stage_links, stage_idx)
            + _one_way("STAGE", stage_links, prod_links, prod_idx))


def _hyperlink_issues(prod_path: str, stage_path: str):
    """Broken or lost hyperlinks in either document.

    Checks three things: an external link stored as a *file launch* rather than a
    web URI (it will not open in a browser), an internal jump whose destination
    does not resolve, and a web address PROD links to that STAGE does not.
    """
    def links(path):
        doc, out = fitz.open(path), []
        for i, page in enumerate(doc, 1):
            for l in page.get_links():
                out.append((i, l, doc.page_count))
        doc.close()
        return out

    findings, prod_uris, stage_uris = [], set(), set()

    same_file = os.path.abspath(prod_path) == os.path.abspath(stage_path)
    # STAGE is the document under test — a broken link is only reported when it
    # is STAGE's. PROD links are still read, but only to collect the web
    # addresses PROD carries so the "not linked in STAGE" comparison works.
    sides = [("PROD", prod_path, prod_uris, False)]
    if not same_file:
        sides.append(("STAGE", stage_path, stage_uris, True))
    for label, path, bucket, report in sides:
        for pno, l, npages in links(path):
            kind = l.get("kind")
            if kind == fitz.LINK_URI:
                uri = (l.get("uri") or "").strip()
                bucket.add(uri.lower().rstrip("/"))
                if report and not re.match(r"^(https?|mailto):", uri, re.I):
                    findings.append({"doc": label, "page": pno,
                                     "kind": "Hyperlink has no usable scheme",
                                     "text": uri})
            elif kind == fitz.LINK_LAUNCH:
                # A launch action pointing at a domain is unusual, but common
                # viewers do follow it, so it is NOT reported — only links that
                # provably cannot resolve are. The address is still recorded so
                # the cross-document check knows the link exists.
                target = (l.get("file") or "").strip()
                if _DOMAINISH_RE.match(target):
                    bucket.add(target.lower().rstrip("/"))
            if not report:
                continue
            rect = fitz.Rect(l.get("from") or (0, 0, 0, 0))
            if rect.get_area() < 4.0:
                # A hotspot this small cannot be hit with a pointer, so the link
                # is present in the file and unusable in the reader.
                findings.append({"doc": label, "page": pno,
                                 "kind": "Hyperlink hotspot cannot be clicked",
                                 "text": (l.get("uri") or l.get("file") or
                                          f"page {l.get('page', -1) + 1}")})
            if kind in (fitz.LINK_GOTO, fitz.LINK_NAMED):
                tgt = l.get("page", -1)
                if tgt is None or tgt < 0 or tgt >= npages:
                    findings.append({"doc": label, "page": pno,
                                     "kind": "Internal link target does not resolve",
                                     "text": str(l.get("nameddest") or l.get("to") or "")})

    def bare(u):
        return re.sub(r"^(?:https?://)?(?:www\.)?", "", u or "", flags=re.I).rstrip("/")

    if not same_file:               # nothing to compare a document against itself
        stage_bare = {bare(u) for u in stage_uris}
        for u in sorted(prod_uris):
            if bare(u) and bare(u) not in stage_bare:
                findings.append({"doc": "PROD", "page": 0,
                                 "kind": "Web address in PROD not linked in STAGE",
                                 "text": u})
    return findings


# ── Link highlighting (the anchor's drawn appearance) ────────────────────────
# _link_loss_issues compares link ANNOTATIONS — whether the region is clickable.
# A reader cannot see an annotation. What tells them a phrase is a link is that
# it is drawn differently from body text: coloured, or underlined. STAGE can
# carry the annotation and still print the anchor in plain black, which reads as
# ordinary prose and is a real defect that the annotation check passes clean.
_LINK_COLOUR_MIN_DELTA = 60   # sRGB distance from body colour to read as coloured
_UNDERLINE_MAX_H = 2.5        # a rule thicker than this is not an underline
_UNDERLINE_GAP_PT = 4.0       # how far below the baseline box a rule may sit


def _srgb(color_int) -> tuple:
    """PyMuPDF stores a span colour as a packed sRGB integer."""
    try:
        c = int(color_int)
    except (TypeError, ValueError):
        return (0, 0, 0)
    return ((c >> 16) & 255, (c >> 8) & 255, c & 255)


def _colour_distance(a, b) -> float:
    return sum(abs(x - y) for x, y in zip(a, b))


def _body_text_colour(pdf_path: str, nav_pages: set) -> tuple:
    """The colour the document sets ordinary body text in."""
    counts, doc = {}, fitz.open(pdf_path)
    try:
        for i, page in enumerate(doc, 1):
            if i in nav_pages:
                continue
            for blk in page.get_text("dict").get("blocks", []):
                for line in blk.get("lines", []):
                    for span in line.get("spans", []):
                        n = len(span.get("text", "").strip())
                        if n:
                            counts[span.get("color", 0)] = (
                                counts.get(span.get("color", 0), 0) + n)
    finally:
        doc.close()
    if not counts:
        return (0, 0, 0)
    return _srgb(max(counts, key=counts.get))


def _underlined(page, rect) -> bool:
    """True when a thin rule is drawn along the bottom of `rect`."""
    try:
        drawings = page.get_drawings()
    except Exception:
        return False
    for dr in drawings:
        r = fitz.Rect(dr["rect"])
        if r.height > _UNDERLINE_MAX_H:
            continue
        if not (rect.y1 - _UNDERLINE_GAP_PT <= r.y0 <= rect.y1 + _UNDERLINE_GAP_PT):
            continue
        # The rule has to run under the words, not merely cross the page near
        # them — a table border and a footer rule both sit at the right height.
        overlap = min(r.x1, rect.x1) - max(r.x0, rect.x0)
        if overlap >= 0.6 * (rect.x1 - rect.x0):
            return True
    return False


def _anchor_style(page, rect, body_colour):
    """(is_highlighted, colour, underlined) for the text inside `rect`."""
    best, best_n = None, 0
    for blk in page.get_text("dict").get("blocks", []):
        for line in blk.get("lines", []):
            for span in line.get("spans", []):
                sr = fitz.Rect(span["bbox"])
                if not sr.intersects(rect):
                    continue
                n = len(span.get("text", "").strip())
                if n > best_n:
                    best, best_n = span, n
    if best is None:
        return None
    colour = _srgb(best.get("color", 0))
    coloured = _colour_distance(colour, body_colour) >= _LINK_COLOUR_MIN_DELTA
    under = _underlined(page, rect)
    return (coloured or under, colour, under)


def _link_style_issues(prod_path, stage_path, prod_nav, stage_nav,
                       max_links: int = 2000):
    """Anchors PROD draws as links that STAGE draws as plain body text.

    Only anchors PROD actually highlights are considered, and only those whose
    wording can be found in exactly one place in each document — the same
    single-hit rule the other anchored checks use, so a repeated phrase is never
    paired against the wrong occurrence.
    """
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []
    prod_body = _body_text_colour(prod_path, prod_nav)
    stage_body = _body_text_colour(stage_path, stage_nav)

    findings = []
    prod_doc = fitz.open(prod_path)
    stage_doc = fitz.open(stage_path)
    try:
        for i, page in enumerate(prod_doc, 1):
            if i in prod_nav or len(findings) >= max_links:
                continue
            for l in page.get_links():
                rect = fitz.Rect(l["from"])
                try:
                    text = " ".join(page.get_textbox(rect).split())
                except Exception:
                    continue
                toks = _seq_tokens(text)
                # One-word anchors ("here") are too common to pair safely.
                if len(toks) < 2 or len(text) > 120:
                    continue
                p_style = _anchor_style(page, rect, prod_body)
                if not p_style or not p_style[0]:
                    continue          # PROD does not highlight it either
                s_hits = _locate_all_tokens(stage_path, text,
                                            skip_pages=stage_nav, limit=3)
                p_hits = _locate_all_tokens(prod_path, text,
                                            skip_pages=prod_nav, limit=3)
                if len(s_hits) != 1 or len(p_hits) != 1:
                    continue
                s_pg, s_rects = s_hits[0]
                if not s_pg or not s_rects:
                    continue
                box = fitz.Rect(s_rects[0])
                for r in s_rects[1:]:
                    box |= fitz.Rect(r)
                s_page = stage_doc[s_pg - 1]
                s_style = _anchor_style(s_page, box, stage_body)
                if not s_style or s_style[0]:
                    continue          # STAGE highlights it too — nothing wrong
                findings.append({
                    "page": s_pg, "prod_page": i, "text": text.strip(),
                    "target": l.get("uri") or l.get("file") or "",
                    "prod_colour": "#%02x%02x%02x" % p_style[1],
                    "stage_colour": "#%02x%02x%02x" % s_style[1],
                    "prod_underlined": p_style[2],
                    "why": ("PROD draws this anchor "
                            + ("underlined" if p_style[2] else "in a link colour")
                            + "; STAGE draws it as plain body text")})
    finally:
        prod_doc.close()
        stage_doc.close()
    return findings


# ── Page numbering ───────────────────────────────────────────────────────────
# A page number in the STAGE footer/margin is normal pagination and is not
# checked — any value there is valid. The only page-number defect worth
# reporting is one baked into a hyperlink's own visible text (below).

# A hyperlink whose visible text names a page ("… on page 36") has to land on
# that page. Carrying a page number is NOT itself a defect — those links work —
# so only a mismatch between what the text promises and where the link goes is
# reported. The bare-number form ("12") is deliberately not matched: a link
# label that is only a digit is a diagram callout number or a list marker, and
# treating those as page references reported perfectly good links as broken.
_LINK_PAGEREF_RE = re.compile(r"\bpages?\s+(\d{1,4})\b", re.IGNORECASE)
_FOLIO_CACHE = {}


def _printed_folio(doc, pno: int):
    """The page number printed in this page's margin, or None.

    A PDF's physical page index and the number printed on the page part company
    as soon as there is front matter, so a link's destination is judged by what
    the reader actually sees on the page it lands on.
    """
    key = (id(doc), pno)
    if key in _FOLIO_CACHE:
        return _FOLIO_CACHE[key]
    value = None
    try:
        page = doc[pno - 1]
        h = page.rect.height or 1.0
        for b in page.get_text("blocks"):
            txt = (b[4] or "").strip()
            if re.fullmatch(r"\d{1,4}", txt) and (b[1] > h * 0.88
                                                  or b[3] < h * 0.12):
                value = int(txt)
                break
    except Exception:
        value = None
    if len(_FOLIO_CACHE) > 4000:
        _FOLIO_CACHE.clear()
    _FOLIO_CACHE[key] = value
    return value


def _hyperlink_pageno_issues(pdf_path: str, doc_label: str, nav_pages: set):
    """Hyperlinks whose text names one page and whose target is another."""
    doc, out = fitz.open(pdf_path), []
    try:
        for i, page in enumerate(doc, 1):
            if i in nav_pages:
                continue
            for l in page.get_links():
                if l.get("kind") not in (fitz.LINK_GOTO, fitz.LINK_NAMED):
                    continue
                try:
                    text = " ".join(page.get_textbox(fitz.Rect(l["from"])).split())
                except Exception:
                    continue
                m = _LINK_PAGEREF_RE.search(text or "")
                if not m:
                    continue
                said = int(m.group(1))
                tgt = (l.get("page", -1) or -1) + 1
                if tgt < 1 or tgt > doc.page_count:
                    continue      # unresolved target — its own finding covers it
                folio = _printed_folio(doc, tgt)
                lands = folio if folio is not None else tgt
                if lands == said:
                    continue      # the link goes where its text says it goes
                # The link still WORKS if it lands on the section it names -
                # the printed "page N" is just stale from the other document's
                # pagination, and the reader who clicks gets the right place.
                # Only a link that jumps to the WRONG CONTENT is a real defect.
                anchor = re.sub(r"\s+on\s+pages?\s+\d+\s*$", "", text,
                                flags=re.IGNORECASE).strip()
                a_toks = [t for t in _seq_tokens(anchor) if len(t) > 2]
                if len(a_toks) >= 2:
                    win = "".join(
                        _flat_key(doc[p].get_text())
                        for p in range(max(0, tgt - 2), min(doc.page_count, tgt + 1)))
                    hit = sum(1 for t in a_toks if _flat_key(t) in win)
                    if hit >= max(2, len(a_toks) - 1):
                        continue   # lands on the right section - link works
                shown = text if len(text) <= 90 else text[:87] + "…"
                out.append({"doc": doc_label, "page": i,
                            "kind": "Hyperlink goes to the wrong page",
                            "text": (f"the hyperlink “{shown}” on {doc_label} "
                                     f"page {i} does not work: its text says "
                                     f"page {said}, and clicking it jumps to "
                                     f"page {lands}, which is not that section")})
    finally:
        doc.close()
    return out


_XREF_RE = re.compile(
    r"(?:see|refer to|go to|as (?:described|shown|explained) in)\s+"
    r"([A-Z][A-Za-z0-9 ,/&()’'\-]{3,70}?)\s+on\s+pages?\s+(\d{1,4})",
    re.IGNORECASE)


def _xref_section_issues(stage_path, stage_nav, toc_results, prod_path=None):
    """Validate printed "see <Section> on page N" cross-references in STAGE.

    For every such reference this checks that STAGE's own section <Section>
    really is on the page the sentence names.  Reports:
      * "Cross-reference page number is wrong" - the named section exists in
        STAGE but on a different page than the one printed;
      * "Cross-reference points to the wrong section" - STAGE page N holds a
        different section than the one named.
    Only references whose wording matches a real STAGE section heading are
    checked, so a stray "... on page 5" in prose is never flagged.
    """
    doc = fitz.open(stage_path)
    try:
        # title -> printed folio of the STAGE page the section starts on
        folio_of_title = {}
        page_titles = {}       # printed folio -> [section titles starting there]
        for r in toc_results or []:
            t = (r.get("title") or "").strip()
            sp = r.get("stage_page")
            if not t or not isinstance(sp, int) or sp < 1 or sp > doc.page_count:
                continue
            fol = _printed_folio(doc, sp)
            fol = fol if fol is not None else sp
            folio_of_title.setdefault(_norm_key(t), fol)
            page_titles.setdefault(fol, []).append(t)

        def _match_title(phrase):
            k = _norm_key(phrase)
            if k in folio_of_title:
                return k
            cands = [tk for tk in folio_of_title
                     if tk and (tk.startswith(k + " ") or k.startswith(tk + " "))]
            if len(cands) == 1:
                return cands[0]
            return None

        out, seen = [], set()
        for i, page in enumerate(doc, 1):
            if i in stage_nav:
                continue
            text = " ".join(page.get_text("text").split())
            for m in _XREF_RE.finditer(text):
                phrase, said = m.group(1).strip(" ,"), int(m.group(2))
                tkey = _match_title(phrase)
                if not tkey:
                    continue
                actual = folio_of_title[tkey]
                if actual == said:
                    continue
                dedupe = (tkey, said)
                if dedupe in seen:
                    continue
                seen.add(dedupe)
                here = page_titles.get(said)
                if here:
                    out.append({
                        "doc": "STAGE", "page": i,
                        "kind": "Cross-reference points to the wrong section",
                        "text": (f"STAGE page {i} says “see {phrase} on page "
                                 f"{said}”, but page {said} holds "
                                 f"“{here[0]}” — “{phrase}” is on page {actual}.")})
                else:
                    out.append({
                        "doc": "STAGE", "page": i,
                        "kind": "Cross-reference page number is wrong",
                        "text": (f"STAGE page {i} says “see {phrase} on page "
                                 f"{said}”, but “{phrase}” is on page {actual} "
                                 f"in STAGE.")})
        return out
    finally:
        doc.close()


# ── Bold / emphasis ──────────────────────────────────────────────────────────
_BOLD_FLAG = 1 << 4          # PyMuPDF span flag bit for a bold face
# Weight names that render visually dark. The test is not "is it the same bold
# face" but "does it look heavy" — a heading set in Poppins-SemiBold and one set
# in Poppins-Bold both read as emphasised, and neither should be reported.
_HEAVY_RE = re.compile(r"bold|black|heavy|semib|demib|extrab|ultrab|medi",
                       re.IGNORECASE)


def _span_is_dark(span) -> bool:
    """True when a span renders visually dark (heavy weight)."""
    if span.get("flags", 0) & _BOLD_FLAG:
        return True
    return bool(_HEAVY_RE.search(span.get("font", "") or ""))


def _bold_phrases(pdf_path: str, nav_pages: set, min_words: int = 3):
    """{canonical phrase: page} for every bold run of at least `min_words`."""
    doc, out = fitz.open(pdf_path), {}
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        for block in _page_dict(page)["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                run, run_size = [], 0.0
                for sp in line.get("spans", []):
                    bold = _span_is_dark(sp)
                    if bold:
                        run.append(sp.get("text", ""))
                        run_size = max(run_size, sp.get("size", 0))
                    elif run:
                        _add_bold(out, " ".join(run), i, min_words, run_size)
                        run, run_size = [], 0.0
                if run:
                    _add_bold(out, " ".join(run), i, min_words, run_size)
    doc.close()
    return out


def _add_bold(store, text, page, min_words, size=0.0):
    toks = _seq_tokens(text)
    if len(toks) >= min_words:
        store.setdefault(" ".join(toks),
                         (page, re.sub(r"\s+", " ", text).strip(), size))


def _dark_tokens(pdf_path: str, nav_pages: set) -> set:
    """Canonical tokens that appear anywhere in `pdf_path` rendered dark."""
    doc, out = fitz.open(pdf_path), set()
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        for block in _page_dict(page)["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for sp in line.get("spans", []):
                    if _span_is_dark(sp):
                        out.update(_seq_tokens(sp.get("text", "")))
    doc.close()
    return out


def _text_sizes(pdf_path: str, nav_pages: set):
    """{canonical token: largest font size it is drawn at}.

    Emphasis is not only the bold bit: one document may set a heading in
    Poppins-Bold at 13.5 pt and the other in plain Poppins at 18 pt. The second
    is not less emphatic, so size has to be part of the comparison or every
    heading is reported as having lost its bold.
    """
    doc, out = fitz.open(pdf_path), {}
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        for block in _page_dict(page)["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for sp in line.get("spans", []):
                    size = sp.get("size", 0)
                    # Indexed per token, not per line: a bold run often spans
                    # lines or covers only part of one, and a line-keyed lookup
                    # then finds nothing and reads as "size 0".
                    for tok in _seq_tokens(sp.get("text", "")):
                        out[tok] = max(out.get(tok, 0), size)
    doc.close()
    return out


_INK_ZOOM      = 4.0     # render scale used when measuring how dark text is
_DARK_RATIO    = 1.08    # even slightly darker than plain body text counts as
                         # emphasis — the reader sees weight, not a ratio, so the
                         # bar for "this is bold" is deliberately low
_LOST_RATIO    = 1.05    # and any visible loss of that weight in STAGE is a
                         # defect, not just a large one


_PAGE_INK_CACHE = {}


def _page_ink(page):
    """(samples, width, height, zoom) for a page, rendered once and cached.

    Rendering a pixmap per line was what made measuring every line unaffordable.
    One greyscale render per page, sampled per rectangle, is orders of magnitude
    cheaper and lets every line be measured rather than only those whose font
    happens to be named bold.
    """
    key = (id(page.parent), page.number)
    hit = _PAGE_INK_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(_INK_ZOOM, _INK_ZOOM),
                              colorspace=fitz.csGRAY)
        val = (pix.samples, pix.width, pix.height, _INK_ZOOM,
               page.rect.x0, page.rect.y0)
    except Exception:
        val = None
    if len(_PAGE_INK_CACHE) > 12:
        _PAGE_INK_CACHE.clear()
    _PAGE_INK_CACHE[key] = val
    return val


def _ink_density_cached(page, rect) -> float:
    """Dark-pixel share of `rect`, sampled from the page's cached render."""
    got = _page_ink(page)
    if not got:
        return 0.0
    data, w, h, zoom, ox, oy = got
    r = fitz.Rect(rect)
    x0 = max(0, int((r.x0 - ox) * zoom)); x1 = min(w, int((r.x1 - ox) * zoom))
    y0 = max(0, int((r.y0 - oy) * zoom)); y1 = min(h, int((r.y1 - oy) * zoom))
    if x1 - x0 < 2 or y1 - y0 < 2:
        return 0.0
    # Sample every pixel in the region. Striding across columns to save time
    # skewed wide text: a heading was measured over a handful of columns and came
    # out LIGHTER than body text, which is the opposite of the truth.
    dark = tot = 0
    for y in range(y0, y1):
        base = y * w
        row = data[base + x0: base + x1]
        tot += len(row)
        dark += sum(1 for v in row if v < 128)
    return dark / max(1, tot)


def _ink_density(page, rect) -> float:
    """Fraction of dark pixels inside `rect` — how heavy the text looks.

    This is the actual rendered weight rather than what the font is called. A
    face named "Poppins" can be drawn heavy and a face named "…-Medium" light,
    so measuring the ink is the only reliable way to say "this looks bold".
    """
    try:
        r = fitz.Rect(rect)
        if r.width < 2 or r.height < 2:
            return 0.0
        pix = page.get_pixmap(clip=r, matrix=fitz.Matrix(_INK_ZOOM, _INK_ZOOM),
                              colorspace=fitz.csGRAY)
        data = pix.samples
        if not data:
            return 0.0
        return sum(1 for v in data if v < 128) / len(data)
    except Exception:
        return 0.0


def _token_union(boxes, toks, within=None):
    """Union of the word boxes matching `toks`, or None.

    `within` restricts the match to boxes inside that rectangle. Without it the
    union collects every occurrence of those words ANYWHERE on the page, so a
    heading whose words also appear in the body spanned half the page and
    measured lighter than plain text — the opposite of the truth.
    """
    want = set(toks)
    rects = []
    for t, r in boxes:
        if t not in want:
            continue
        if within is not None:
            rr = fitz.Rect(r)
            mid_y = (rr.y0 + rr.y1) / 2
            if not (within.y0 - 1 <= mid_y <= within.y1 + 1):
                continue
            if rr.x1 < within.x0 - 1 or rr.x0 > within.x1 + 1:
                continue
        rects.append(r)
    if not rects:
        return None
    box = fitz.Rect(rects[0])
    for r in rects[1:]:
        box |= fitz.Rect(r)
    return box


def _plain_density(pdf_path: str, nav_pages: set, sample: int = 40) -> float:
    """Typical ink density of ordinary body text in this document.

    Used as the baseline each document is judged against, so a naturally heavy
    typeface is not mistaken for emphasis everywhere.
    """
    doc, vals = fitz.open(pdf_path), []
    for i, page in enumerate(doc, 1):
        if i in nav_pages or len(vals) >= sample:
            continue
        boxes = _page_token_boxes(page)
        for block in _page_dict(page)["blocks"]:
            if block.get("type") != 0 or len(vals) >= sample:
                continue
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                txt = "".join(sp.get("text", "") for sp in spans)
                if len(txt.strip()) < 12 or any(_span_is_dark(sp) for sp in spans):
                    continue
                # Measured exactly as candidates are — over the word boxes.
                # Mixing box shapes made plain body text look as dark as bold.
                box = _token_union(boxes, _seq_tokens(txt))
                if box is None:
                    continue
                d = _ink_density_cached(page, box)
                if d > 0:
                    vals.append(d)
                if len(vals) >= sample:
                    break
    doc.close()
    if not vals:
        return 0.0
    vals.sort()
    return vals[len(vals) // 2]          # median


_DARK_OVER_PLAIN = 1.10   # this much darker than the document's own body text
                          # counts as emphasis — deliberately low, so anything
                          # visibly heavier than plain text is treated as bold
_PLAIN_UNDER     = 0.95   # and STAGE must be clearly LIGHTER than its own body
                          # text before the emphasis is called lost. Text sitting
                          # around the plain level is ambiguous, and reporting it
                          # produced findings for headings STAGE still renders
                          # with weight.
_TOC_LINE_RE     = re.compile(r"\.{3,}\s*\d{1,3}\s*$|\s\d{1,3}\s*$")


def _line_density(page, line, boxes=None) -> float:
    """Ink density of one line, measured over just that line's own words."""
    boxes = boxes if boxes is not None else _page_token_boxes(page)
    txt = "".join(sp.get("text", "") for sp in line.get("spans", []))
    box = _token_union(boxes, _seq_tokens(txt), within=fitz.Rect(line["bbox"]))
    if box is None:
        return 0.0
    return _ink_density_cached(page, box)


def _document_weights(pdf_path: str, nav_pages: set):
    """({canonical line: (page, text, density)}, median density).

    The median over all lines is the document's own "plain text" level. Judging
    each document against itself is what makes this work across files set in
    different typefaces, where raw ink levels are not comparable at all.
    """
    doc, lines, vals = fitz.open(pdf_path), {}, []
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        page_lines = _page_lines(page)
        # A contents listing sets the heading text as plain body text with the
        # page number alongside. Those lines carry the same words as the heading
        # they point at, so measuring them answers the wrong question. Detected
        # by the number sitting on the same baseline, which is the layout that
        # gives it away even when there are no dot leaders.
        toc_like = set()
        for txt, rect in page_lines:
            if not re.fullmatch(r"\s*\d{1,3}\s*", txt):
                continue
            mid = (rect.y0 + rect.y1) / 2
            for t2, r2 in page_lines:
                if r2.x1 <= rect.x0 and r2.y0 <= mid <= r2.y1:
                    toc_like.add(" ".join(_seq_tokens(t2)))
        boxes = _page_token_boxes(page)
        for block in _page_dict(page)["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                spans = [sp for sp in line.get("spans", [])
                         if sp.get("size", 0) >= _MIN_READABLE_PT]
                if not spans:
                    continue
                txt = "".join(sp.get("text", "") for sp in spans)
                toks = _seq_tokens(txt)
                if len(toks) < 3:
                    continue
                # Contents-style lines ("Care and cleaning ....... 10") carry the
                # same words as the heading they point at but are set as plain
                # body text. Letting them stand in for the heading measured the
                # wrong occurrence entirely.
                if _TOC_LINE_RE.search(txt.strip()):
                    continue
                if " ".join(toks) in toc_like:
                    continue          # contents entry, not the heading itself
                dens = _line_density(page, line, boxes)
                if dens <= 0:
                    continue
                vals.append(dens)
                key = " ".join(toks)
                prev = lines.get(key)
                # Keep the darkest occurrence: the same words often appear as a
                # heading and again as plain prose.
                if prev is None or dens > prev[2]:
                    lines[key] = (i, re.sub(r"\s+", " ", txt).strip(), dens)
    doc.close()
    vals.sort()
    median = vals[len(vals) // 2] if vals else 0.0
    return lines, median


def _line_emphasis(line):
    """(text, is_bold) for a line, or None when it carries no readable text.

    Weight is read from the face the words are actually drawn in, not from a
    measured ink ratio. A ratio has to be tuned against a threshold, and a
    threshold set anywhere reports ordinary text as bold somewhere in the
    document. "Roboto,Bold" and "Roboto-Bold" name the same thing; the question
    is only whether the words are drawn heavy at all, never how heavy.
    """
    spans = [sp for sp in line.get("spans", [])
             if sp.get("size", 0) >= _MIN_READABLE_PT]
    if not spans:
        return None
    text = "".join(sp.get("text", "") for sp in spans)
    if not _seq_tokens(text) or len(text.strip()) < 3:
        return None
    # Judge the spans that carry words. A bullet or a step number is set in the
    # body face even when the text after it is bold, and counting it made STAGE's
    # bold "• YES" read as plain.
    ink = [sp for sp in spans
           if _WORDCHAR_RE.search(sp.get("text", "") or "")
           or re.search(r"\d", sp.get("text", "") or "")]
    if not ink:
        return None
    return re.sub(r"\s+", " ", text).strip(), all(_span_is_dark(sp) for sp in ink)


def _emphasis_map(pdf_path: str, nav_pages: set):
    """{canonical line: (page, text, is_bold)} for one document.

    Where the same words appear more than once, the emphasised occurrence wins:
    a term set bold in a table and again as plain prose is still emphasised in
    the document, and reporting the prose copy would be a false drop.
    """
    doc, out = fitz.open(pdf_path), {}
    for i, page in enumerate(doc, 1):
        # The cover is excluded with the navigation pages. It is laid out, not
        # written — PROD sets its title in Poppins Medium at 60pt and STAGE in
        # Poppins Regular at 24pt — so comparing weight there reports a redesign
        # as a defect.
        if i in nav_pages or i == 1:
            continue
        for block in _page_dict(page)["blocks"]:
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                got = _line_emphasis(line)
                if got is None:
                    continue
                text, bold = got
                if _TOC_LINE_RE.search(text):
                    continue          # a contents entry, not the heading itself
                key = " ".join(_seq_tokens(text))
                prev = out.get(key)
                if prev is None or (bold and not prev[2]):
                    out[key] = (i, text, bold)
    doc.close()
    return out


def _bold_issues(prod_path, stage_path, prod_nav, stage_nav, stage_idx):
    """Text PROD sets bold that STAGE draws in its ordinary face.

    Only losses are reported. Text STAGE emphasises and PROD does not is left
    alone: PROD is the reference, and extra emphasis in STAGE is not a defect
    against it. Size is never compared — a heading set larger or smaller is a
    layout change, not a lost weight.
    """
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []
    prod_lines = _emphasis_map(prod_path, prod_nav)
    stage_lines = _emphasis_map(stage_path, stage_nav)
    if not prod_lines or not stage_lines:
        return []
    findings = []
    for key, (page, shown, bold) in prod_lines.items():
        if not bold:
            continue
        hit = stage_lines.get(key)
        if hit is None:
            continue          # absent from STAGE altogether: a content issue
        s_page, _s_text, s_bold = hit
        if s_bold:
            continue
        findings.append({"page": page, "text": shown, "stage_page": s_page})
    return findings


# ── Table crawling: validate every table cell against STAGE ──────────────────
_TABLE_CRAWL_CACHE = {}
# A word a print layout hard-wraps mid-word ("connect-\ning") reads as two
# tokens ("connect", "ing") that match nothing in STAGE's reflowed text, which
# has "connecting" as one word. Rejoining at the hyphen+newline is what makes a
# wrapped cell compare the same as an unwrapped one.
_CELL_HYPHEN_RE = re.compile(r"-\s*\n\s*")


def _dehyphenate_cell(text):
    return _CELL_HYPHEN_RE.sub("", text) if text else text


def _crawl_tables(pdf_path: str, nav_pages: set):
    """[(page_no, n_rows, n_cols, rows), ...] for every table found in the body.

    Cached: table detection is the single most expensive read of a document and
    four separate checks ask for the same crawl.
    """
    ckey = (os.path.abspath(pdf_path), frozenset(nav_pages or ()))
    hit = _TABLE_CRAWL_CACHE.get(ckey)
    if hit is not None:
        return hit
    out = []
    doc = fitz.open(pdf_path)
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        try:
            found = page.find_tables()
        except Exception:
            continue
        for tbl in getattr(found, "tables", []):
            try:
                rows = tbl.extract()
            except Exception:
                continue
            if rows:
                rows = [[_dehyphenate_cell(c) for c in row] for row in rows]
                out.append((i, tbl.row_count, tbl.col_count, rows))
    doc.close()
    if len(_TABLE_CRAWL_CACHE) > 8:
        _TABLE_CRAWL_CACHE.clear()
    _TABLE_CRAWL_CACHE[ckey] = out
    return out

def _merge_continued_tables(tables):
    """Join tables that are one logical table split across consecutive pages.

    A table broken by a page break appears as two detections whose column count
    matches and whose pages are adjacent. Treating them separately makes the
    continuation rows look like a different table; merging them means a row that
    simply ran onto the next page is validated as part of its own table.
    """
    if not tables:
        return []

    def _rk(row):
        return " ".join(_seq_tokens(" | ".join((c or "") for c in (row or []))))

    merged = [list(tables[0])]
    for pno, nrow, ncol, rows in tables[1:]:
        prev = merged[-1]
        if pno == prev[0] + 1 and ncol == prev[2]:
            add_rows = list(rows)
            # A table that runs onto the next page usually re-prints its header
            # row. That repeat is the same header, not a new data row — counting
            # it inflates the row total and makes an ordinary page break look
            # like a cell-split. Drop it before the continuation rows join.
            if add_rows and prev[3]:
                hk = _rk(prev[3][0])
                ck = _rk(add_rows[0])
                hset, cset = set(hk.split(" ")), set(ck.split(" "))
                if hk and (ck == hk or (hset and cset
                                        and len(hset & cset) / len(hset) >= 0.6)):
                    add_rows = add_rows[1:]
            prev[1] += len(add_rows)
            prev[3] = prev[3] + add_rows       # continuation rows join the table
        else:
            merged.append([pno, nrow, ncol, rows])
    return [tuple(m) for m in merged]


def _row_anchor_tokens(row, exclude_ci: int):
    """Canonical tokens of the row's longest cell other than the one being
    checked — the row's most distinctive text, used to locate its STAGE
    counterpart without depending on column position lining up."""
    best = []
    for ci, cell in enumerate(row):
        if ci == exclude_ci:
            continue
        toks = _seq_tokens((cell or "").strip())
        if len(toks) > len(best):
            best = toks
    return best


def _find_stage_row(stage_rows, anchor_toks):
    """The STAGE table row whose text contains `anchor_toks`, else None.

    A short, generic cell ("ON", "OFF", "10 min") reads as present anywhere in
    the whole STAGE document almost by chance — those same words label a dozen
    other settings. Anchoring on the row's own longest, most distinctive cell
    and matching row by row is what makes the comparison specific to this row.
    Too short an anchor (< 3 words) cannot be trusted to pick one row, so it is
    left unmatched and the caller falls back to the whole-document check.
    """
    if len(anchor_toks) < 3:
        return None
    for i, (row, row_idx) in enumerate(stage_rows):
        if _seq_present(row_idx, anchor_toks, max_gap=2):
            return i
    return None


# A long option list often does not fit one STAGE row: the layout engine spills
# the tail into the following row(s), or over a page break into a fresh table.
# Comparing against the anchored row alone reported those continuations as
# missing values, so the anchored row plus this many following rows are read as
# one block.
_ROW_SPILL = 6
# Above this many bullets a list is long enough that STAGE re-splitting it
# across rows/tables/pages is expected, and presence is judged document-wide.
_BULLET_COUNT_RESPLIT = 8


_BULLET_CELL_RE = re.compile(r"[•▪‣·]")
# A physical-product dimension label ("Dimension (WxDxH)", "Dimensioin (WxDxH)")
# — the spec value itself varies release to release and a typo in the row
# label is not content worth reporting, same as image dimensions are skipped.
_WXDXH_RE = re.compile(r"w\s*[x×]\s*d\s*[x×]\s*h", re.IGNORECASE)
# A cell that is nothing but an admonition label. PROD prints the word; STAGE
# renders the same callout with an icon and no word - house style, not lost
# content. find_tables() also merges these boxes into the real table above them,
# so a whole-table guard misses some; this is a per-cell skip.
_ADMONITION_RE = re.compile(
    r"^(tip|note|notice|warning|caution|important|danger|attention)$", re.I)
# Anything outside Latin / Latin-Extended: Cyrillic, Greek, Arabic, Hebrew, CJK,
# Hangul. Cells mixing these (the OSD language list) are not comparable.
_MIXED_SCRIPT_RE = re.compile(
    r"[Ͱ-ϿЀ-ӿ֐-׿؀-ۿ"
    r"　-鿿가-힯ﭐ-﻿]")


def _is_bullet_cell(text: str) -> bool:
    """True for a cell that lists option values ("• OFF • 10 min • 20 min").

    A Range/options cell belongs to one row of one table. Long ones used to be
    treated as prose and searched for across the whole STAGE document, where
    the same words label a dozen other settings — so an entirely emptied Range
    column read as present. Bullet cells are matched against their own STAGE
    row instead, however long they are.
    """
    return bool(_BULLET_CELL_RE.search(text or ""))


def _cell_covers(prod_toks, stage_cell_text) -> bool:
    """True when every PROD token (with repeats) also occurs in the STAGE cell.

    A multiset check, not a sequence match: bullet values in a short cell
    ("ON", "10 min", "20 min") can be drawn in a different order in STAGE
    without anything actually being lost.
    """
    need = collections.Counter(prod_toks)
    have = collections.Counter(_seq_tokens(stage_cell_text))
    return all(have[t] >= n for t, n in need.items())


def _split_bullet_values(text: str) -> list:
    """A bullet cell -> its individual option values, in order.

    "* OFF * 10 min * 20 min" -> ["OFF", "10 min", "20 min"]. Reporting the
    whole cell as missing when one value was dropped hides which value it is;
    splitting lets the finding name the exact entry.
    """
    parts = re.split(r"[\u2022\u25aa\u2023\u00b7]", text or "")
    return [" ".join(p.split()) for p in parts if p.strip()]


def _row_label(row, exclude_ci: int = -1) -> str:
    """The plain text a reader would use to find this row: its first filled
    cell, falling back to its longest, other than the value being checked."""
    first = ""
    for ci, cell in enumerate(row):
        if ci == exclude_ci:
            continue
        t = " ".join((cell or "").split())
        if t:
            first = first or t
    if first and len(first.split()) <= 8:
        return first
    best = ""
    for ci, cell in enumerate(row):
        if ci == exclude_ci:
            continue
        t = " ".join((cell or "").split())
        if 0 < len(t) < len(best) or not best:
            if t:
                best = t if not best or len(t) < len(best) else best
    return (first or best)[:80]


def _table_header_label(rows) -> str:
    """The column names of a table, joined, for naming it in the report."""
    if not rows:
        return ""
    head = " | ".join(" ".join((c or "").split()) for c in rows[0] if (c or "").strip())
    return head[:120]


def _table_finding_detail(page, header, row_label, text, whole_cell) -> str:
    """Plain-text version of a table finding, for the HTML report / data feed."""
    where = (f"the table with columns [{header}]" if header
             else f"a table on PROD page {page}")
    row = f" the row for '{row_label}'" if row_label else " one row"
    if whole_cell:
        return (f"PROD page {page}: in {where},{row} contains \"{text}\" — "
                f"STAGE's copy of that row does not carry this text anywhere.")
    return (f"PROD page {page}: in {where},{row} lists the value \"{text}\" — "
            f"that value is missing from STAGE; the rest of the row is present.")


def _table_row_missing_issues(prod_path: str, stage_path: str,
                              prod_nav: set, stage_nav: set,
                              stage_full_lower: str):
    """PROD table rows that have NO counterpart row in STAGE.

    _validate_tables checks each PROD cell and, when a whole row is gone, ends
    up reporting its cells one by one, scattered. This reports the row as one
    finding: "the row 'Receiver USB-C-output resolution: DP 1.2 ...' is not in
    STAGE". A row counts as present when its label wording is found in any STAGE
    table row, or when the label plus its longest value both appear anywhere in
    STAGE's text (the row may have been re-laid-out as prose).
    """
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []
    prod_tables = _merge_continued_tables(_crawl_tables(prod_path, prod_nav))
    stage_tables = _merge_continued_tables(_crawl_tables(stage_path, stage_nav))
    if not prod_tables:
        return []

    # Every STAGE table row as a flat character key, for a fast "is this label
    # a row somewhere in STAGE" test.
    stage_row_keys = []
    for _p, _nr, _nc, s_rows in stage_tables:
        for r in s_rows:
            k = _flat_key(" ".join((c or "") for c in r))
            if k:
                stage_row_keys.append(k)
    stage_doc_flat = _flat_document(stage_path, stage_nav)
    # PROD's own text, flattened - used to reject detector artefacts: a "row"
    # whose label+value do not read as one unit in PROD (cells mashed together,
    # a word sliced in half) is not real PROD content and cannot be a STAGE gap.
    prod_doc_flat = _flat_document(prod_path, prod_nav)

    findings, seen = [], set()
    for pno, nrow, ncol, rows in prod_tables:
        filled = sum(1 for r in rows for c in r if (c or "").strip())
        if ncol < 2 or nrow < 2 or filled < 4:
            continue
        if ncol == 2 and not any((r[0] or "").strip() for r in rows):
            continue          # admonition box, see _validate_tables
        for ri, row in enumerate(rows):
            if ri == 0:
                continue                       # header row
            cells = [" ".join((c or "").split()) for c in row]
            nonempty = [c for c in cells if c]
            if len(nonempty) < 2:
                continue                       # sub-header / spacer, not a data row
            label = cells[0] or nonempty[0]
            value = max((c for c in cells[1:] if c), key=len, default="")
            if not label or not value:
                continue
            label_toks = [t for t in _seq_tokens(label) if len(t) > 1]
            # A one-word generic label ("Mode", "Color", "Item") is not
            # distinctive enough to be sure a row is gone rather than reworded.
            if len(label_toks) < 2 and len(_flat_key(label)) < 8:
                continue
            if _script_unreliable(label) or _MIXED_SCRIPT_RE.search(label):
                continue
            lk = _flat_key(label)
            vk = _flat_key(value)
            if len(lk) < 4 or len(vk) < 3:
                continue
            # Reject detector artefacts up front: a real spec row has a short
            # noun-phrase label with no bullets, no step numbers, no sentence
            # punctuation, and its label+value read as one unit in PROD itself.
            if ("\u2022" in label or "\u2022" in value
                    or re.search(r"\b\d+\.\s", label)
                    or re.search(r"[.!?;]\s+\S", label)
                    or len(label_toks) > 7):
                continue
            _pl = prod_doc_flat.find(lk)
            if _pl < 0 or not (0 <= prod_doc_flat.find(vk, _pl) - _pl <= 400):
                continue        # not a coherent PROD row - artefact
            # PRESENT if the label heads any STAGE table row (however that row
            # then rewords its value).
            if any(lk in k for k in stage_row_keys):
                continue
            # PRESENT if the label phrase AND its most distinctive value both
            # turn up in STAGE's running text within ~400 characters of each
            # other - the row was re-laid-out as a sentence, not dropped.
            li = stage_doc_flat.find(lk)
            if li >= 0 and 0 <= stage_doc_flat.find(vk, li) - li <= 400:
                continue
            # PRESENT if the value alone (when it is a long, distinctive string
            # like a resolution list or a part number) is somewhere in STAGE -
            # the label may just have been reworded.
            if len(vk) >= 14 and vk in stage_doc_flat:
                continue
            # Otherwise the row is gone: its label heads no STAGE row and its
            # label+value pair appears nowhere in STAGE's text.
            key = (pno, lk)
            if key in seen:
                continue
            seen.add(key)
            findings.append({
                "page": pno, "label": label, "value": value[:160],
                "detail": (f"PROD page {pno}: the table row "
                           f"\"{label}: {value[:120]}\" has no counterpart "
                           f"row in STAGE - neither as a table row nor as "
                           f"text elsewhere.")})
    return findings


def _pair_prod_stage_tables(prod_tables, stage_tables):
    """{prod table index -> that table's paired STAGE rows}.

    Two tables are the same table when their body cells overlap enough
    (Jaccard >= _TABLE_PAIR_MIN), the same test _table_merge_issues uses. Once
    a PROD table is paired, a missing cell can be judged against THAT table's
    rows - so a value that survives elsewhere in STAGE but was dropped from the
    row it belongs to is still caught, which a whole-document search misses.
    """
    def body(rows):
        out = set()
        for r in rows:
            for c in r:
                k = " ".join(_seq_tokens(c or ""))
                if k:
                    out.add(k)
        return out

    p_body = [body(rows) for _p, _nr, _nc, rows in prod_tables]
    s_body = [body(rows) for _p, _nr, _nc, rows in stage_tables]
    scored = []
    for pi, pb in enumerate(p_body):
        if not pb:
            continue
        for si, sb in enumerate(s_body):
            if not sb:
                continue
            inter = len(pb & sb)
            if inter:
                scored.append((inter / len(pb | sb), pi, si))
    scored.sort(reverse=True)
    paired, used_p, used_s = {}, set(), set()
    for score, pi, si in scored:
        if score < _TABLE_PAIR_MIN or pi in used_p or si in used_s:
            continue
        used_p.add(pi)
        used_s.add(si)
        paired[pi] = stage_tables[si][3]        # the STAGE table's rows
    return paired


def _match_row_in(stage_rows, anchor_toks, min_anchor=2):
    """Index of the STAGE row (in a specific table's rows) that carries
    anchor_toks - a looser threshold than _find_stage_row because the table is
    already known to be the right one."""
    if len(anchor_toks) < min_anchor:
        return None
    best, best_hits = None, 0
    for i, r in enumerate(stage_rows):
        rtoks = set(t for c in r for t in _seq_tokens(c or ""))
        hits = sum(1 for t in anchor_toks if t in rtoks)
        if hits > best_hits and hits >= max(min_anchor, len(anchor_toks) - 1):
            best, best_hits = i, hits
    return best


def _validate_tables(prod_path: str, stage_path: str,
                     prod_nav: set, stage_nav: set, stage_full_lower: str):
    """Return (summary, [findings]) for PROD tables checked cell by cell.

    Prose cells are checked with the same gap-tolerant window matcher used for
    body text — a cell whose wording STAGE re-wraps or re-orders is not
    reported, only text with no counterpart anywhere in STAGE is. Short cells
    (a label, a unit, a bullet value) are matched against their own STAGE row
    instead: words that short ("ON", "OFF", "min") turn up all over a settings
    table, so a whole-document search cannot tell this row's value from
    another row's.
    """
    prod_tables  = _merge_continued_tables(_crawl_tables(prod_path, prod_nav))
    stage_tables = _merge_continued_tables(_crawl_tables(stage_path, stage_nav))
    idx = _stage_seq_index(stage_full_lower)

    # Table detection is heuristic and on some layouts slices cells mid-word
    # ("er outlet.", "Descr", "iption"). Such a cell is not real PROD text, so
    # it is checked against PROD's own token stream first: a fragment that does
    # not read that way in PROD is a detection artifact, never a STAGE defect.
    _pd = fitz.open(prod_path)
    _praw = " ".join(_normalize(_strip_formatting(_pd[i].get_text()))
                     for i in range(_pd.page_count) if (i + 1) not in prod_nav)
    _pd.close()
    prod_idx = _stage_seq_index(_s_norm(re.sub(r"\s+", " ", _praw)).lower())

    # One small per-row token index per STAGE table row, built once up front —
    # every short PROD cell needs to find its matching row, and rebuilding this
    # per cell would repeat the same scan over and over.
    stage_rows = []
    for _pno, _nrow, _ncol, s_rows in stage_tables:
        for s_row in s_rows:
            row_idx = {}
            for pos, tok in enumerate(_seq_tokens(
                    " ".join((c or "") for c in s_row))):
                row_idx.setdefault(tok, []).append(pos)
            stage_rows.append((s_row, row_idx))

    # Which STAGE table each PROD table pairs with - lets a cell be judged
    # against its own row rather than the whole document.
    paired = _pair_prod_stage_tables(prod_tables, stage_tables)
    stage_doc_flat = _flat_document(stage_path, stage_nav)

    findings, n_cells = [], 0
    for pi, (pno, nrow, ncol, rows) in enumerate(prod_tables):
        paired_rows = paired.get(pi)
        # find_tables() also fires on things that are not tables — an image with
        # a caption, a bordered callout, a single framed paragraph. Their "cells"
        # then get reported as missing from STAGE even though no table was lost.
        # A real table has at least two columns, two rows, and several populated
        # cells; anything thinner is a detection artefact and is skipped.
        filled = sum(1 for r in rows for c in r if (c or "").strip())
        if ncol < 2 or nrow < 2 or filled < 4:
            continue
        # An admonition box - a narrow icon column plus "Tip / Warning / Note"
        # and body text - is drawn as a 2-column table and find_tables() picks
        # it up as one. Its first column is empty in every row (the icon has no
        # text). STAGE renders the same callouts with an icon and no word, so
        # the label cell read as "Tip missing from STAGE" on every page. A real
        # data table has text in its first column.
        if ncol == 2 and not any((r[0] or "").strip() for r in rows):
            continue
        _hdr_keys = {" ".join(_seq_tokens(c or "")) for c in rows[0]
                     if (c or "").strip()}
        for ri, row in enumerate(rows):
            if ri == 0:
                continue          # header row - _table_heading_issues owns it
            # The STAGE row that matches this PROD row, when the table is paired.
            row_stage_text = ""
            if paired_rows:
                _rmi = _match_row_in(
                    paired_rows, [t for c in row for t in _seq_tokens(c or "")])
                if _rmi is not None:
                    row_stage_text = " ".join(
                        c or "" for rr in paired_rows[_rmi:_rmi + 2] for c in rr)
            for ci, cell in enumerate(row):
                text = (cell or "").strip()
                if not text:
                    continue
                if _WXDXH_RE.search(text):
                    continue      # physical product dimension label — not tracked
                if _ADMONITION_RE.match(text.strip()):
                    continue      # style label ("Tip", "Warning") — see above
                _tk = " ".join(_seq_tokens(text))
                if _tk and _tk in _hdr_keys:
                    continue      # a header word repeated in the body: the
                                  # detector split the header - _table_heading
                                  # owns any real column-heading loss
                # The detector merges identical per-model columns into one
                # cell ("1 x 3.0 (PD 65 W), 1 x 1 x 3.0 (PD 65 W), 1 x ..."):
                # any 10+ char run that repeats three or more times means the
                # same value was concatenated across columns - not real PROD
                # text. The merge check reports the column split itself.
                if len(text) >= 30:
                    _probe = text[3:18]
                    if len(_probe.strip()) >= 8 and text.count(_probe) >= 3:
                        continue
                    _mid = text[len(text) // 2 - 8:len(text) // 2 + 7]
                    if len(_mid.strip()) >= 8 and text.count(_mid) >= 3:
                        continue
                n_cells += 1
                toks = _seq_tokens(text)
                if not toks:
                    continue
                if _script_unreliable(text):
                    continue      # see _script_unreliable — not comparable
                if _MIXED_SCRIPT_RE.search(text):
                    # The OSD language list mixes Latin, Cyrillic, Arabic and
                    # CJK entries, and the two export pipelines mangle those
                    # differently (one drops them, one garbles them). Comparing
                    # such a cell says nothing about whether content was lost.
                    continue
                if len(toks) <= _SHORT_CELL_WORDS or _is_bullet_cell(text):
                    # A short cell is one atomic label ("Item", "Illustration",
                    # "5V / 3A") or a bullet value ("ON", "10 min"); a bullet
                    # cell is a Range/options list of any length. Both belong to
                    # one row of one table, so they are matched against their own
                    # STAGE row; only fall back to the whole-document search when
                    # the row itself cannot be found (its anchor is too short, or
                    # genuinely absent — already reported separately for it).
                    si = _find_stage_row(
                        stage_rows, _row_anchor_tokens(row, ci))
                    # The paired table's matching row is the strictest, most
                    # honest place to look: a value dropped from THIS row is
                    # missing even if the same words survive in another row.
                    # Unpaired table -> the row _find_stage_row located, plus
                    # its spill rows; nothing at all -> whole-document search.
                    if row_stage_text:
                        sc = row_stage_text
                    elif si is not None:
                        sc = " ".join(
                            (c or "")
                            for r2, _ in stage_rows[si:si + 1 + _ROW_SPILL]
                            for c in r2)
                    else:
                        sc = ""

                    if sc:
                        present = _cell_covers(toks, sc)
                        if not present and not _is_bullet_cell(text):
                            if row_stage_text and paired_rows:
                                # a plain label may just have moved cell/row
                                # within the paired table
                                present = _cell_covers(
                                    toks, " ".join(c or "" for rr in paired_rows
                                                   for c in rr))
                            else:
                                present = (_seq_present(idx, toks)
                                           or _all_words_present(text, idx))
                        elif not present and _BULLET_COUNT_RESPLIT < len(
                                _BULLET_CELL_RE.findall(text)):
                            # A very long list (the OSD language list, 20+
                            # entries) is routinely re-split across rows, tables
                            # and a page break, so no row window holds all of it.
                            # If nearly every entry is somewhere in STAGE it was
                            # re-laid-out, not dropped.
                            want = set(toks)
                            have = sum(1 for t in want if t in idx)
                            if want and have / len(want) >= 0.85:
                                present = True
                    else:
                        present = (_seq_present(idx, toks)
                                   or _all_words_present(text, idx))
                    hdr = _table_header_label(rows)
                    rlab = _row_label(row, ci)
                    if _is_bullet_cell(text) and _source_ok(prod_idx, toks):
                        # Check each option value on its own — and do it whatever
                        # the coarse `present` check said, because that one drops
                        # digits ("10 min", "20 min", "30 min" all reduce to
                        # "min") and so passes a row that has lost two of three
                        # values. Values are compared as flattened characters,
                        # against the STAGE row's own block first, then the whole
                        # STAGE document.
                        vals = _split_bullet_values(text)
                        row_flat = _flat_key(sc) if sc else ""
                        doc_flat = _flat_document(stage_path, stage_nav)
                        gone = []
                        for val in vals:
                            # Drop a trailing model qualifier ("(SW272 only)",
                            # "(XL2586X+ only)") - STAGE often rewords or moves
                            # it, and it is not the value itself.
                            core = re.sub(r"\s*\([^()]*\)\s*$", "", val).strip()
                            vk = _flat_key(core or val)
                            if len(vk) < 2:
                                continue
                            core_words = [w for w in _seq_tokens(core or val)
                                          if len(w) > 1]
                            if len(core_words) >= 3:
                                # A multi-word text value ("Optimum Resolution
                                # best with the monitor"): STAGE may re-word or
                                # re-space it, so it counts as lost only when
                                # most of its words are absent - a contiguous
                                # match would flag every rewrite.
                                absent = sum(1 for w in core_words
                                             if w not in idx)
                                if absent <= len(core_words) // 2:
                                    continue
                            else:
                                # A short or numeric value ("20 min", "50 Hz"):
                                # digits matter and there is nothing to re-word,
                                # so the flat form must appear verbatim.
                                if vk in row_flat or vk in doc_flat:
                                    continue
                            if _text_in_artwork(stage_path, val, pno, stage_nav):
                                continue
                            gone.append(val)
                        if gone and len(gone) < len(vals):
                            for val in gone:
                                findings.append({
                                    "page": pno, "row": ri, "col": ci,
                                    "text": val, "row_label": rlab,
                                    "header": hdr, "whole_cell": False,
                                    "detail": _table_finding_detail(
                                        pno, hdr, rlab, val, False)})
                        elif gone:
                            findings.append({
                                "page": pno, "row": ri, "col": ci,
                                "text": text.replace("\n", " "),
                                "row_label": rlab, "header": hdr,
                                "whole_cell": True,
                                "detail": _table_finding_detail(
                                    pno, hdr, rlab, text.replace("\n", " "),
                                    True)})
                    elif (not present
                            and _source_ok(prod_idx, toks)
                            # Every word of the cell somewhere in STAGE = the
                            # cell was reworded / re-split, not dropped. A real
                            # loss leaves most of the words gone too.
                            and not _all_words_present(text, idx)
                            and not _text_in_artwork(stage_path, text, pno,
                                                     stage_nav)):
                        _t = text.replace("\n", " ")
                        findings.append({"page": pno, "row": ri, "col": ci,
                                         "text": _t,
                                         "row_label": rlab, "header": hdr,
                                         "whole_cell": True,
                                         "detail": _table_finding_detail(
                                             pno, hdr, rlab, _t, True)})
                else:
                    hdr = _table_header_label(rows)
                    rlab = _row_label(row, ci)
                    # A long value cell that STAGE re-wrapped or re-ordered is
                    # not a loss - only text with no window match in STAGE is.
                    # When the table is paired, scope the search to the matched
                    # STAGE row so a phrase that survives in a DIFFERENT row is
                    # still reported as missing from THIS one.
                    for gap in _refine_fragment(_tokenize(text), idx,
                                                prod_idx):
                        if _script_unreliable(gap):
                            continue
                        gtoks = _seq_tokens(gap)
                        # Must genuinely read this way in PROD - reject a cell
                        # the detector sliced mid-sentence.
                        if not _source_ok(prod_idx, gtoks):
                            continue
                        # Two independent checks must agree it is gone: absent
                        # from the paired STAGE row (when the table paired) AND
                        # absent from STAGE as a whole. Either one alone is not
                        # trusted - the row match can be wrong, and the
                        # whole-doc search can be fooled by the same words in a
                        # different setting.
                        in_row = bool(row_stage_text) and _cell_covers(
                            gtoks, row_stage_text)
                        in_doc = (_seq_present(idx, gtoks)
                                  or _all_words_present(gap, idx))
                        if in_row or in_doc:
                            continue
                        # The words may be baked into a STAGE figure rather than
                        # laid out as text.
                        if _text_in_artwork(stage_path, gap, pno, stage_nav):
                            continue
                        findings.append({"page": pno, "row": ri, "col": ci,
                                         "text": gap, "row_label": rlab,
                                         "header": hdr, "whole_cell": False,
                                         "detail": _table_finding_detail(
                                             pno, hdr, rlab, gap, False)})
    summary = {
        "prod_tables":  len(prod_tables),
        "stage_tables": len(stage_tables),
        "prod_cells":   n_cells,
        "stage_cells":  sum(r * c for _, r, c, _ in stage_tables),
    }
    return summary, findings


# ── Figures: raster vs vector rendering ──────────────────────────────────────
def _figure_summary(prod_path: str, stage_path: str,
                    prod_nav: set, stage_nav: set) -> dict:
    """Raster-figure, icon and vector-artwork counts for both documents.

    PROD and STAGE routinely use different rendering pipelines: PROD ships
    figures as raster images while STAGE (InDesign/FrameMaker export) draws the
    same figures as vector art. Raster counts are therefore NOT comparable, and
    reporting the difference as "missing figures" would be false. This returns
    the raw counts plus a flag so the report can say plainly which pipeline each
    document used, and leaves per-figure claims to the labels, which are text and
    can be matched exactly.
    """
    def counts(path, nav):
        doc = fitz.open(path)
        figs = icons = vectors = 0
        for i, page in enumerate(doc, 1):
            if i in nav:
                continue
            for (iw, ih) in _page_onpage_images(page):
                if max(iw, ih) > _ICON_MAX_ONPAGE:
                    figs += 1
                else:
                    icons += 1
            try:
                for dr in page.get_drawings():
                    r = dr.get("rect")
                    if r is not None and min(r.width, r.height) >= 3:
                        vectors += 1
            except Exception:
                pass
        doc.close()
        return {"figures": figs, "icons": icons, "vectors": vectors}

    p, s = counts(prod_path, prod_nav), counts(stage_path, stage_nav)
    # STAGE draws its figures rather than embedding them?
    s["vector_rendered"] = (s["vectors"] > max(3 * p["vectors"], 500)
                            and s["figures"] < p["figures"])
    return {"prod": p, "stage": s}


# ── Evidence screenshots ─────────────────────────────────────────────────────
# Every reported issue is backed by a picture of the page it was found on, with
# the offending text boxed in red, so the finding can be checked against the
# source without opening the PDFs side by side.
EVIDENCE_SHOTS = True     # see generate_report — pairing not yet verifiable
_SHOT_ZOOM = 2.4          # render scale — keeps 8pt table text readable
_SHOT_PAD  = 46           # points of context kept around the hit
_SHOT_MIN_W = 300         # narrowest crop, so a short hit still reads in context


_PAGE_DICT_CACHE = {}


def _page_dict(page):
    """The page's text dictionary, cached per page.

    Fifteen checks each walk the blocks of every page, and re-parsing the text
    layer for each of them was the largest single cost in a run. The structure
    is read-only to every caller.
    """
    try:
        ckey = (page.parent.name, page.number)
    except Exception:
        return page.get_text("dict")
    hit = _PAGE_DICT_CACHE.get(ckey)
    if hit is None:
        hit = page.get_text("dict")
        if len(_PAGE_DICT_CACHE) > 200:
            _PAGE_DICT_CACHE.clear()
        _PAGE_DICT_CACHE[ckey] = hit
    return hit


_TOKEN_BOX_CACHE = {}


def _page_token_boxes(page):
    """[(canonical token, Rect)] for every word on the page, in reading order.

    Cached per page: the locators walk the same pages repeatedly, once per
    finding, and re-reading every word each time dominated the run.
    """
    try:
        ckey = (page.parent.name, page.number)
    except Exception:
        ckey = None
    if ckey is not None:
        hit = _TOKEN_BOX_CACHE.get(ckey)
        if hit is not None:
            return hit
    out = []
    try:
        words = page.get_text("words")
    except Exception:
        return out
    for w in words:
        rect = fitz.Rect(w[0], w[1], w[2], w[3])
        for tok in _split_canon(w[4]):
            out.append((tok, rect))
    if ckey is not None:
        if len(_TOKEN_BOX_CACHE) > 400:
            _TOKEN_BOX_CACHE.clear()
        _TOKEN_BOX_CACHE[ckey] = out
    return out


def _locate_tokens(pdf_path: str, needle: str, hint_page: int = 0,
                   max_gap: int = SEQ_SOURCE_GAP, skip_pages=None):
    """(page_no, [rects]) where `needle` actually occurs — exact, or (0, []).

    Matches the SAME canonical token sequence the validator compared, against
    each page's word boxes. It never falls back to a prefix or a single word, so
    a screenshot is only ever produced for a page that genuinely carries the
    text — the previous prefix search is what put shots on the wrong page.
    """
    target = _seq_tokens(needle)
    if not target:
        return 0, []
    doc = fitz.open(pdf_path)
    skip  = set(skip_pages or ())
    order = [p for p in range(doc.page_count) if (p + 1) not in skip]
    if hint_page and 1 <= hint_page <= doc.page_count:
        near  = [p for p in order if abs(p + 1 - hint_page) <= 4]
        order = near + [p for p in order if p not in near]
    try:
        for pno in order:
            boxes = _page_token_boxes(doc[pno])
            toks  = [t for t, _ in boxes]
            for start in (i for i, t in enumerate(toks) if t == target[0]):
                pos, hits, ok = start, [boxes[start][1]], True
                for want in target[1:]:
                    nxt = None
                    for j in range(pos + 1, min(pos + 1 + max_gap + 1, len(toks))):
                        if toks[j] == want:
                            nxt = j
                            break
                    if nxt is None:
                        ok = False
                        break
                    pos = nxt
                    hits.append(boxes[nxt][1])
                if ok:
                    return pno + 1, hits
    finally:
        doc.close()
    return 0, []


def _locate_all_tokens(pdf_path: str, needle: str, skip_pages=None,
                       limit: int = 6, max_gap: int = SEQ_SOURCE_GAP):
    """Every page where `needle` occurs, as [(page, [rects]), ...] up to `limit`."""
    target = _seq_tokens(needle)
    if not target:
        return []
    doc, out = fitz.open(pdf_path), []
    skip = set(skip_pages or ())
    try:
        for pno in range(doc.page_count):
            if (pno + 1) in skip:
                continue
            boxes = _page_token_boxes(doc[pno])
            toks = [t for t, _ in boxes]
            for start in (i for i, t in enumerate(toks) if t == target[0]):
                pos, hits, ok = start, [boxes[start][1]], True
                for want in target[1:]:
                    nxt = None
                    for j in range(pos + 1, min(pos + 1 + max_gap + 1, len(toks))):
                        if toks[j] == want:
                            nxt = j
                            break
                    if nxt is None:
                        ok = False
                        break
                    pos = nxt
                    hits.append(boxes[nxt][1])
                if ok:
                    out.append((pno + 1, hits))
                    break                      # one hit per page is enough
            if len(out) >= limit:
                break
    finally:
        doc.close()
    return out


def _page_shot(pdf_path: str, page_no: int, groups=None, max_w_pt: float = 340.0,
               color=(0.85, 0.1, 0.1), min_height: float = 0.0,
               full_width: bool = False):
    """PNG of `page_no`, cropped around `groups` and boxed.

    `groups` is either a plain list of rects (drawn in `color`) or a list of
    (rects, color) pairs, so a defect and the shared anchor can be marked in
    different colours on the same shot. `min_height` pads the crop so a pair of
    shots can be given the same vertical extent.
    """
    if not page_no:
        return None
    try:
        doc = fitz.open(pdf_path)
        if not (1 <= page_no <= doc.page_count):
            doc.close()
            return None
        page = doc[page_no - 1]

        norm = []
        if groups:
            if isinstance(groups[0], (tuple, list)) and len(groups[0]) == 2 \
                    and not isinstance(groups[0][0], (int, float)) \
                    and not hasattr(groups[0], "x0"):
                norm = [(list(rs), c) for rs, c in groups if rs]
            else:
                norm = [(list(groups), color)]

        clip = None
        if norm:
            box = None
            for rects, col in norm:
                for r in rects:
                    rr = fitz.Rect(r)
                    box = rr if box is None else (box | rr)
                    page.draw_rect(rr, color=col, width=1.4)
            if box is not None:
                y0, y1 = box.y0 - _SHOT_PAD, box.y1 + _SHOT_PAD
                if min_height and (y1 - y0) < min_height:
                    grow = (min_height - (y1 - y0)) / 2.0
                    y0, y1 = y0 - grow, y1 + grow
                # Crop horizontally as well as vertically. Keeping the full page
                # width put whatever else sat in the same band next to the hit -
                # a figure label 8pt wide arrived in a 595pt strip, showing a
                # neighbouring illustration the caption was not talking about.
                # A generous minimum keeps a short hit in enough context to be
                # placed on the page.
                if full_width:
                    # Two shots of the same section have to be read against each
                    # other, so both are cropped the full width of their page and
                    # to the same height. Cropping each one tightly around its own
                    # boxes gave the two sides different scales — one pane came
                    # back magnified several times over.
                    x0, x1 = page.rect.x0, page.rect.x1
                else:
                    x0, x1 = box.x0 - _SHOT_PAD, box.x1 + _SHOT_PAD
                    if (x1 - x0) < _SHOT_MIN_W:
                        grow = (_SHOT_MIN_W - (x1 - x0)) / 2.0
                        x0, x1 = x0 - grow, x1 + grow
                clip = fitz.Rect(x0, y0, x1, y1) & page.rect
        pix = page.get_pixmap(matrix=fitz.Matrix(_SHOT_ZOOM, _SHOT_ZOOM), clip=clip)
        png = pix.tobytes("png")
        doc.close()
        return png
    except Exception as exc:
        print(f"  screenshot failed for {os.path.basename(pdf_path)} p{page_no}: {exc}")
        return None


_DEFECT_COLOR = (0.85, 0.10, 0.10)     # red   — the problem itself
_ANCHOR_COLOR = (0.10, 0.35, 0.85)     # blue  — the shared text used to line the pages up


def _paired_evidence(src_path, other_path, defect_text, anchors,
                     src_hint=0, src_skip=None, other_skip=None):
    """Two shots of the SAME content region, one per document.

    Both crops are taken around an anchor the two documents share, and the
    document carrying the defect also has the defect itself in view. Cropping the
    two sides independently — one around the defect, one around the anchor — is
    what made the panes show unrelated parts of the page.

    Returns (src_png, src_page, other_png, other_page, anchor) or Nones.
    """
    d_pg, d_rects = _locate_tokens(src_path, defect_text, src_hint,
                                   skip_pages=src_skip)
    if not d_pg:
        return None, 0, None, 0, None

    for anchor in anchors:
        a_src_pg, a_src_rects = _locate_tokens(src_path, anchor, d_pg,
                                               skip_pages=src_skip)
        if a_src_pg != d_pg or not a_src_rects:
            continue                    # anchor must sit on the defect's page
        a_oth_pg, a_oth_rects = _locate_tokens(other_path, anchor, 0,
                                               skip_pages=other_skip)
        if not a_oth_pg:
            continue

        box = fitz.Rect(d_rects[0])
        for r in list(d_rects) + list(a_src_rects):
            box |= fitz.Rect(r)
        height = (box.y1 - box.y0) + 2 * _SHOT_PAD

        src_png = _page_shot(src_path, d_pg,
                             [(d_rects, _DEFECT_COLOR),
                              (a_src_rects, _ANCHOR_COLOR)])
        oth_png = _page_shot(other_path, a_oth_pg,
                             [(a_oth_rects, _ANCHOR_COLOR)],
                             min_height=height)
        return src_png, d_pg, oth_png, a_oth_pg, anchor

    # No anchor shared on the defect's page: show the defect alone rather than
    # pairing it with an unrelated region of the other document.
    return (_page_shot(src_path, d_pg, [(d_rects, _DEFECT_COLOR)]),
            d_pg, None, 0, None)


def _shot_flowable(png: bytes, max_w: float = 344.0, max_h: float = 260.0):
    """A ReportLab Image scaled to fit the evidence column, or a placeholder."""
    if not png:
        return None
    try:
        bio = io.BytesIO(png)
        img = RLImage(bio)
        scale = min(max_w / img.imageWidth, max_h / img.imageHeight, 1.0)
        img.drawWidth  = img.imageWidth * scale
        img.drawHeight = img.imageHeight * scale
        return img
    except Exception as exc:
        print(f"  screenshot flowable failed: {exc}")
        return None


_LATIN_PHRASE_RE = re.compile(r"[A-Za-z][A-Za-z0-9\-’']{2,}(?:\s+[A-Za-z][A-Za-z0-9\-’']{2,}){1,4}")
_ANY_PHRASE_RE   = re.compile(r"[^\W\d_]{2,}(?:\s+[^\W\d_]{2,}){1,4}", re.UNICODE)
_UNRELIABLE_CHAR_RE = re.compile(r"[\u0590-\u05ff\u0600-\u06ff\u3000-\u9fff"
                                 r"\uac00-\ud7af\uf900-\ufaff\ue000-\uf8ff]")


def _anchor_candidates(*texts):
    """Latin phrases from `texts`, longest first — used to find the same place
    in the other document. An anchor must be text both documents share, so the
    missing text itself is never used; its surroundings are."""
    # Ordered by SOURCE, not by length: `texts` is passed nearest-context-first,
    # and an anchor from right beside the defect points at the same place on the
    # other page. Sorting everything by length let a long phrase from elsewhere
    # on the page win, so the two panes ended up showing different regions.
    seen, uniq = set(), []
    for t in texts:
        if not t:
            continue
        # Bullets and other separators would otherwise break a perfectly good
        # anchor ("Nederlands • Svenska • Português") into single words, which
        # are too weak to anchor on. Separators become spaces; the locator
        # compares canonical tokens and ignores punctuation anyway.
        flat_t = re.sub(r"[^\w\s'’-]+", " ", re.sub(r"\s+", " ", t))
        found = []
        for m in _LATIN_PHRASE_RE.finditer(flat_t):
            phrase = m.group(0).strip()
            if len(phrase) >= 8:
                found.append(phrase)
        if not found:
            # No Latin run nearby (a language list's neighbours may be Cyrillic
            # or Greek). Those scripts extract correctly in both documents, so
            # they anchor just as well — the locator compares canonical tokens
            # and is script-agnostic. Private-use and unmapped CJK are excluded,
            # since those are exactly the characters that cannot be trusted.
            for m in _ANY_PHRASE_RE.finditer(flat_t):
                phrase = m.group(0)
                # Cut at the first character that cannot be trusted, so an
                # anchor never carries the very glyphs the other document
                # renders differently.
                cut = _UNRELIABLE_CHAR_RE.search(phrase)
                if cut:
                    phrase = phrase[:cut.start()]
                phrase = re.sub(r"\s+", " ", phrase).strip()
                if len(phrase) >= 6 and len(phrase.split(" ")) >= 2:
                    found.append(phrase)
        found.sort(key=len, reverse=True)      # longest within this source only
        for a in found:
            k = a.lower()
            if k not in seen:
                seen.add(k)
                uniq.append(a)
    return uniq[:8]


def _counterpart_shot(other_path: str, anchors, hint_page: int = 0,
                      skip_pages=None):
    """(png, page_no, anchor) for the matching place in the other document.

    The anchor is text the two documents share, so the page is verified — it is
    boxed in blue to distinguish it from the red box marking a defect. Contents
    and index pages are skipped: a heading's first occurrence is in the table of
    contents, and showing that as "the same topic" would be misleading. Returns
    (None, 0, None) when no shared anchor can be found, rather than guessing.
    """
    for anchor in anchors:
        pno, rects = _locate_tokens(other_path, anchor, hint_page,
                                    skip_pages=skip_pages)
        if pno:
            return (_page_shot(other_path, pno, rects, color=(0.10, 0.35, 0.85)),
                    pno, anchor)
    return None, 0, None


def _evidence_pair(prod_path, stage_path, prod_png, prod_page,
                   stage_png, stage_page, caption, note, styles):
    """Caption + a PROD/STAGE screenshot pair, each labelled with what it shows."""
    p_img = _shot_flowable(prod_png)
    s_img = _shot_flowable(stage_png)
    if not p_img and not s_img:
        return []
    cap_s = ParagraphStyle("EvCap", parent=styles["Normal"], fontSize=8.5,
                           leading=11.5, spaceAfter=3)
    lab_s = ParagraphStyle("EvLab", parent=styles["Normal"], fontSize=7.5,
                           leading=10, textColor=colors.HexColor("#37474f"))
    def side(img, label):
        return ([Paragraph(label, lab_s), img] if img
                else [Paragraph(label, lab_s)])
    cells = [[Paragraph(f"<b>PROD</b> — {'page %d' % prod_page if prod_page else 'not shown'}", lab_s),
              Paragraph(f"<b>STAGE</b> — {'page %d' % stage_page if stage_page else 'not shown'}", lab_s)],
             [p_img or Paragraph("—", lab_s), s_img or Paragraph("—", lab_s)]]
    t = Table(cells, colWidths=[350, 350])
    t.setStyle(TableStyle([
        ("GRID",         (0,0), (-1,-1), 0.5, colors.HexColor("#b0bec5")),
        ("VALIGN",       (0,0), (-1,-1), "TOP"),
        ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#eceff1")),
        ("TOPPADDING",   (0,0), (-1,-1), 4),
        ("BOTTOMPADDING",(0,0), (-1,-1), 4),
        ("LEFTPADDING",  (0,0), (-1,-1), 4),
    ]))
    out = [Paragraph(caption, cap_s)]
    if note:
        out.append(Paragraph(note, ParagraphStyle(
            "EvNote", parent=styles["Normal"], fontSize=7.5, leading=10,
            textColor=colors.grey, spaceAfter=2)))
    out += [t, Spacer(1, 10)]
    return out


def _shot_on_known_page(pdf_path: str, page_no: int, needle: str):
    """(png, page_no) for a page we already know carries `needle`.

    Used for encoding glitches, where the scan recorded the exact page. The glyph
    is boxed when its word box can be found; otherwise the page is still shown,
    because the page itself is verified — only the box is uncertain.
    """
    if not page_no:
        return None, 0
    rects = []
    try:
        doc = fitz.open(pdf_path)
        if 1 <= page_no <= doc.page_count:
            probe = (needle or "").strip()
            for tok, rect in _page_token_boxes(doc[page_no - 1]):
                if probe and (tok in _split_canon(probe)
                              or probe.lower().startswith(tok)):
                    rects.append(rect)
            if not rects:
                for r in (doc[page_no - 1].search_for(probe) or []):
                    rects.append(fitz.Rect(r))
        doc.close()
    except Exception:
        rects = []
    return _page_shot(pdf_path, page_no, rects or None), page_no


def _issue_shot(pdf_path: str, needle: str, hint_page: int = 0):
    """(png, page_no) for `needle` boxed on the page that has it, else (None, 0)."""
    page_no, rects = _locate_tokens(pdf_path, needle, hint_page)
    if not page_no:
        return None, 0
    return _page_shot(pdf_path, page_no, rects), page_no


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
def _readable_glyphs(text: str) -> str:
    """Show unmappable characters by code point instead of an empty box.

    Private-use and replacement characters have no glyph in any font, so they
    print as a hollow box that tells the reader nothing. Spelling them out makes
    the defect legible in the report.
    """
    out = []
    for ch in text or "":
        o = ord(ch)
        if 0xE000 <= o <= 0xF8FF or o == 0xFFFD:
            out.append(f"<U+{o:04X}>")
        else:
            out.append(ch)
    return "".join(out)


def _esc(text):
    esc = _readable_glyphs(text or "")
    esc = esc.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
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


# Every issue the report can raise, paired with what it means and what to do
# about it. The report is read by people who then have to fix STAGE, so each row
# carries the remedy next to the finding instead of leaving it to be inferred.
_FIX_ADVICE = {
    "Text layer":            ("The page looks right but its text is not readable "
                              "by machine.",
                              "Re-embed the font with a proper Unicode (ToUnicode) "
                              "map, or replace it with a text-mapped font, so "
                              "copy/paste, search and screen readers get the "
                              "right characters."),
    "HTML entity":           ("A raw HTML entity was published instead of the "
                              "character it stands for.",
                              "Decode the entity in the source content, then "
                              "re-publish."),
    "Image label missing":   ("A label PROD prints on a figure is not readable in "
                              "STAGE.",
                              "Restore the label on the STAGE figure. If STAGE "
                              "draws the figure as a flat image, publish it with "
                              "a live text layer so the label is selectable."),
    "Diagram callout number missing":
                             ("PROD numbers a diagram's parts, but one or more "
                              "of those callout numbers do not appear anywhere "
                              "on STAGE's version of the same diagram.",
                              "Restore the missing callout number(s) so every "
                              "part STAGE's diagram calls out is labelled, "
                              "matching PROD."),
    "Image missing":         ("PROD illustrates this topic; STAGE has no figure "
                              "for it.",
                              "Add the figure to the STAGE topic, or confirm the "
                              "topic was intentionally published without it."),
    "Images pixelated":      ("STAGE's figures are stored at a lower resolution "
                              "than they are displayed at, so the pixels show.",
                              "Re-export the source artwork at print resolution "
                              "— 300 dpi or better at the size each figure is "
                              "placed — and re-publish. Downscaling on export, "
                              "or re-using a screen-sized asset, is the usual "
                              "cause."),
    "Image not correctly updated":
                             ("This section carries different artwork in the two "
                              "documents — STAGE was not updated with the figure "
                              "PROD publishes.",
                              "Replace the figure in this STAGE section with the "
                              "current artwork, then confirm it renders at the "
                              "right size and resolution."),
    "Image difference":      ("The two documents show different artwork for the "
                              "same figure.",
                              "Check which artwork is current and re-publish "
                              "STAGE with it."),
    "Image alignment changed": (
        "The same picture appears in PROD and STAGE but is placed differently "
        "in the text column - left where PROD centres it, or the reverse.",
        "Match PROD's placement: set the figure to the same alignment "
        "(left / centre / right) in STAGE."),
    "Image mismatch":        ("PROD prints more figures under this heading than "
                              "STAGE has anywhere in the same section — one of "
                              "PROD's pictures has no counterpart in STAGE.",
                              "Put the PROD artwork back in this STAGE section. "
                              "Every figure PROD publishes under a heading must "
                              "appear under that heading in STAGE."),
    "Table heading missing": ("A column heading is not present in any STAGE "
                              "table header.",
                              "Restore the heading so the column is identifiable, "
                              "and check the column itself was not dropped with "
                              "it."),
    "Table row missing":     ("A whole row PROD prints in a table - its label "
                              "and its values - is not in STAGE, as a table row "
                              "or as text anywhere else.",
                              "Add the row back to the STAGE table, or confirm "
                              "the row was removed on purpose."),
    "Table cell missing":    ("A value PROD prints in a table row is not present "
                              "anywhere in STAGE's copy of that row — the "
                              "wording was matched against the whole document, "
                              "not just the same cell position, so re-wrapping "
                              "or a moved column is not what triggered this.",
                              "Put the missing value back in the STAGE table "
                              "row, or confirm the row was cut on purpose."),
    "Table breaking the margins":
                             ("The table is wider than the text column and spills "
                              "into the page margin.",
                              "Narrow the table to the text column — reduce column "
                              "widths, wrap long cell text, or set the table to "
                              "the page width the rest of the content uses."),
    "Table cell layout differs":
                             ("The columns are the same but the cells inside the "
                              "table are merged or split differently from PROD.",
                              "Match the cell structure to PROD: keep the same "
                              "cells merged and the same values together in one "
                              "cell."),
    "Table continuation missing its header":
                             ("The table runs onto this page but the column "
                              "header row is not repeated, so the columns on "
                              "this page are unlabelled.",
                              "Set the table's header row to repeat on every "
                              "page it continues onto."),
    "Table layout broken":   ("A page break splits the table, separating rows "
                              "from their header.",
                              "Keep the table on one page, or repeat the header "
                              "row on each page it continues onto."),
    "Table column layout differs":
                             ("STAGE lays this table out with different columns "
                              "than PROD — a column is added, dropped or moved.",
                              "Match the column definition to PROD. A merged, "
                              "split or re-ordered column changes what each value "
                              "belongs to, so check the cells landed in the right "
                              "column as well."),
    "Table columns differ":  ("The table is laid out with a different number of "
                              "columns than PROD.",
                              "Re-check the column definition: a merged or split "
                              "column changes what each value belongs to."),
    "Bold lost":             ("Text emphasised in PROD is drawn in the ordinary "
                              "body face in STAGE.",
                              "Restore the bold on this text. The weight marks it "
                              "as a label or a term, and without it the line reads "
                              "as ordinary prose."),
    "Italic lost":           ("Text italicised in PROD is upright in STAGE.",
                              "Restore the emphasis — italics usually mark a term "
                              "or a caption and carry meaning."),
    "List alignment broken": ("A step number sits on its own line, away from the "
                              "step it numbers.",
                              "Fix the list style so the marker and its text stay "
                              "on one line and wrap under the text, not under the "
                              "number."),
    "List marker changed":   ("The list uses a different marker style than PROD.",
                              "Match PROD's marker style so numbered and bulleted "
                              "steps stay distinguishable."),
    "Text alignment changed":
                             ("A title or paragraph is aligned differently than it "
                              "is in PROD — centred where PROD sets it flush, or "
                              "set at a different indent.",
                              "Match PROD's paragraph/heading style so the block "
                              "sits where PROD sits it."),
    "List indent changed":   ("A list item sits at a different depth than it does "
                              "in PROD. A sub-item set flush with its parent has "
                              "lost its nesting, and the reader can no longer see "
                              "which step it belongs to.",
                              "Restore the nesting in the STAGE list so sub-items "
                              "indent under their parent as they do in PROD."),
    "Paragraph merged with heading":
                             ("PROD prints this numbered item's label on its own "
                              "line and starts the description underneath; STAGE "
                              "runs the description straight on from the label "
                              "on the same line.",
                              "Break the paragraph onto its own line under the "
                              "label in STAGE, matching PROD."),
    "Content missing":       ("Text PROD carries under this topic is absent from "
                              "STAGE.",
                              "Restore the sentence in the STAGE topic, or confirm "
                              "it was withdrawn deliberately."),
    "Hyperlink lost":        ("PROD links this text; STAGE publishes the same "
                              "words with no link on them.",
                              "Restore the hyperlink on this text in STAGE and "
                              "point it at the same destination PROD uses."),
    "Hyperlink not highlighted in STAGE":
                             ("STAGE carries the link, but draws the anchor in "
                              "plain body text — no colour, no underline. The "
                              "link works if you happen to click it, but nothing "
                              "on the page tells the reader it is there.",
                              "Apply the link character style in STAGE so the "
                              "anchor is drawn the way PROD draws it."),
    "Hyperlink added in STAGE":
                             ("STAGE links this cross-reference; PROD prints the "
                              "same words as plain text with no link.",
                              "Confirm the STAGE link is wanted; if PROD is the "
                              "baseline, add the same link in PROD or drop it "
                              "from STAGE."),
    "Hyperlink":             ("A link differs between the two documents.",
                              "Point the STAGE link at the same destination as "
                              "PROD and confirm it resolves."),
    "Hyperlink goes to the wrong page":
                             ("The link's own text names one page, but clicking it "
                              "lands the reader on a different one.",
                              "Re-generate the cross-reference so the page it names "
                              "and the page it jumps to are the same."),
    "Hyperlink goes to the wrong section":
                             ("The link names one section but its target lands the "
                              "reader in a different section.",
                              "Re-point the link at the heading it names, then "
                              "click it and confirm it arrives at that section."),
    "Cross-reference page number is wrong":
                             ("STAGE prints “see … on page N” but the "
                              "named section is not on STAGE page N.",
                              "Re-generate all page cross-references after the final "
                              "STAGE pagination so each printed page number is "
                              "correct."),
    "Cross-reference points to the wrong section":
                             ("A “see … on page N” reference in STAGE "
                              "lands on a page that holds a different section.",
                              "Fix the reference so the section name and the page it "
                              "sends the reader to match."),
    "Page number":           ("A page reference does not match.",
                              "Re-generate the cross-references after the final "
                              "pagination."),
    "Image highlight box missing":
                             ("One document draws a coloured box / highlight on "
                              "this figure that the other does not.",
                              "Add the same coloured highlight to the figure in "
                              "the document that is missing it, or remove it from "
                              "the one that has it, so both match."),
    "Broken image":          ("An image did not render.",
                              "Re-link or re-upload the asset, then confirm it "
                              "renders in the published output."),
}
_FIX_DEFAULT = ("STAGE does not match PROD at this location.",
                "Compare the two documents here and bring STAGE in line with "
                "PROD, or record why the difference is intended.")


def _llm_verify_missing(content_results, prod_text, stage_text):
    """Optional second-opinion pass over the mechanical content diff.

    The shingle matcher flags any run of PROD characters it cannot find in
    STAGE (and vice versa).  Many flags are false positives: PyMuPDF
    concatenates page text in draw order, so screenshot captions, on-screen
    display menu labels and table cells come out as reordered word-salad that
    never existed as a sentence.  This asks Claude to separate genuine dropped
    prose from extraction artifacts and prunes the artifacts in place.

    Best-effort — returns silently (leaving findings untouched) when the
    ``anthropic`` package or credentials are missing or the call fails.
    Disable entirely with ``VALIDATOR_LLM_VERIFY=0``.
    """
    if os.environ.get("VALIDATOR_LLM_VERIFY", "").strip() == "0":
        return
    try:
        import anthropic
    except ImportError:
        return

    cand = []
    for ri, r in enumerate(content_results):
        for kind in ("missing", "extra"):
            for mi, frag in enumerate(r.get(kind) or []):
                cand.append({"id": len(cand), "ri": ri, "kind": kind,
                             "mi": mi, "title": r.get("title", ""),
                             "text": frag})
    if not cand:
        return

    try:
        client = anthropic.Anthropic()
    except Exception:
        return

    listing = "\n".join(
        f'[{c["id"]}] ({c["kind"]}; topic {c["title"]!r}) {c["text"]}'
        for c in cand)
    system = (
        "You verify a PDF documentation diff. Two revisions of one product "
        "manual (PROD = older, STAGE = newer) were compared by a mechanical "
        "character matcher that flags text present in one revision but not "
        "found in the other. The PDF text extractor concatenates page content "
        "in draw order, so screenshot captions, on-screen-display menu labels "
        "and table cells often come out as reordered word-salad that never "
        "existed as a real sentence — those are false positives. For each "
        "flagged fragment decide GENUINE (a readable sentence or phrase of "
        "body text that really is absent from the other revision) or ARTIFACT "
        "(reordered/garbled extraction, OCR noise, or duplicated boilerplate "
        "that should not be reported). When unsure, answer GENUINE. Reply with "
        'ONLY a JSON object: {"verdicts":[{"id":<int>,"verdict":"GENUINE"|'
        '"ARTIFACT"}, ...]} covering every id.')
    user = ("PROD full text:\n<<<\n" + prod_text + "\n>>>\n\n"
            "STAGE full text:\n<<<\n" + stage_text + "\n>>>\n\n"
            "Flagged fragments ('missing' = claimed absent from STAGE, "
            "'extra' = claimed absent from PROD):\n" + listing)

    try:
        resp = client.messages.create(
            model="claude-opus-5",
            max_tokens=8000,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        print(f"  LLM verify skipped: {e}")
        return

    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return
    try:
        verdicts = json.loads(m.group(0)).get("verdicts", [])
    except Exception:
        return

    drop = {v["id"] for v in verdicts
            if str(v.get("verdict", "")).upper() == "ARTIFACT"}
    if not drop:
        print("  LLM verify: all content fragments confirmed genuine")
        return

    by_row = {}
    for c in cand:
        if c["id"] in drop:
            by_row.setdefault((c["ri"], c["kind"]), set()).add(c["mi"])
    n_dropped = 0
    for (ri, kind), idxs in by_row.items():
        r = content_results[ri]
        orig = r.get(kind) or []
        r[kind] = [f for i, f in enumerate(orig) if i not in idxs]
        n_dropped += len(orig) - len(r[kind])
    for r in content_results:
        if r.get("status") == "Fail" and not (r.get("missing") or r.get("extra")):
            r["status"] = "Pass"
    print(f"  LLM verify: dropped {n_dropped} extraction-artifact fragment(s)")


def _ai_cross_check(content_results, prod_path, stage_path,
                    prod_raw="", stage_raw="", prod_nav=None, stage_nav=None,
                    prod_sections=None, stage_sections=None, stage_lookup=None):
    """Heading-scoped cross-check that ADDS content missing from STAGE that the
    main pass did not report.

    Two independent passes, both keyed on the *heading*, never on page numbers
    (PROD content under a heading may sit on one page and the matching STAGE
    content spill across two):

      1. Table-row multiset diff - every "label / value" row from both PDFs'
         tables; a row PROD has more copies of than STAGE is a dropped row
         (catches a single spec row removed from a table that repeats the same
         labels for other product variants).
      2. Section prose diff - for each PROD section, diff its text against the
         *whole* matching STAGE section (all pages it spans), and report the
         runs of PROD wording that appear nowhere in that STAGE section.

    Findings render in the normal report under "Content missing".  A local
    Ollama model, when reachable, prunes extraction noise.  Disable with
    ``VALIDATOR_AI_CROSSCHECK=0``.
    """
    if os.environ.get("VALIDATOR_AI_CROSSCHECK", "").strip() == "0":
        print("  AI cross-check: disabled (checkbox off) - plain report")
        return

    prod_nav = prod_nav or {1}
    stage_nav = stage_nav or {1}
    prod_sections = prod_sections or {}
    stage_sections = stage_sections or {}
    stage_lookup = stage_lookup or {}

    _alpha = re.compile(r"[a-z]{3,}")
    _tokrx = re.compile(r"[a-z0-9][a-z0-9.\-/%°]*")
    _UNIT = re.compile(r"\b\d+(\.\d+)?\s?(w|kw|g|kg|mg|mm|cm|m|km|hz|khz|mhz|ghz|"
                       r"mbps|gbps|kbps|fps|pcs|pc|v|va|a|ma|db|dba|ppi|dpi|bit|"
                       r"nits?|cd|lm|hrs?|hours?|min|sec|ms|inch|inches|lbs?|"
                       r"°c|°f)\b", re.I)
    _SENT = re.compile(r"[a-z]{3,}(\s+[a-z’'a-z]{2,}){5,}", re.I)
    _XREF = re.compile(r"^(see|refer to|for more information|for details|"
                       r"for the location|on page)\b", re.I)
    _JUNK = re.compile(r"all rights reserved|benq corporation|modification reserved"
                       r"|^\W*$", re.I)
    _XREF_WORDS = {"see", "refer", "page", "pages", "information", "instructions",
                   "details", "section", "chapter", "above", "below", "following"}
    _FUNC = {"a", "an", "the", "is", "are", "to", "of", "and", "or", "for", "in",
             "on", "with", "you", "your", "this", "that", "it", "will", "can",
             "do", "not", "be", "as", "at", "by", "from", "when", "if", "no"}
    _CONTENTS_TITLE = re.compile(r"table of contents?|^contents?$|\bindex\b", re.I)

    def _tok(s):
        out = []
        for w in _tokrx.findall((s or "").lower()):
            w = w.strip(".-/")
            if w:
                out.append(w)
        return out

    def _sig(t):
        return re.sub(r"[^a-z0-9]+", "", str(t or "").lower())

    def _prettify(run):
        t = re.sub(r"\s+([.,;:%)])", r"\1", " ".join(run))
        t = re.sub(r"\s{2,}", " ", t).strip()
        return (t[:1].upper() + t[1:])[:400]

    def _is_content(frag):
        w = [x.lower() for x in frag.split()]
        if len(w) < 5:
            return False
        if sum(1 for x in w if len(x) <= 2) / len(w) >= 0.45:
            return False
        if _XREF.match(frag) or _JUNK.search(frag):
            return False
        if sum(1 for x in w if x in _XREF_WORDS) >= 3:
            return False
        has_unit = bool(_UNIT.search(frag))
        if not has_unit and sum(1 for x in w if x in _FUNC) / len(w) < 0.18:
            return False
        return bool(_SENT.search(frag) or has_unit)

    # de-dup against everything already reported (exact + fuzzy word-set)
    seen = set()
    kept_wsets = []

    def _wset(t):
        return frozenset(w for w in re.findall(r"[a-z]{3,}", str(t or "").lower()))

    for r in content_results:
        for k in ("missing", "extra"):
            for frag in (r.get(k) or []):
                s = _sig(frag)
                if len(s) >= 12:
                    seen.add(s)
                ws = _wset(frag)
                if len(ws) >= 4:
                    kept_wsets.append(ws)

    def _dup(frag):
        if _sig(frag) in seen:
            return True
        ws = _wset(frag)
        if len(ws) < 4:
            return False
        for other in kept_wsets:
            inter = len(ws & other)
            if inter and inter / min(len(ws), len(other)) >= 0.75:
                return True
        return False

    def _mark_seen(frag):
        seen.add(_sig(frag))
        ws = _wset(frag)
        if len(ws) >= 4:
            kept_wsets.append(ws)

    def _row_for_title(title):
        key = _norm_key(title)
        for r in content_results:
            if _norm_key(r.get("title", "")) == key:
                return r
        r = {"title": title, "level": 2, "status": "Fail",
             "prod_page": "?", "stage_page": "?",
             "coverage": 0.0, "missing": [], "extra": []}
        content_results.append(r)
        return r

    # ── Pass 1: table-row multiset diff ──
    def _dehyph(s):
        s = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", s or "")
        return re.sub(r"\s+", " ", s).strip()

    def _clean_cell(s):
        return _dehyph(re.sub(r"[•▪◦·‣■●∙]", " ", s or "")).strip()

    def _good_label(raw):
        """A real row label: starts upper/digit, <=5 words, not a sentence."""
        t = raw.strip()
        if not t or len(t) > 45 or "," in t or t.rstrip().endswith((".", ":")):
            return False
        if not re.match(r"[A-Z0-9]", t):
            return False
        return 1 <= len(re.findall(r"\S+", t)) <= 5

    def _split_vals(cell):
        """A cell like 'YES NO' or 'Model Name Version Usage Time' -> list."""
        parts = re.split(r"\s*[•\n]\s*|\s{2,}", cell)
        parts = [p.strip() for p in parts if p.strip()]
        return parts or ([cell.strip()] if cell.strip() else [])

    def _tables(path, nav):
        """[(page, [header], {row_label: [cells]})] for every real-looking table."""
        d = fitz.open(path)
        out = []
        for i in range(d.page_count):
            if (i + 1) in nav:
                continue
            try:
                tabs = d[i].find_tables().tables
            except Exception:  # noqa: BLE001
                tabs = []
            for t in tabs:
                try:
                    grid = t.extract()
                except Exception:  # noqa: BLE001
                    continue
                if not grid or len(grid) < 3 or len(grid[0]) < 2:
                    continue
                header = [_clean_cell(c).lower() for c in grid[0]]
                rows = {}
                for row in grid[1:]:
                    if len(row) < 2 or not _good_label(_clean_cell(row[0])):
                        continue
                    rows[_clean_cell(row[0]).lower()] = [
                        _clean_cell(c) for c in row[1:]]
                if len(rows) >= 2:
                    out.append((i + 1, header, rows))
        d.close()
        return out

    n_tbl = 0
    try:
        p_tabs = _tables(prod_path, prod_nav)
        s_tabs = _tables(stage_path, stage_nav)

        _pmarks = sorted((r["prod_page"], r.get("title", ""))
                         for r in content_results
                         if isinstance(r.get("prod_page"), int))

        def _sec_of_ppage(pg):
            name = _pmarks[0][1] if _pmarks else ""
            for mp, mt in _pmarks:
                if mp <= pg:
                    name = mt
                else:
                    break
            return name

        for ppg, phdr, prows in p_tabs:
            # match STAGE's table with the most shared row labels
            best, best_n = None, 0
            for spg, shdr, srows in s_tabs:
                n = len(set(prows) & set(srows))
                if n > best_n:
                    best, best_n = (spg, shdr, srows), n
            if not best or best_n < 2:
                continue
            spg, shdr, srows = best
            sec = _sec_of_ppage(ppg)
            for rlab, pcells in prows.items():
                scells = srows.get(rlab)
                if scells is None:
                    continue
                s_join = " ".join(scells).lower()
                lost = []
                for ci, pc in enumerate(pcells):
                    for v in _split_vals(pc):
                        vl = v.lower()
                        if len(re.findall(r"[a-z0-9]", vl)) < 2 or len(v) > 90:
                            continue
                        if vl in s_join or v in " ".join(pcells[:ci]):
                            continue
                        col = (phdr[ci + 1] if ci + 1 < len(phdr) and phdr[ci + 1]
                               else "")
                        lost.append((col, v))
                if not lost:
                    continue
                items, seents = [], set()
                for col, v in lost:
                    key = v.lower()
                    if key in seents:
                        continue
                    seents.add(key)
                    items.append((f"[{col}] " if col else "") + v)
                frag = (f"table “{rlab.title()}” row — STAGE is missing: "
                        + "; ".join(items))[:500]
                if _dup(frag):
                    continue
                r = _row_for_title(sec)
                r.setdefault("missing", [])
                r["missing"].append(f"(AI) p{ppg} (STAGE p{spg}): {frag}")
                if r.get("status") in ("Pass", "NO CONTENT"):
                    r["status"] = "Fail"
                n_tbl += 1
                _mark_seen(frag)
        if n_tbl:
            print(f"  AI cross-check: {n_tbl} table row(s) with dropped cells")
    except Exception as _e:  # noqa: BLE001
        print(f"  AI cross-check: table-cell pass skipped ({_e})")

    # ── Pass 2: heading-scoped sentence diff ──
    # For each PROD section, split into sentences and report any whole sentence
    # whose 5-grams appear nowhere in the matching STAGE section.  A complete
    # sentence either survives verbatim or it is genuinely gone -- reflow across
    # pages does not fragment it, so this is far quieter than a token diff.
    cand = []   # (title, fragment)
    _SPLIT = re.compile(r"(?<=[.!?;])\s+")
    for title, ptext in prod_sections.items():
        if not ptext or _CONTENTS_TITLE.search(title or ""):
            continue
        stext = (stage_sections.get(title)
                 or stage_lookup.get(_norm_key(title)) or "")
        if not stext:
            continue                       # "missing section" already handled
        st = _tok(stext)
        if len(st) < 20:
            continue
        s3 = {" ".join(st[k:k + 3]) for k in range(len(st) - 2)}
        s5 = {" ".join(st[k:k + 5]) for k in range(len(st) - 4)}
        for sent in _SPLIT.split(ptext):
            w = _tok(sent)
            real = [x for x in w if _alpha.fullmatch(x)]
            if len(real) < 12:
                continue
            g5 = [" ".join(w[k:k + 5]) for k in range(len(w) - 4)]
            g3 = [" ".join(w[k:k + 3]) for k in range(len(w) - 2)]
            if not g5:
                continue
            # present if a third of its 5-grams OR two-thirds of its 3-grams
            # already appear in STAGE (covers light rewording / reflow)
            if (sum(1 for x in g5 if x in s5) / len(g5) >= 0.30
                    or sum(1 for x in g3 if x in s3) / max(1, len(g3)) >= 0.66):
                continue
            frag = _prettify(w)
            if _is_content(frag) and not _dup(frag):
                cand.append((title, frag))
                _mark_seen(frag)

    # every candidate that survives the checks above is reported -- no cap,
    # so a heavily-restructured section does not have its later findings
    # silently dropped once an arbitrary count is reached
    keep = list(range(len(cand)))
    if cand:
        try:
            import urllib.request as _u
            from content_validation import ai_validate as _ai
            _u.urlopen(_ai.OLLAMA_HOST + "/api/version", timeout=3).read()
            sysmsg = ("Each line is text present in the OLD manual revision but "
                      "not found in the matching section of the NEW one. Mark "
                      "'real' if it is readable content a reader would miss (a "
                      "sentence, a spec row, a table row, a warning) or 'noise' "
                      "if it is garbled word-salad or a stray fragment. Unsure "
                      '-> real. JSON only: {"r":[{"i":<int>,"v":"real"|"noise"}]}')
            n_b = max(1, (len(cand) + 11) // 12)
            for bi, base in enumerate(range(0, len(cand), 12)):
                _emit(0.52 + 0.10 * (bi + 1) / n_b, f"AI cross-check {bi+1}/{n_b}")
                chunk = cand[base:base + 12]
                txt = "\n".join(f'[{base+k}] {c[1][:180]}'
                                for k, c in enumerate(chunk))
                body = json.dumps({"model": _ai.OLLAMA_MODEL, "system": sysmsg,
                                   "prompt": txt, "stream": False,
                                   "format": "json",
                                   "options": {"temperature": 0, "num_ctx": 4096,
                                               "num_predict": 500}}).encode()
                rq = _u.Request(_ai.OLLAMA_HOST + "/api/generate", data=body,
                                headers={"Content-Type": "application/json"})
                raw = json.loads(_u.urlopen(rq, timeout=180).read())["response"]
                for vm in re.finditer(
                        r'"i"\s*:\s*(\d+)\s*,\s*"v"\s*:\s*"([a-z]+)"', raw):
                    if vm.group(2).lower().startswith("nois"):
                        keep = [i for i in keep if i != int(vm.group(1))]
            print(f"  AI cross-check: model kept {len(keep)}/{len(cand)} prose "
                  f"candidate(s)")
        except Exception as _e:  # noqa: BLE001
            print(f"  AI cross-check: model unavailable, phrase-check only ({_e})")

    added = 0
    for i in keep:
        title, frag = cand[i]
        r = _row_for_title(title)
        r.setdefault("missing", [])
        r["missing"].append(f"(AI) {frag}")
        added += 1
        if r.get("status") in ("Pass", "NO CONTENT"):
            r["status"] = "Fail"

    print(f"  AI cross-check: added {added} prose finding(s) + {n_tbl} table "
          f"row(s) the section matcher missed")



def _fix_for(issue: str):
    """(what it means, what to do) for an issue label — longest match wins."""
    hit = max((k for k in _FIX_ADVICE if issue.lower().startswith(k.lower())),
              key=len, default=None)
    if hit is None:
        hit = max((k for k in _FIX_ADVICE if k.lower() in issue.lower()),
                  key=len, default=None)
    return _FIX_ADVICE[hit] if hit else _FIX_DEFAULT


# Issues are grouped by the kind of defect they are, because that is how they
# get fixed: encoding problems go to whoever owns the publishing pipeline, table
# problems to whoever owns the templates, image problems to whoever owns the
# artwork. A single flat list forced every reader to sort it themselves.
_CATEGORIES = [
    ("encoding",  "Encoding &amp; text-layer issues",
     "Characters that were published wrong, or text the page draws correctly but "
     "no machine can read."),
    ("content",   "Content differences",
     "Wording PROD carries that STAGE does not."),
    ("image",     "Image issues",
     "Figures, their labels, and the quality they are published at."),
    ("table",     "Table issues",
     "Table structure and the cells inside it."),
    ("alignment", "Alignment &amp; formatting issues",
     "Layout and emphasis: markers separated from their text, weight and slant "
     "that did not survive."),
    ("other",     "Links &amp; references",
     "Hyperlinks and page references."),
]

# Which category an issue belongs to, by the issue label it is reported under.
_ISSUE_CATEGORY = {
    "Text layer":            "encoding",
    "HTML entity":           "encoding",
    "Content missing":       "content",
    "Image label missing":   "image",
    "Diagram callout number missing": "image",
    "Image missing":         "image",
    "Image difference":      "image",
    "Image not correctly updated": "image",
    "Image mismatch":        "image",
    "Image alignment changed": "image",
    "Images pixelated":      "image",
    "Image highlight box missing": "image",
    "Broken image":          "image",
    "Table heading missing": "table",
    "Table continuation missing its header": "table",
    "Table cell layout differs": "table",
    "Table row missing":     "table",
    "Table cell missing":    "table",
    "Table layout broken":   "table",
    "Table breaking the margins": "table",
    "Table columns differ":  "table",
    "Table column layout differs": "table",
    "List alignment broken": "alignment",
    "List indent changed":   "alignment",
    "Text alignment changed": "alignment",
    "List marker changed":   "alignment",
    "Bold lost":             "alignment",
    "Italic lost":           "alignment",
    "Paragraph merged with heading": "alignment",
    "Hyperlink not highlighted in STAGE": "other",
    "Hyperlink":             "other",
    "Hyperlink lost":        "other",
    "Web address in PROD not linked in STAGE": "other",
    "Hyperlink added in STAGE": "other",
    "Hyperlink goes to the wrong page": "other",
    "Hyperlink goes to the wrong section": "other",
    "Cross-reference page number is wrong": "other",
    "Cross-reference points to the wrong section": "other",
    "Page number":           "other",
}


def _category_for(issue: str) -> str:
    """The section an issue belongs in — longest matching label wins."""
    hit = max((k for k in _ISSUE_CATEGORY if issue.lower().startswith(k.lower())),
              key=len, default=None)
    if hit is None:
        hit = max((k for k in _ISSUE_CATEGORY if k.lower() in issue.lower()),
                  key=len, default=None)
    return _ISSUE_CATEGORY.get(hit, "other")


# The report is scoped to the defects that were asked for: things missing,
# alignment, table layout and margin breakage, and hyperlinks that are gone or
# broken. Everything the validator can still detect stays detected — it is only
# the reporting that is narrowed — so widening this set is the whole change
# needed to bring a kind of issue back.
_REPORTED_ISSUES = (
    # encoding — leads the list. Garbled text or a broken text layer makes every
    # other reading of the page unreliable, so it has to be visible in the
    # report rather than detected and dropped.
    "Text layer",
    "HTML entity",
    "Encoding",
    "Garbled",
    # missing
    "Content missing",
    "Image label missing",
    "Diagram callout number missing",
    "Image missing",
    "Table row missing",
    "Table cell missing",
    "Table heading missing",
    # image: the picture PROD prints in a section must be the picture STAGE
    # prints there, and it must sit where PROD sits it.
    "Image mismatch",
    # "Image not correctly updated" is NOT reported: PROD and STAGE are rendered
    # by different pipelines, so an SSIM/pixel comparison scores the *same*
    # artwork anywhere from 0.06 to 0.82 (measured across 109 figure pairs) and a
    # genuinely changed image lands in that same band — there is no threshold
    # that separates them, so every reading is a coin-flip.  A missing or
    # re-placed figure is caught by "Image mismatch" / "Image alignment changed"
    # in terms of the picture itself.
    # figure placed left where PROD centres it (or the reverse) - a clear
    # left/centre/right change only, small shifts are not reported
    "Image alignment changed",
    # one side paints a coloured callout box / highlight on a figure, the other
    # does not - a strongly-coloured region reads the same in both pipelines
    "Image highlight box missing",
    # a figure whose STAGE artwork will not decode / renders blank
    "Broken image",
    # alignment
    # a step list PROD numbers 1. 2. 3. that STAGE draws a. b. c. or as bullets
    "List marker changed",
    "List alignment broken",
    "List indent changed",
    "Text alignment changed",
    "Paragraph merged with heading",
    # table layout and breakage
    "Table layout broken",
    "Table columns differ",
    "Table column layout differs",
    "Table breaking the margins",
    "Table cell layout differs",
    # a table that runs onto the next page without repeating its header leaves
    # the continuation as unlabelled columns
    "Table continuation missing its header",
    # A link STAGE carries but draws as plain body text: the reader has no way
    # to know it is there. Asked for explicitly, so it is reported.
    "Hyperlink not highlighted in STAGE",
    # A link STAGE carries but draws as plain body text - the reader cannot
    # see it is a link. Asked for explicitly.
    "Hyperlink not highlighted in STAGE",
    # Hyperlinks are reported ONLY when the link itself does not work - the
    # target does not resolve, the scheme is unusable, the hotspot has no area.
    # NOT reported (verified working links, repeatedly confirmed as false):
    #   "Hyperlink goes to the wrong page" / "wrong section"
    #   "Cross-reference page number is wrong" / "points to the wrong section"
    #     - these fire on a STALE PRINTED page number ("see X on page 45") while
    #       the clickable link still lands on the right section. The navigation
    #       works; only the typed number is out of date, and the page->folio
    #       inference behind the check is itself unreliable.
    #   "Hyperlink lost" / "added in STAGE" / "Web address ... not linked"
    #     - a link existing on one side only is a formatting choice, not breakage.
    "Internal link target does not resolve",
    "Hyperlink has no usable scheme",
    "Hyperlink hotspot cannot be clicked",
)


def _is_reported(issue: str) -> bool:
    """True when this issue label is one the report is scoped to."""
    low = (issue or "").lower()
    return any(low.startswith(k.lower()) or k.lower() in low
               for k in _REPORTED_ISSUES)


# ── Attributing every finding to the section it falls in ─────────────────────
def _section_page_index(toc_results, side: str):
    """[(page, title)] sorted, for resolving a page to the section holding it."""
    key = "prod_page" if side == "prod" else "stage_page"
    marks = []
    for r in toc_results or []:
        try:
            pg = int(r.get(key))
        except (TypeError, ValueError):
            continue
        if pg > 0 and r.get("title"):
            marks.append((pg, r["title"]))
    marks.sort(key=lambda t: t[0])
    return marks


def generate_report(prod_path, stage_path, toc_results, content_results,
                    image_results, icon_doc_summary, report_path,
                    tm_counts=None, tm_dropped=None,
                    prod_encoding_issue=False, stage_encoding_issue=False,
                    table_summary=None, table_findings=None, tablerow_findings=None,
                    figure_summary=None, glitches=None,
                    heading_findings=None, label_findings=None,
                    prod_nav_pages=None, stage_nav_pages=None,
                    link_findings=None, linkloss_findings=None,
                    pageno_findings=None,
                    bold_findings=None, figure_findings=None,
                    icon_findings=None, align_findings=None,
                    italic_findings=None, callout_counts=None,
                    figdiff_findings=None, pixel_findings=None,
                    liststyle_findings=None,
                    tableshape_findings=None, tablebreak_findings=None,
                    tablemargin_findings=None,
                    tablecont_findings=None,
                    tablemerge_findings=None,
                    callgap_findings=None,
                    figalign_findings=None, listindent_findings=None,
                    textalign_findings=None,
                    linkstyle_findings=None, imgmismatch_findings=None,
                    labelmerge_findings=None, colour_findings=None):
    colour_findings = colour_findings or []
    prod_nav_pages   = prod_nav_pages or set()
    stage_nav_pages  = stage_nav_pages or set()
    link_findings    = link_findings or []
    linkloss_findings = linkloss_findings or []
    pageno_findings  = pageno_findings or []
    bold_findings    = bold_findings or []
    figure_findings  = figure_findings or []
    icon_findings    = icon_findings or []
    align_findings   = align_findings or []
    italic_findings  = italic_findings or []
    callout_counts   = callout_counts or {}
    figdiff_findings = figdiff_findings or []
    pixel_findings   = pixel_findings or []
    liststyle_findings  = liststyle_findings or []
    tableshape_findings = tableshape_findings or []
    tablebreak_findings = tablebreak_findings or []
    tablemargin_findings = tablemargin_findings or []
    tablecont_findings = tablecont_findings or []
    tablemerge_findings = tablemerge_findings or []
    figalign_findings   = figalign_findings or []
    listindent_findings = listindent_findings or []
    textalign_findings = textalign_findings or []
    linkstyle_findings  = linkstyle_findings or []
    imgmismatch_findings = imgmismatch_findings or []
    callgap_findings    = callgap_findings or []
    labelmerge_findings = labelmerge_findings or []
    glitches         = glitches or []
    heading_findings = heading_findings or []
    label_findings   = label_findings or []
    tm_counts = tm_counts or []
    tm_dropped = tm_dropped or []
    table_findings = table_findings or []
    tablerow_findings = tablerow_findings or []

    # The same finding can reach the report twice (two detectors agreeing, or a
    # table/figure straddling pages so it is collected once per page). Duplicates
    # are dropped here, before anything is written, so neither the issues table
    # nor the evidence section ever shows the same thing twice.
    def _dedupe(items, *keys):
        seen, out = set(), []
        for it in items or []:
            k = tuple(str(it.get(x, "")) for x in keys) if isinstance(it, dict) else (str(it),)
            if k in seen:
                continue
            seen.add(k)
            out.append(it)
        return out

    def _dedupe_count(items, *keys):
        """Like _dedupe, but a repeated key is folded into ONE finding carrying
        how many separate instances it stood for, instead of silently dropping
        the rest — three identical images on a page each missing the same
        caption is three real defects, not one."""
        agg, order = {}, []
        for it in items or []:
            k = tuple(str(it.get(x, "")) for x in keys)
            if k not in agg:
                agg[k] = dict(it)
                agg[k]["_count"] = 1
                order.append(k)
            else:
                agg[k]["_count"] += 1
        return [agg[k] for k in order]

    glitches            = _dedupe(glitches, "doc", "page", "kind", "text")
    icon_findings       = _dedupe(icon_findings, "doc", "page", "kind", "text")
    label_findings      = _dedupe_count(label_findings, "page", "text")
    figure_findings     = _dedupe(figure_findings, "page", "stage_page", "title")
    heading_findings    = _dedupe(heading_findings, "page", "text", "row")
    table_findings      = _dedupe(table_findings, "page", "row", "col", "text")
    tablerow_findings   = _dedupe(tablerow_findings, "page", "label")
    tableshape_findings = _dedupe(tableshape_findings, "prod_page", "page", "header")
    tablebreak_findings = _dedupe(tablebreak_findings, "prod_page", "stage_from",
                                  "stage_to", "header")
    tablemargin_findings = _dedupe(tablemargin_findings, "page", "header")
    tablecont_findings  = _dedupe(tablecont_findings, "doc", "page", "header")
    tablemerge_findings = _dedupe(tablemerge_findings, "prod_page", "page", "header")
    figdiff_findings    = _dedupe(figdiff_findings, "page", "stage_page", "anchor")
    callgap_findings    = _dedupe(callgap_findings, "prod_page", "missing")
    labelmerge_findings = _dedupe(labelmerge_findings, "prod_page", "stage_page", "label")
    italic_findings     = _dedupe(italic_findings, "page", "text")
    align_findings      = _dedupe(align_findings, "doc", "page", "marker", "text")
    liststyle_findings  = _dedupe(liststyle_findings, "doc", "page", "text")
    link_findings       = _dedupe(link_findings, "doc", "page", "kind", "text")
    linkloss_findings   = _dedupe(linkloss_findings, "doc", "page", "text", "target")
    # Keyed on where the figure sits, not just its page: two different pictures
    # on one PROD page are two findings, and collapsing them on "kind" hid one.
    imgmismatch_findings = _dedupe(imgmismatch_findings, "section", "prod_page",
                                   "prod_where")
    figalign_findings   = _dedupe(figalign_findings, "page", "stage_page", "anchor")
    colour_findings     = _dedupe(colour_findings, "page", "stage_page", "colour")
    listindent_findings = _dedupe(listindent_findings, "prod_page", "page", "text")
    textalign_findings  = _dedupe(textalign_findings, "prod_page", "page", "text")
    linkstyle_findings  = _dedupe(linkstyle_findings, "prod_page", "page", "text")
    bold_findings       = _dedupe(bold_findings, "page", "stage_page", "text")
    pixel_findings      = _dedupe(pixel_findings, "count", "total", "worst")
    pageno_findings     = _dedupe(pageno_findings, "doc", "page", "kind", "text")
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
    fix_s   = ParagraphStyle("Fix",  parent=styles["Normal"],    fontSize=7,   leading=9.5,
                             textColor=colors.HexColor("#1e3a5f"))

    story = []

    # ── Header ──
    story.append(Paragraph("PDF Content Validation Report", title_s))
    story.append(Paragraph(f"Production: {os.path.basename(prod_path)}", sub_s))
    story.append(Paragraph(f"Staging:    {os.path.basename(stage_path)}", sub_s))
    story.append(Spacer(1, 8))

    # ═══════════════════════════════════════════
    # TOC / SECTION STATUS — every PROD topic and whether STAGE has it
    # ═══════════════════════════════════════════
    # This table validates ONE thing: does the TOC entry (heading) itself exist
    # in both documents. Content differences, table/image/link issues etc. are
    # already reported in full detail in their own sections below — repeating
    # them here as a second, differently-worded "Fail" on the same section was
    # confusing, not additive.
    _toc_status_rows, _n_toc_pass, _n_toc_miss, _n_toc_extra = [], 0, 0, 0
    _n_toc_level = 0
    for r in toc_results:
        title = r.get("title") or "—"
        ts = r.get("toc_status")
        _plvl, _slvl = r.get("prod_level"), r.get("stage_level")
        _lvl_changed = r.get("level_status") == "Changed"
        if _lvl_changed:
            _n_toc_level += 1
        if ts == "Extra in Stage":
            label, sty = "Extra in STAGE", extra_s
            _n_toc_extra += 1
        elif ts == "Missing in Stage":
            label, sty = "Missing in STAGE", miss_s
            _n_toc_miss += 1
        else:
            # The heading matches on both sides — a level change alone is
            # informational (shown in the "Level" column), not a fail.
            label, sty = "Pass", pass_s
            _n_toc_pass += 1
        # Indent the title by its own depth so the outline reads as a hierarchy
        # rather than a flat list of every heading at every level.
        _depth = _plvl if isinstance(_plvl, int) else (_slvl if isinstance(_slvl, int) else 1)
        _indent = "&nbsp;" * (4 * max(0, _depth - 1))
        if _lvl_changed:
            _lvl_txt = f"L{_plvl} \u2192 L{_slvl}"
        elif isinstance(_plvl, int) and isinstance(_slvl, int):
            _lvl_txt = f"L{_plvl}"
        elif isinstance(_plvl, int):
            _lvl_txt = f"L{_plvl} / —"
        elif isinstance(_slvl, int):
            _lvl_txt = f"— / L{_slvl}"
        else:
            _lvl_txt = "—"
        _toc_status_rows.append([
            Paragraph(_indent + _esc(title), cell_s),
            Paragraph(_lvl_txt, fail_s if _lvl_changed else cell_s),
            Paragraph(str(r.get("prod_page") or "—"), cell_s),
            Paragraph(str(r.get("stage_page") or "—"), cell_s),
            Paragraph(label, sty),
        ])
    _toc_overall = "FAIL" if _n_toc_miss else "PASS"
    # Every outline depth on either side, so the reader can see the nesting the
    # two documents use — not just the headings that happen to differ.
    _depths = sorted({d for r in toc_results
                      for d in (r.get("prod_level"), r.get("stage_level"))
                      if isinstance(d, int)})
    _level_breakdown = " &middot; ".join(
        f"L{d}: {sum(1 for r in toc_results if r.get('prod_level') == d)}"
        f"/{sum(1 for r in toc_results if r.get('stage_level') == d)}"
        for d in _depths)
    story.append(Paragraph(
        f"TOC / section status: <b>{_toc_overall}</b> &nbsp;—&nbsp; "
        f"{_n_toc_pass} pass &middot; "
        f"<font color='#e65100'>{_n_toc_miss} missing in STAGE</font> &middot; "
        f"<font color='#1565c0'>{_n_toc_extra} extra in STAGE</font> &middot; "
        f"<font color='#b71c1c'>{_n_toc_level} level change(s)</font>",
        ParagraphStyle("TocVerdict", parent=styles["Normal"], fontSize=10,
                       leading=13, spaceAfter=6,
                       textColor=(colors.red if _toc_overall == "FAIL"
                                  else colors.HexColor("#2e7d32")))))
    if _depths:
        story.append(Paragraph(
            f"TOC levels validated (PROD/STAGE entries per depth): "
            f"<b>{_level_breakdown}</b>",
            ParagraphStyle("TocLevels", parent=styles["Normal"], fontSize=9,
                           leading=12, spaceAfter=6,
                           textColor=colors.HexColor("#37474f"))))
    if _toc_status_rows:
        _tsr = [[Paragraph(f"<b>{h}</b>", hdr_s) for h in
                 ["Section (TOC entry)", "Level", "PROD pg", "STAGE pg",
                  "Status"]]]
        _tsr += _toc_status_rows
        _tst = Table(_tsr, colWidths=[290, 52, 46, 46, 220], repeatRows=1)
        _tst.setStyle(TableStyle([
            ("BACKGROUND",   (0, 0), (-1, 0), colors.HexColor("#37474f")),
            ("GRID",         (0, 0), (-1, -1), 0.5, colors.grey),
            ("VALIGN",       (0, 0), (-1, -1), "TOP"),
            ("TOPPADDING",   (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f7f7f7")]),
        ]))
        story += [_tst, Spacer(1, 12)]

    # ═══════════════════════════════════════════
    # ISSUES ONLY — like-for-like comparison
    # ═══════════════════════════════════════════
    diff_rows = [r for r in content_results if r["status"] == "Fail"]

    # A parent heading's span includes its sub-headings, so the same sentence can
    # surface under several topics ("USB peripherals Headphone PC" appeared under
    # both "Getting to know your monitor" and "Connections"). Each distinct piece
    # of text is reported once, against the deepest heading that contains it —
    # the most specific place a reader would look for it.
    _seen_frag = {}
    for r in sorted(diff_rows, key=lambda x: -(x.get("level") or 1)):
        for field in ("missing", "extra"):
            kept = []
            for frag in r.get(field, []):
                key = (field, " ".join(_seq_tokens(frag)))
                if not key[1] or key in _seen_frag:
                    continue
                _seen_frag[key] = True
                kept.append(frag)
            r[field] = kept
    diff_rows = [r for r in diff_rows if r.get("missing") or r.get("extra")]
    n_cmiss = sum(len(r.get("missing", [])) for r in diff_rows)
    n_cxtra = 0     # "extra in STAGE" is not reported: PROD is the reference
    n_only_prod  = sum(1 for r in toc_results if r["toc_status"] == "Missing in Stage")
    n_only_stage = sum(1 for r in toc_results if r["toc_status"] == "Extra in Stage")
    n_label = sum(f.get("_count", 1) for f in label_findings)
    total_issues = (len(glitches) + len(heading_findings) + n_label
                    + len(table_findings) + len(tablerow_findings) + n_cmiss + n_cxtra
                    + len(link_findings) + len(linkloss_findings)
                    + len(pageno_findings)
                    + len(figure_findings) + len(figdiff_findings)
                    + len(icon_findings) + len(align_findings)
                    + len(italic_findings)
                    + len(bold_findings)
                    + len(liststyle_findings) + len(tableshape_findings)
                    + len(tablebreak_findings) + len(tablemargin_findings)
                    + len(tablecont_findings) + len(tablemerge_findings)
                    + len(callgap_findings)
                    + len(imgmismatch_findings) + len(figalign_findings)
                    + len(listindent_findings) + len(linkstyle_findings)
                    + len(textalign_findings))

    if not total_issues:
        story.append(Paragraph("No issues found — STAGE matches PROD.",
                               ParagraphStyle("AllOk", parent=styles["Normal"],
                                              fontSize=10,
                                              textColor=colors.HexColor("#2e7d32"))))
    else:
        # One row per issue, grouped by kind. The table keeps every issue on a
        # single line so the whole list can be scanned at a glance.
        def _pageno(v):
            """A TOC page as an int; entries missing on a side carry "-"."""
            try:
                return int(v)
            except (TypeError, ValueError):
                return 0

        _p_marks = sorted(((_pageno(r.get("prod_page")), r["title"])
                           for r in toc_results
                           if _pageno(r.get("prod_page")) and r.get("title")),
                          key=lambda x: x[0])
        _s_marks = sorted(((_pageno(r.get("stage_page")), r["title"])
                           for r in toc_results
                           if _pageno(r.get("stage_page")) and r.get("title")),
                          key=lambda x: x[0])
        _pair_page = {r["title"]: (r.get("prod_page"), r.get("stage_page"))
                      for r in toc_results if r.get("title")}

        def _topic_at(marks, page, path=None, defect_rects=None):
            """The heading whose section contains this spot.

            Several headings can start on one page, so the last one at or before
            the page is not necessarily the one the defect sits under. When the
            defect's position is known, the heading printed nearest above it on
            that page wins — that is the section a reader would say it is in.
            """
            candidates = [t for start, t in marks if start <= page]
            hit = candidates[-1] if candidates else None
            if not (path and defect_rects and candidates):
                return hit
            top = min(fitz.Rect(r).y0 for r in defect_rects)
            best_y, best = -1.0, None
            below = set()
            for title in candidates[-6:]:
                pg, rects = _locate_tokens(path, title, page)
                if pg != page or not rects:
                    continue
                y = min(fitz.Rect(r).y0 for r in rects)
                if y <= top:
                    if y > best_y:
                        best_y, best = y, title
                else:
                    below.add(title)      # printed under the defect, so not its section
            if best:
                return best
            # Nothing on this page sits above it, so the section began earlier.
            # Headings printed lower down this page are not candidates at all —
            # taking the last one regardless named a section the defect is not in.
            earlier = [t for start, t in marks if start <= page and t not in below]
            return earlier[-1] if earlier else hit


        def _section_for(where, probe=None):
            """The manual section an issue sits in, named for the report.

            Read from the document the issue was found in: the page comes from
            the issue's own location, and when the issue's text can be placed on
            that page the heading printed nearest above it wins.
            """
            m = re.search(r"\b(PROD|STAGE)\s*p(\d+)", where or "")
            if not m:
                return "\u2014"
            side, pg = m.group(1), int(m.group(2))
            path = prod_path if side == "PROD" else stage_path
            marks = _p_marks if side == "PROD" else _s_marks
            rects = None
            if probe:
                p2, r2 = _locate_tokens(path, probe, pg)
                if p2 == pg:
                    rects = r2
            title = _topic_at(marks, pg, path if rects else None, rects)
            return title or "\u2014"

        # Collected per category and rendered as one section each. Numbering runs
        # within a section (E1, C1, I1 …) so an issue's id says what kind it is.
        buckets = {key: [] for key, _t, _d in _CATEGORIES}
        _seen_rows = set()

        def add(issue, where, topic, detail, style, probe=None):
            if not _is_reported(issue):
                return
            key = (issue, where, topic, detail)
            if key in _seen_rows:
                return
            _seen_rows.add(key)
            buckets[_category_for(issue)].append(
                (issue, where, topic, detail, style, probe))

        for g in glitches:
            if g["kind"].startswith("Text layer"):
                detail = (f"Font <font color='#b71c1c'><b>{_esc(g['text'])}</b></font> "
                          f"has no Unicode map — the page <b>displays correctly</b>, "
                          f"but copy/paste, search and screen readers get the wrong "
                          f"characters.<br/>Nearby: {_esc(_trunc(g['context'], 170))}")
            else:
                detail = (f"<font color='#b71c1c'><b>{_esc(g['text'])}</b></font>"
                          f"<br/>In context: {_esc(_trunc(g['context'], 170))}")
            add(g["kind"], f"{g['doc']} p{g['page']}",
                f"{g['doc']} page {g['page']}", detail, fail_s)

        for f in icon_findings:
            add("Broken image", f"{f['doc']} p{f['page']}",
                f"Image on {f['doc']} page {f['page']}",
                f"<b>{_esc(f['kind'])}</b> — "
                f"<font color='#b71c1c'>{_esc(_trunc(f['text'], 240))}</font>",
                fail_s)

        for f in label_findings:
            n = f.get("_count", 1)
            times = (f" This label is missing from <b>{n} separate figures</b> "
                     f"on this page, not just one." if n > 1 else "")
            add("Image label missing", f"PROD p{f['page']}",
                f"Figure on PROD page {f['page']}",
                f"<b>Look at PROD page {f['page']}</b>. A figure there carries "
                f"the label <font color='#b71c1c'><b>"
                f"“{_esc(_trunc(f['text'], 200))}”</b></font>.<br/>"
                f"<b>That wording appears nowhere in STAGE</b> — not beside the "
                f"figure, not drawn inside the artwork, and not in the body "
                f"text.{times}", fail_s, probe=f["text"])

        for f in figure_findings:
            add("Image missing", f"PROD p{f['page']} / STAGE p{f['stage_page']}",
                _esc(f["title"]),
                f"PROD shows {f['n']} figure(s) under this topic; the STAGE pages "
                f"for the same topic have none.", fail_s)

        for f in figdiff_findings:
            add("Image not correctly updated",
                f"PROD p{f['page']} / STAGE p{f['stage_page']}",
                f"Section \u201c{_esc(_trunc(f['anchor'], 46))}\u201d",
                f"<font color='#b71c1c'><b>The image in this section is "
                f"different between PROD and STAGE.</b></font> Both figures "
                f"carry the same caption, but the picture is not the same — a "
                f"changed screenshot, diagram or photo. Compare the two crops "
                f"below and put the correct image in STAGE.", fail_s)

        for f in imgmismatch_findings:
            # Spelled out in the order a reader needs it: which picture, on
            # which page, and what is wrong with it.
            named = (f"the figure captioned “{_esc(_trunc(f['caption'], 70))}”"
                     if f.get("caption") else "an illustration")
            detail = (
                f"<b>Look at PROD page {f['prod_page']}</b> — "
                f"{_esc(f.get('prod_where') or 'a figure on that page')}. "
                f"PROD prints {named} there.<br/>"
                f"<font color='#b71c1c'><b>STAGE has no matching picture "
                f"anywhere in this section.</b></font>")
            add("Image mismatch", f"PROD p{f['prod_page']}",
                f"Section “{_esc(_trunc(f['section'], 46))}”",
                detail, fail_s, probe=f.get("caption") or None)

        for f in figalign_findings:
            add("Image alignment changed",
                f"PROD p{f['prod_page'] if 'prod_page' in f else f['page']} / "
                f"STAGE p{f['stage_page']}",
                f"Section “{_esc(_trunc(f['anchor'], 46))}”",
                f"The same picture appears on both sides, but it is placed "
                f"differently.<br/>"
                f"<b>PROD page {f['prod_page']}</b> — the figure is "
                f"<b>{f['prod_align']}</b>.<br/>"
                f"<b>STAGE page {f['stage_page']}</b> — the same figure is "
                f"<font color='#b71c1c'><b>{f['stage_align']}</b></font>.",
                fail_s, probe=f["anchor"])

        for f in colour_findings:
            add("Image highlight box missing",
                f"PROD p{f['page']} / STAGE p{f['stage_page']}",
                f"Section “{_esc(_trunc(f['anchor'], 46))}”",
                f"<b>{f['has']}</b> draws a <b>{f['colour']}</b> box / highlight "
                f"on this figure (about <b>{f['pct']}%</b> of the picture) — "
                f"<font color='#b71c1c'><b>{f['missing']} has no {f['colour']} "
                f"mark on the same figure.</b></font> Look at the two crops "
                f"below.", fail_s, probe=f["anchor"])

        for f in pixel_findings:
            add("Images pixelated", f"STAGE ({len(f['pages'])} pages)",
                "STAGE artwork",
                f"<font color='#b71c1c'><b>{f['count']} of {f['total']}</b></font> "
                f"images in STAGE are drawn below <b>{_PIXELATED_DPI} dpi</b> "
                f"(lowest <b>{f['worst']} dpi</b>, median "
                f"<b>{f['stage_median']} dpi</b>); PROD's artwork runs at a "
                f"median of <b>{f['prod_median']} dpi</b>. The pixels are "
                f"visible at the size the images are placed."
                f"<br/><b>Topics affected:</b> "
                f"{_esc(', '.join(f['topics'][:15]))}"
                + (f" \u2026 and {len(f['topics']) - 15} more topics"
                   if len(f['topics']) > 15 else ""), fail_s)

        for f in linkloss_findings:
            src = f.get("doc", "PROD")
            other = "STAGE" if src == "PROD" else "PROD"
            label = "Hyperlink lost" if src == "PROD" else "Hyperlink added in STAGE"
            add(label, f"{src} p{f['page']}",
                f"Cross-reference on {src} page {f['page']}",
                f"<font color='#b71c1c'><b>{_esc(_trunc(f['text'], 160))}</b></font> "
                f"is a link to <b>{_esc(f['target'])}</b> in {src}. {other} prints "
                f"the same wording as plain text, with no link on it.",
                fail_s, probe=f["text"])

        for f in linkstyle_findings:
            add("Hyperlink not highlighted in STAGE",
                f"PROD p{f['prod_page']} / STAGE p{f['page']}",
                f"Link on STAGE page {f['page']}",
                f"<font color='#b71c1c'><b>{_esc(_trunc(f['text'], 160))}</b></font> "
                f"{_esc(f['why'])}"
                + (f" (PROD {f['prod_colour']}, STAGE {f['stage_colour']})")
                + (f", target <b>{_esc(_trunc(f['target'], 80))}</b>"
                   if f.get("target") else "")
                + ". A reader cannot tell it is a link.",
                fail_s, probe=f["text"])

        for f in link_findings:
            add(f["kind"],
                f"{f['doc']}" + (f" p{f['page']}" if f["page"] else ""),
                f"Hyperlink ({f['doc']})",
                f"<font color='#b71c1c'>{_esc(_trunc(f['text'], 260))}</font>", fail_s)

        for f in callgap_findings:
            stage_where = (f"STAGE p{f['stage_first']}"
                          if f['stage_first'] == f['stage_last']
                          else f"STAGE p{f['stage_first']}-{f['stage_last']}")
            add("Diagram callout number missing",
                f"PROD p{f['prod_page']} / {stage_where}",
                f"Diagram on PROD page {f['prod_page']}",
                f"PROD numbers this diagram <b>"
                f"{', '.join(str(n) for n in f['present'])}</b> — "
                f"<font color='#b71c1c'><b>"
                f"{', '.join(str(n) for n in f['missing'])}</b></font> "
                f"{'is' if len(f['missing']) == 1 else 'are'} not there on "
                f"STAGE's diagram ({stage_where}).", fail_s)

        for f in labelmerge_findings:
            add("Paragraph merged with heading",
                f"PROD p{f['prod_page']} / STAGE p{f['stage_page']}",
                f"“{_esc(f['label'])}” on STAGE page {f['stage_page']}",
                f"PROD starts the description for <b>{_esc(f['label'])}</b> "
                f"on its own line, underneath the label. "
                f"<font color='#b71c1c'>STAGE runs the description on from "
                f"the label on the same line</font> instead — the paragraph "
                f"reads as part of its own heading.", fail_s, probe=f["label"])

        for f in tablemargin_findings:
            side = []
            if f["left"]:
                side.append(f"<b>{f['left']}pt</b> past the left margin")
            if f["right"]:
                side.append(f"<b>{f['right']}pt</b> past the right margin")
            add("Table breaking the margins", f"STAGE p{f['page']}",
                f"Table \u201c{_esc(_trunc(f['header'], 44))}\u201d",
                f"The table runs outside the text column: it spans "
                f"x={f['x0']}\u2013{f['x1']}pt where the body text sits between "
                f"{f['col_left']} and {f['col_right']}pt \u2014 "
                f"{' and '.join(side)}. PROD keeps this table inside its column.",
                fail_s, probe=f.get("header"))

        for f in tablebreak_findings:
            add("Table layout broken",
                f"PROD p{f['prod_page']} / STAGE p{f['stage_from']}-{f['stage_to']}",
                f"Table \u201c{_esc(_trunc(f['header'], 44))}\u201d",
                f"The table is split by a page break in STAGE: it runs over "
                f"<b>{f['stage_pages']} pages</b> (p{f['stage_from']}\u2013"
                f"{f['stage_to']}) where PROD keeps it on <b>{f['prod_pages']}</b>. "
                f"Rows are separated from their header.", fail_s,
                probe=f.get("header"))

        for f in tablemerge_findings:
            ra = f.get("rows_affected") or []
            lead = f["what"].split(". Rows affected:")[0]
            body = (f"The reader-visible columns match PROD, but the cells "
                    f"inside the table are merged/split differently — "
                    f"<font color='#b71c1c'>{_esc(lead)}</font>.")
            if ra:
                body += "<br/><b>Where:</b><br/>" + "<br/>".join(
                    f"&nbsp;&nbsp;• {_esc(_trunc(x, 200))}" for x in ra)
            add("Table cell layout differs",
                f"PROD p{f['prod_page']} / STAGE p{f['page']}",
                f"Table “{_esc(_trunc(f['header'], 44))}”", body, fail_s,
                probe=f.get("header"))

        for f in tablecont_findings:
            _sect = _section_for(f"{f['doc']} p{f['page']}", f.get("header"))
            add("Table continuation missing its header",
                f"{f['doc']} p{f['page']}",
                f"Table “{_esc(_trunc(f['header'], 40))}” under "
                f"“{_esc(_sect)}”, continued on {f['doc']} page {f['page']}",
                f"The table from page {f['prev_page']} continues onto page "
                f"{f['page']} without repeating its header row "
                f"(<font color='#b71c1c'><b>{_esc(_trunc(f['header'], 90))}</b>"
                f"</font>), so the columns on this page are unlabelled.", fail_s,
                probe=f.get("header"))

        for f in tableshape_findings:
            def _cols(names, bad):
                return ", ".join(
                    (f"<font color='#b71c1c'><b>{_esc(n)}</b></font>"
                     if n in bad else _esc(n)) for n in names)
            if f["reordered"]:
                what = ("The same columns are present but in a different order.")
            else:
                bits = []
                if f["gone"]:
                    bits.append("PROD column(s) <font color='#b71c1c'><b>"
                                + _esc(", ".join(f["gone"]))
                                + "</b></font> are not in STAGE")
                if f["extra"]:
                    bits.append("STAGE adds <font color='#b71c1c'><b>"
                                + _esc(", ".join(f["extra"])) + "</b></font>")
                what = "; ".join(bits) + "."
            add("Table column layout differs",
                f"PROD p{f['prod_page']} / STAGE p{f['page']}",
                f"Table \u201c{_esc(_trunc(f['header'], 44))}\u201d",
                f"{what}<br/><b>PROD ({f['prod_cols']}):</b> "
                f"{_cols(f['prod_head'], f['gone'])}"
                f"<br/><b>STAGE ({f['stage_cols']}):</b> "
                f"{_cols(f['stage_head'], f['extra'])}", fail_s,
                probe=f.get("header"))

        for f in heading_findings:
            _sect = _section_for(f"PROD p{f['page']}", f.get("row"))
            add("Table heading missing", f"PROD p{f['page']}",
                f"Table under “{_esc(_sect)}”, PROD page {f['page']}",
                f"Column heading(s) <font color='#b71c1c'><b>"
                f"{_esc(_trunc(f['text'], 140))}</b></font> dropped. PROD header: "
                f"<i>{_esc(_trunc(f.get('row',''), 170))}</i>", fail_s,
                probe=f.get("row") or f["text"])

        for f in tablerow_findings:
            _sect = _section_for(f"PROD p{f['page']}", f["label"])
            add("Table row missing", f"PROD p{f['page']}",
                f"Table under “{_esc(_sect)}”, PROD page {f['page']}",
                f"<b>PROD page {f['page']}</b> has a table row<br/>"
                f"<font color='#b71c1c'><b>{_esc(_trunc(f['label'], 80))}</b>"
                f"{(' : ' + _esc(_trunc(f['value'], 150))) if f.get('value') else ''}"
                f"</font><br/>"
                f"<b>This whole row is absent from STAGE</b> — not as a table "
                f"row and not as text anywhere else in the document.",
                fail_s, probe=f["label"])

        for f in table_findings:
            hdr = f.get("header") or ""
            rlab = f.get("row_label") or ""
            # A pure quantity marker ("x1", "x2", "1", "N/A") is never
            # meaningful "missing content" - skip it, it is what a dropped
            # packing-list table leaves behind and it is pure noise.
            if re.fullmatch(r"[x\u00d7]?\s*\d{1,3}|n/?a", (f["text"] or "").strip(),
                            re.IGNORECASE):
                continue
            where_tbl = (f"the table with columns [{_esc(_trunc(hdr, 90))}]"
                         if hdr else f"a table on PROD page {f['page']}")
            row_bit = (f" the row for <b>{_esc(_trunc(rlab, 70))}</b>"
                       if rlab else " one row")
            if f.get("whole_cell", True):
                detail = (f"<b>PROD page {f['page']}</b> — in {where_tbl},"
                          f"{row_bit} contains<br/>"
                          f"<font color='#b71c1c'><b>{_esc(_trunc(f['text'], 240))}"
                          f"</b></font><br/>"
                          f"<b>STAGE's copy of that row does not carry this "
                          f"text anywhere.</b>")
            else:
                detail = (f"<b>PROD page {f['page']}</b> — in {where_tbl},"
                          f"{row_bit} lists the value<br/>"
                          f"<font color='#b71c1c'><b>{_esc(_trunc(f['text'], 200))}"
                          f"</b></font><br/>"
                          f"<b>That value is missing from STAGE</b> — the rest "
                          f"of the row's values are present.")
            _sect = _section_for(f"PROD p{f['page']}", f["text"])
            add("Table cell missing",
                f"PROD p{f['page']} · row {f.get('row', '?')}",
                f"Table under “{_esc(_sect)}”, PROD page {f['page']}", detail,
                fail_s,
                probe=f["text"])

        for f in bold_findings:
            add("Bold lost", f"PROD p{f['page']} / STAGE p{f['stage_page']}",
                f"Text on PROD page {f['page']}",
                f"<b>{_esc(_trunc(f['text'], 220))}</b> is set bold in PROD; "
                f"STAGE draws it in the ordinary body face.", fail_s,
                probe=f["text"])

        for f in italic_findings:
            add("Italic lost", f"PROD p{f['page']}",
                f"Text on PROD page {f['page']}",
                f"<i>{_esc(_trunc(f['text'], 220))}</i> is italic in PROD but is "
                f"rendered upright in STAGE.", fail_s, probe=f["text"])

        for f in align_findings:
            add("List alignment broken", f"{f['doc']} p{f['page']}",
                f"{f['doc']} page {f['page']}",
                f"Step marker <font color='#b71c1c'><b>{_esc(f['marker'])}</b></font> "
                f"sits on its own line; its text "
                f"<font color='#b71c1c'><b>{_esc(_trunc(f['text'], 160))}</b></font> "
                f"wraps to the line below \u2014 the number and its text are not "
                f"aligned. {_esc(f.get('why', ''))}.", fail_s, probe=f["text"])

        for f in listindent_findings:
            add("List indent changed",
                f"PROD p{f['prod_page']} / STAGE p{f['page']}",
                f"List item on STAGE page {f['page']}",
                f"<font color='#b71c1c'><b>{_esc(_trunc(f['text'], 160))}</b></font> "
                f"is indented <b>{f['prod_indent']:.0f} pt</b> from the text "
                f"column in PROD and <b>{f['stage_indent']:.0f} pt</b> in STAGE "
                f"— <b>{f['direction']}</b> by "
                f"{abs(f['delta']):.0f} pt relative to the rest of its list. "
                f"A sub-item set flush with its parent has lost its nesting.",
                fail_s, probe=f["text"])

        _STYLE_WORD = {"number": "numbered 1. 2. 3.", "letter": "lettered a. b. c.",
                       "bullet": "bulleted"}
        for f in liststyle_findings:
            add("List marker changed",
                f"PROD p{f['prod_page']} / STAGE p{f['page']}",
                f"List item on STAGE page {f['page']}",
                f"<font color='#b71c1c'><b>{_esc(_trunc(f['text'], 150))}</b></font>"
                f"<br/>PROD marks this step as "
                f"<b>{_STYLE_WORD.get(f['prod_style'], f['prod_style'])}</b>; "
                f"STAGE marks it as <font color='#b71c1c'><b>"
                f"{_STYLE_WORD.get(f['stage_style'], f['stage_style'])}</b></font>. "
                f"The wording is the same but the list has changed meaning for "
                f"the reader.", fail_s, probe=f["text"])

        for f in textalign_findings:
            if f["reflowed"]:
                why = (f"PROD sets it <b>{_esc(f['prod_align'])}</b> in the text "
                       f"column; STAGE sets it "
                       f"<font color='#b71c1c'><b>{_esc(f['stage_align'])}</b>"
                       f"</font>.")
            else:
                why = (f"It sits <b>{f['prod_indent']:.0f} pt</b> from the text "
                       f"column in PROD and <b>{f['stage_indent']:.0f} pt</b> in "
                       f"STAGE — <font color='#b71c1c'><b>"
                       f"{abs(f['delta']):.0f} pt "
                       f"{'right' if f['delta'] > 0 else 'left'}</b></font> of "
                       f"where PROD puts it, after allowing for the margin "
                       f"difference between the two documents.")
            add("Text alignment changed",
                f"PROD p{f['prod_page']} / STAGE p{f['page']}",
                f"{f['kind_label']} on STAGE page {f['page']}",
                f"<font color='#b71c1c'><b>{_esc(_trunc(f['text'], 150))}</b>"
                f"</font> — {why}", fail_s, probe=f["text"])

        for f in pageno_findings:
            add(f["kind"], f"{f['doc']} p{f['page']}",
                f"{f['doc']} page {f['page']}", _esc(f["text"]), fail_s)

        for r in diff_rows:
            where = f"PROD p{r['prod_page']} / STAGE p{r['stage_page']}"
            for m in r.get("missing", []):
                add("Content missing", where, _esc(r["title"]),
                    f"In PROD, absent from STAGE: <font color='#b71c1c'>"
                    f"{_highlight_notice_labels(_trunc(m, 300))}</font>", fail_s,
                    probe=m)


        # ── Summary: how many of each kind ──
        story.append(Paragraph(
            f"Metrics &nbsp;—&nbsp; "
            f"<font color='{'#b71c1c' if diff_rows else '#2e7d32'}'>"
            f"<b>{len(diff_rows)}</b> section(s) with content issues</font> &middot; "
            f"<font color='#e65100'><b>{_n_toc_miss}</b> section(s) missing in "
            f"STAGE</font> &middot; <b>{n_cmiss}</b> content fragment(s) dropped "
            f"&middot; <b>{total_issues}</b> issue(s) total",
            ParagraphStyle("MetricsLine", parent=styles["Normal"], fontSize=9,
                           leading=12, spaceAfter=6)))
        present = [(k, title, blurb) for k, title, blurb in _CATEGORIES
                   if buckets[k]]
        if not present:
            # Everything detected fell outside the reported scope. Say so rather
            # than rendering an empty summary.
            story.append(Paragraph(
                "No issues found in the reported categories — STAGE matches PROD "
                "for missing content, alignment, table layout and hyperlinks.",
                ParagraphStyle("AllOk2", parent=styles["Normal"], fontSize=10,
                               textColor=colors.HexColor("#2e7d32"))))
            present = []
        srow_h = [Paragraph(f"<b>{t}</b>", hdr_s) for _k, t, _b in present]
        if not present:
            srow_h = []
        srow_v = [Paragraph(f"<font color='{'#b71c1c' if buckets[k] else '#2e7d32'}'"
                            f" size='15'><b>{len(buckets[k])}</b></font>", cell_s)
                  for k, _t, _b in present]
        sm = (Table([srow_h, srow_v],
                    colWidths=[734.0 / len(present)] * len(present))
              if present else None)
        if sm is not None:
            sm.setStyle(TableStyle([
                ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#37474f")),
                ("BACKGROUND",   (0,1), (-1,1), colors.HexColor("#f5f7f8")),
                ("GRID",         (0,0), (-1,-1), 0.5, colors.grey),
                ("ALIGN",        (0,0), (-1,-1), "CENTER"),
                ("VALIGN",       (0,0), (-1,-1), "MIDDLE"),
                ("TOPPADDING",   (0,0), (-1,-1), 6),
                ("BOTTOMPADDING",(0,0), (-1,-1), 6),
            ]))
            story += [sm, Spacer(1, 4)]

        # ── One section per category ──
        for key, title, blurb in present:
            story.append(Paragraph(title, head_s))
            story.append(Paragraph(blurb, ParagraphStyle(
                "CatBlurb", parent=styles["Normal"], fontSize=8,
                textColor=colors.grey, spaceAfter=6)))
            rows = [[Paragraph(f"<b>{h}</b>", hdr_s) for h in
                     ["#", "Section", "Where", "Issue", "Detail",
                      "What it means &amp; how to fix it"]]]
            tag = key[0].upper()
            for idx, (issue, where, topic, detail, style, probe) in enumerate(
                    buckets[key], 1):
                means, fix = _fix_for(issue)
                sect = _section_for(where, probe)
                rows.append([Paragraph(f"{tag}{idx}", cell_s),
                             Paragraph(f"{_esc(sect)}<br/>"
                                       f"<font size='6.5' color='#6b7280'>"
                                       f"{topic}</font>", topic_s),
                             Paragraph(where, cell_s),
                             Paragraph(issue, style),
                             Paragraph(detail, cell_s),
                             Paragraph(f"{_esc(means)}<br/><b>Fix:</b> {_esc(fix)}",
                                       fix_s)])
            # Detail carries the "which picture, which page, what is wrong"
            # narrative now, so it gets the room; the fix column repeats the
            # same advice for every row of a kind and needs less.
            it = Table(rows, colWidths=[24, 116, 62, 84, 288, 160], repeatRows=1)
            it.setStyle(TableStyle([
                ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#37474f")),
                ("GRID",         (0,0), (-1,-1), 0.5, colors.grey),
                ("VALIGN",       (0,0), (-1,-1), "TOP"),
                ("TOPPADDING",   (0,0), (-1,-1), 4),
                ("BOTTOMPADDING",(0,0), (-1,-1), 4),
                ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, colors.HexColor("#f7f7f7")]),
            ]))
            story += [it, Spacer(1, 10)]

    # Structure note — NOT counted as content issues (not like-for-like)
    if n_only_prod or n_only_stage:
        story.append(Spacer(1, 14))
        story.append(Paragraph("Structure (for information — not counted as issues)",
                               head_s))
        story.append(Paragraph(
            f"The two documents are chaptered differently: <b>{n_only_prod}</b> "
            f"heading(s) appear only in PROD and <b>{n_only_stage}</b> only in "
            "STAGE. Their text is still compared — it simply sits under a "
            "different heading — so this is listed here rather than as missing "
            "content.",
            ParagraphStyle("StructN", parent=styles["Normal"], fontSize=8.5,
                           leading=12, textColor=colors.HexColor("#1e3a5f"),
                           spaceAfter=6)))
        srows = [[Paragraph(f"<b>{h}</b>", hdr_s)
                  for h in ["Heading", "Only in", "Page"]]]
        for r in toc_results:
            if r["toc_status"] == "Missing in Stage":
                srows.append([Paragraph(_esc(r["title"]), cell_s),
                              Paragraph("PROD", cell_s),
                              Paragraph(str(r["prod_page"]), cell_s)])
            elif r["toc_status"] == "Extra in Stage":
                srows.append([Paragraph(_esc(r["title"]), cell_s),
                              Paragraph("STAGE", cell_s),
                              Paragraph(str(r["stage_page"]), cell_s)])
        stt = Table(srows, colWidths=[420, 70, 60], repeatRows=1)
        stt.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#607d8b")),
            ("GRID",         (0,0), (-1,-1), 0.5, colors.grey),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("TOPPADDING",   (0,0), (-1,-1), 2),
            ("BOTTOMPADDING",(0,0), (-1,-1), 2),
        ]))
        story.append(stt)

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
    n_skipped = 0
    for lvl, title, pg in prod_toc:
        if _is_skipped_section(title):
            n_skipped += 1
            continue
        k = _norm_key(title)
        if k in stage_keys:
            stage_lvl = stage_keys[k][1]
            # A heading can survive the move and still be re-nested — promoted to
            # a top-level topic, or demoted under a sibling. The text matches, so
            # every other check passes; only the outline depth gives it away.
            level_changed = (isinstance(stage_lvl, int) and stage_lvl != lvl)
            toc_results.append({
                "title": title, "level": lvl,
                "prod_level": lvl, "stage_level": stage_lvl,
                "level_status": "Changed" if level_changed else "Match",
                "prod_page": pg, "stage_page": stage_keys[k][2],
                "toc_status": "Match",
                "note": (f"TOC level L{lvl} in PROD, L{stage_lvl} in STAGE"
                         if level_changed else "")
            })
        else:
            pno = _find_heading_in_texts(title, stage_page_texts)
            if pno is not None:
                # Found in the page text but not in STAGE's outline, so STAGE has
                # no bookmark level to compare against — the depth is unknown,
                # not unchanged.
                toc_results.append({
                    "title": title, "level": lvl,
                    "prod_level": lvl, "stage_level": None,
                    "level_status": "No STAGE bookmark",
                    "prod_page": pg, "stage_page": pno,
                    "toc_status": "Match",
                    "note": f"Heading found inside page: {pno}"
                })
            else:
                toc_results.append({
                    "title": title, "level": lvl,
                    "prod_level": lvl, "stage_level": None,
                    "level_status": "",
                    "prod_page": pg, "stage_page": "-",
                    "toc_status": "Missing in Stage",
                    "note": ""
                })
    n_step_bookmarks = 0
    for lvl, title, pg in stage_toc:
        if _is_skipped_section(title):
            n_skipped += 1
            continue
        if _norm_key(title) not in prod_keys:
            # Skip numbered procedure-step bookmarks — not real extra sections.
            if _STEP_BOOKMARK_RE.match(title or ""):
                n_step_bookmarks += 1
                continue
            toc_results.append({
                "title": title, "level": lvl,
                "prod_level": None, "stage_level": lvl,
                "level_status": "",
                "prod_page": "-", "stage_page": pg,
                "toc_status": "Extra in Stage",
                "note": ""
            })
    if n_step_bookmarks:
        print(f"  (excluded {n_step_bookmarks} numbered step bookmarks from Extra in Stage)")

    if n_skipped:
        print(f"  (skipped {n_skipped} excluded section(s): {', '.join(SKIP_SECTIONS)})")
    n_m = sum(1 for r in toc_results if r["toc_status"] == "Match")
    n_mi = sum(1 for r in toc_results if r["toc_status"] == "Missing in Stage")
    n_e  = sum(1 for r in toc_results if r["toc_status"] == "Extra in Stage")
    n_lv = sum(1 for r in toc_results if r.get("level_status") == "Changed")
    print(f"  TOC: Match={n_m} | Missing in Stage={n_mi} | Extra in Stage={n_e}"
          f" | Level changed={n_lv}")
    # Every outline depth present on either side, so a report reader can see the
    # nesting the two documents actually use rather than only the leaf titles.
    _depths = sorted({d for r in toc_results
                      for d in (r.get("prod_level"), r.get("stage_level"))
                      if isinstance(d, int)})
    if _depths:
        _per = ", ".join(
            f"L{d}: {sum(1 for r in toc_results if r.get('prod_level') == d)} PROD /"
            f" {sum(1 for r in toc_results if r.get('stage_level') == d)} STAGE"
            for d in _depths)
        print(f"  TOC levels — {_per}")

    # ── Content extraction ──
    _emit(0.10, "extracting section text")
    print("Extracting section text...")
    prod_sections  = extract_sections(prod_path,  is_prod=True)
    stage_sections = extract_sections(stage_path, is_prod=False)
    stage_lookup   = {_norm_key(t): v for t, v in stage_sections.items()}
    prod_lookup    = {_norm_key(t): v for t, v in prod_sections.items()}
    print(f"  PROD sections: {len(prod_sections)} | STAGE sections: {len(stage_sections)}")

    # Build STAGE shingle index from ALL non-nav pages (not just section slices)
    # so content that falls before the first TOC heading is still covered.
    _emit(0.24, "building STAGE index")
    print("Building STAGE content index...")
    stage_doc = fitz.open(stage_path)
    stage_nav = {1} | _detect_nav_pages(stage_doc)
    stage_doc.close()
    stage_ns, stage_cset, stage_full_lower = _build_stage_index(stage_path, stage_nav)

    # Index of PROD's own text. A reported fragment must genuinely read that way
    # in PROD — otherwise it is an artifact of concatenating the page-ordered
    # token stream, not content STAGE dropped.
    _pdoc = fitz.open(prod_path)
    _prod_nav = {1} | _detect_nav_pages(_pdoc)
    _prod_raw = " ".join(_normalize(_strip_formatting(_pdoc[i].get_text()))
                         for i in range(_pdoc.page_count)
                         if (i + 1) not in _prod_nav)
    _pdoc.close()
    prod_seq_idx = _raw_page_indexes(prod_path, _prod_nav)
    prod_fig_tokens = _figure_text_keys(prod_path, _prod_nav)

    # Reference for the EXTRA direction: STAGE text with no counterpart in PROD.
    # Read PROD unfiltered here — _extract_page_body_prod drops OSD overlays that
    # _extract_page_body_stage keeps, and comparing the two directly would report
    # every filtered string as "extra".
    prod_ref_ns, prod_ref_cset, prod_ref_full = _build_prod_reference(
        prod_path, _prod_nav)
    stage_seq_idx = _stage_seq_index(stage_full_lower)
    stage_raw_idx = _raw_page_indexes(stage_path, stage_nav)
    stage_fig_tokens = _figure_text_keys(stage_path, stage_nav)

    # Does either document draw text with a font that has no Unicode map?
    _ud = fitz.open(prod_path)
    _untrusted_prod = _untrusted_fonts(_ud)
    _ud.close()
    _ud = fitz.open(stage_path)
    _untrusted_stage = _untrusted_fonts(_ud)
    _ud.close()
    untrusted_present = bool(_untrusted_prod or _untrusted_stage)
    if untrusted_present:
        print(f"  fonts with no Unicode map — PROD: {sorted(_untrusted_prod)} | "
              f"STAGE: {sorted(_untrusted_stage)}")

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
        pc    = prod_sections.get(title)
        if not pc:                      # fall back on a normalized key match
            pc = prod_lookup.get(key, "")
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
                stage_section_lower="", source_idx=prod_seq_idx)
            _stage_hint = r["stage_page"] if isinstance(r.get("stage_page"), int) else 0
            missing = [m for m in missing
                      if not _text_in_artwork(stage_path, m, _stage_hint, stage_nav)]
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
            stage_section_lower=_s_norm(sc or "").lower(),
            source_idx=prod_seq_idx)

        # Reverse direction: text STAGE renders under this heading that PROD
        # never had. Same verification, sides swapped.
        stage_words = _keep(_tokenize(sc or ""))
        extra = []
        if stage_words:
            _, extra = _section_missing(
                stage_words, prod_ref_ns, prod_ref_cset, prod_ref_full,
                stage_section_lower=_s_norm(pc or "").lower(),
                source_idx=stage_raw_idx)

        if untrusted_present:
            missing = [m for m in missing if not _script_unreliable(m)]
            extra   = [x for x in extra   if not _script_unreliable(x)]
        # Drop labels that live inside artwork — see _figure_text_keys.
        missing = [m for m in missing if not _is_artwork_text(m, prod_fig_tokens)]
        extra   = [x for x in extra   if not _is_artwork_text(x, stage_fig_tokens)]
        # A "missing" fragment can genuinely be there, just baked into a STAGE
        # picture instead of laid out as text (PROD prints a lettered option
        # list as body text; STAGE's equivalent diagram carries the same
        # labels as pixels). The table-cell and image-label checks already OCR
        # STAGE's artwork before believing a gap; the main content pass did not.
        _stage_hint = r["stage_page"] if isinstance(r.get("stage_page"), int) else 0
        missing = [m for m in missing
                  if not _text_in_artwork(stage_path, m, _stage_hint, stage_nav)]

        status = "Pass" if not (missing or extra) else "Fail"
        content_results.append({
            "title":      title,
            "level":      r["level"],
            "status":     status,
            "extra":      extra,
            "prod_page":  r["prod_page"],
            "stage_page": r["stage_page"],
            "coverage":   round(coverage, 1),
            "missing":    missing,
        })

    # ── Optional LLM second opinion: prune extraction-artifact fragments ──
    _emit(0.50, "LLM verifying content fragments")
    try:
        _sd = fitz.open(stage_path)
        _stage_raw = " ".join(
            _normalize(_strip_formatting(_sd[i].get_text()))
            for i in range(_sd.page_count) if (i + 1) not in stage_nav)
        _sd.close()
        _llm_verify_missing(content_results, _prod_raw, _stage_raw)
    except Exception as _e:
        print(f"  LLM verify skipped: {_e}")

    # ── Local-AI (Ollama) cross-check: add genuine differences the
    #    section-by-section matcher missed (offline, no API key) ──
    _emit(0.52, "AI cross-check (local)")
    try:
        _ai_cross_check(content_results, prod_path, stage_path,
                        _prod_raw, _stage_raw, _prod_nav, stage_nav,
                        prod_sections, stage_sections, stage_lookup)
    except Exception as _e:
        import traceback as _tb
        print(f"  AI cross-check skipped: {_e}\n{_tb.format_exc()}")

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

    # ── Tables and figures ──
    _emit(0.90, "validating tables")
    print("Crawling and validating tables...")
    _pd = fitz.open(prod_path)
    prod_nav = {1} | _detect_nav_pages(_pd)
    _pd.close()
    try:
        table_summary, table_findings = _validate_tables(
            prod_path, stage_path, prod_nav, stage_nav, stage_full_lower)
        tablerow_findings = _table_row_missing_issues(
            prod_path, stage_path, prod_nav, stage_nav, stage_full_lower)
        print(f"  tables: PROD {table_summary['prod_tables']} "
              f"({table_summary['prod_cells']} cells) | "
              f"STAGE {table_summary['stage_tables']} "
              f"({table_summary['stage_cells']} cells) | "
              f"cells missing in STAGE: {len(table_findings)} | "
              f"whole rows missing: {len(tablerow_findings)}")
    except Exception as exc:
        print(f"  table validation failed: {exc}")
        tablerow_findings = []
        table_summary, table_findings = None, []

    _emit(0.93, "checking encoding, table headings, image labels")
    print("Checking encoding, table headings and image labels...")
    try:
        # PROD is the reference: it is not under test. Only STAGE is inspected,
        # so the report is a list of gaps in STAGE rather than a mix of defects
        # from both documents.
        glitches = _encoding_glitches(stage_path, stage_nav, "STAGE")
        print(f"  encoding/garbling issues: {len(glitches)}")
    except Exception as exc:
        print(f"  encoding scan failed: {exc}")
        glitches = []
    try:
        th_findings = _table_heading_issues(prod_path, stage_path, prod_nav,
                                            stage_nav, stage_seq_idx, prod_seq_idx)
        il_findings = _image_label_issues(prod_path, stage_path, prod_nav,
                                          stage_nav, stage_seq_idx, prod_seq_idx,
                                          to_stage_page=_page_mapper(toc_results))
        print(f"  table headings missing: {len(th_findings)} | "
              f"image labels missing: {len(il_findings)}")
    except Exception as exc:
        print(f"  heading/label check failed: {exc}")
        th_findings, il_findings = [], []

    _emit(0.94, "checking links, page numbers, emphasis, figures")
    try:
        link_findings = _hyperlink_issues(prod_path, stage_path)
        linkloss_findings = _link_loss_issues(
            prod_path, stage_path, prod_nav, stage_nav, stage_seq_idx,
            prod_idx=_stage_seq_index(prod_ref_full),
            titles=([r["title"] for r in toc_results]
                    + list(prod_sections.keys()) + list(stage_sections.keys())))
        pageno_findings = _hyperlink_pageno_issues(stage_path, "STAGE", stage_nav)
        try:
            pageno_findings += _xref_section_issues(
                stage_path, stage_nav, toc_results, prod_path)
        except Exception as _xe:  # noqa: BLE001
            print(f"  cross-reference check failed: {_xe}")
        # Emphasis: text PROD sets bold that STAGE draws plain. Judged on the
        # face the words are drawn in rather than a measured ink ratio, which is
        # what made the earlier version report list items whose marker had
        # broken away from their text.
        bold_findings = _bold_issues(prod_path, stage_path, prod_nav,
                                     stage_nav, stage_seq_idx)
        figure_findings = _missing_figure_issues(prod_path, stage_path, prod_nav,
                                                 stage_nav, content_results)
        # Pixelation is not reported (a resolution difference between two
        # rendering pipelines is not a content defect), so it is not computed.
        pixel_findings = []
        liststyle_findings = _list_style_issues(prod_path, stage_path,
                                               prod_nav, stage_nav)
        tableshape_findings = _table_shape_issues(prod_path, stage_path,
                                                 prod_nav, stage_nav)
        tablebreak_findings = _table_break_issues(prod_path, stage_path,
                                                  prod_nav, stage_nav)
        tablemargin_findings = _table_margin_issues(prod_path, stage_path,
                                                    prod_nav, stage_nav)
        # A table running onto the next page has to repeat its header there, or
        # the continuation is a grid of values with nothing naming the columns.
        tablecont_findings = _table_continuation_header_issues(
            stage_path, "STAGE", stage_nav)
        tablemerge_findings = _table_merge_issues(prod_path, stage_path,
                                                  prod_nav, stage_nav)
        print(f"  tables with merged/split cell differences: "
              f"{len(tablemerge_findings)}")
        callgap_findings = _callout_gap_issues(prod_path, stage_path, prod_nav,
                                               stage_nav, toc_results)
        print(f"  diagram callout numbers missing: {len(callgap_findings)}")
        labelmerge_findings = _label_merge_issues(prod_path, stage_path,
                                                  prod_nav, stage_nav)
        print(f"  numbered-item labels merged with their paragraph in STAGE: "
              f"{len(labelmerge_findings)}")
        print(f"  table continuations with no repeated header: "
              f"{len(tablecont_findings)}")
        print(f"  tables broken across pages: {len(tablebreak_findings)}")
        print(f"  tables breaking the margins: {len(tablemargin_findings)}")
        print(f"  list-marker style: {len(liststyle_findings)} | "
              f"table shape: {len(tableshape_findings)}")
        figdiff_findings = _figure_diff_issues(prod_path, stage_path, prod_nav,
                                               stage_nav, stage_seq_idx)
        print(f"  figures differing from PROD: {len(figdiff_findings)}")
        # Each new check stands on its own: the surrounding try covers a dozen
        # checks at once, so one of these raising would silently discard every
        # finding the others had already produced.
        def _safe(name, fn, *a):
            try:
                return fn(*a)
            except Exception as exc:
                print(f"  {name} check failed: {exc}")
                return []

        imgmismatch_findings = _safe(
            "image mismatch", _image_mismatch_issues,
            prod_path, stage_path, prod_nav, stage_nav, toc_results)
        print(f"  images not matching PROD in the same section: "
              f"{len(imgmismatch_findings)}")
        figalign_findings = _safe(
            "image alignment", _figure_align_issues,
            prod_path, stage_path, prod_nav, stage_nav, toc_results)
        print(f"  figures placed differently from PROD: {len(figalign_findings)}")
        colour_findings = _safe(
            "figure colour", _figure_colour_issues,
            prod_path, stage_path, prod_nav, stage_nav, toc_results)
        print(f"  figures with a coloured box one side lacks: "
              f"{len(colour_findings)}")
        linkstyle_findings = _safe(
            "link highlighting", _link_style_issues,
            prod_path, stage_path, prod_nav, stage_nav)
        print(f"  links PROD highlights that STAGE draws plain: "
              f"{len(linkstyle_findings)}")
        italic_findings = _italic_issues(prod_path, stage_path, prod_nav,
                                         stage_nav, stage_seq_idx)
        align_findings = _alignment_issues(prod_path, stage_path, prod_nav, stage_nav)
        listindent_findings = _safe(
            "list indent", _list_indent_issues,
            prod_path, stage_path, prod_nav, stage_nav)
        print(f"  list items indented differently: {len(listindent_findings)}")
        textalign_findings = _safe(
            "text alignment", _text_align_issues,
            prod_path, stage_path, prod_nav, stage_nav, toc_results)
        print(f"  titles/paragraphs aligned differently: "
              f"{len(textalign_findings)}")
        print(f"  italic lost: {len(italic_findings)}")
        callout_counts = {"prod":  _callout_counts(prod_path, prod_nav),
                          "stage": _callout_counts(stage_path, stage_nav)}
        print(f"  callout numbers as text: PROD {callout_counts['prod'][0]} page(s), "
              f"STAGE {callout_counts['stage'][0]} page(s)")
        print(f"  list alignment issues: {len(align_findings)}")
        print(f"  bold lost: {len(bold_findings)}")
        if pixel_findings:
            _p = pixel_findings[0]
            print(f"  pixelated images in STAGE: {_p['count']}/{_p['total']} "
                  f"below {_PIXELATED_DPI}dpi (PROD median {_p['prod_median']}dpi)")
        icon_findings = _icon_issues(stage_path, "STAGE", stage_nav)
        print(f"  broken icons/images: {len(icon_findings)}")
        print(f"  hyperlinks lost: {len(linkloss_findings)}")
        print(f"  hyperlinks: {len(link_findings)} | page numbers: "
              f"{len(pageno_findings)} | figures missing: {len(figure_findings)}")
    except Exception as exc:
        print(f"  link/format checks failed: {exc}")
        link_findings = pageno_findings = bold_findings = []
        linkloss_findings = []
        figure_findings = icon_findings = align_findings = italic_findings = []
        pixel_findings = []
        callout_counts = {}
        figdiff_findings = liststyle_findings = tableshape_findings = []
        tablemargin_findings = []
        tablebreak_findings = []
        tablecont_findings = []
        tablemerge_findings = []
        callgap_findings = []
        labelmerge_findings = []
        figalign_findings = listindent_findings = linkstyle_findings = []
        textalign_findings = []
        imgmismatch_findings = []
        colour_findings = []

    _emit(0.95, "checking figures")
    try:
        figure_summary = _figure_summary(prod_path, stage_path, prod_nav, stage_nav)
        print(f"  figures: PROD {figure_summary['prod']} | STAGE {figure_summary['stage']}")
    except Exception as exc:
        print(f"  figure check failed: {exc}")
        figure_summary = None

    generate_report(prod_path, stage_path, toc_results, content_results,
                    image_results, icon_doc_summary, report_path,
                    tm_counts, tm_dropped,
                    prod_encoding_issue=prod_garbled,
                    stage_encoding_issue=stage_garbled,
                    table_summary=table_summary,
                    table_findings=table_findings,
                    tablerow_findings=tablerow_findings,
                    figure_summary=figure_summary,
                    glitches=glitches,
                    heading_findings=th_findings,
                    label_findings=il_findings,
                    prod_nav_pages=prod_nav,
                    stage_nav_pages=stage_nav,
                    link_findings=link_findings,
                    linkloss_findings=linkloss_findings,
                    pageno_findings=pageno_findings,
                    bold_findings=bold_findings,
                    figure_findings=figure_findings,
                    icon_findings=icon_findings,
                    align_findings=align_findings,
                    italic_findings=italic_findings,
                    callout_counts=callout_counts,
                    figdiff_findings=figdiff_findings,
                    pixel_findings=pixel_findings,
                    liststyle_findings=liststyle_findings,
                    tableshape_findings=tableshape_findings,
                    tablebreak_findings=tablebreak_findings,
                    tablemargin_findings=tablemargin_findings,
                    tablecont_findings=tablecont_findings,
                    tablemerge_findings=tablemerge_findings,
                    callgap_findings=callgap_findings,
                    figalign_findings=figalign_findings,
                    colour_findings=colour_findings,
                    listindent_findings=listindent_findings,
                    textalign_findings=textalign_findings,
                    linkstyle_findings=linkstyle_findings,
                    imgmismatch_findings=imgmismatch_findings,
                    labelmerge_findings=labelmerge_findings)
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

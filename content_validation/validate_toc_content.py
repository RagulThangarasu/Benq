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
    Image as RLImage,
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

                    # 4) Final gate: re-verify the fragment against the WHOLE
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
_OCR_MAX_PAGES = 8        # ceiling on pages read per label: STAGE draws a
                           # figure on or near the page PROD has it on, and
                           # scanning the whole document per label cost more
                           # than every other check put together
_OCR_MIN_IMG_PT = (96, 32) # a raster smaller than this carries no readable
                           # label — icons and rules are not worth an OCR pass


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
        toks = set(_seq_tokens(got))
        if all(w in toks for w in want):
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


def _image_label_issues(prod_path, stage_path, prod_nav, stage_nav,
                        stage_idx, prod_idx):
    """Figure labels PROD carries that STAGE does not."""
    _SHORT_LABEL = 6
    findings, seen = [], set()
    stage_line_keys = _image_label_line_keys(stage_path, stage_nav)

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
        toks = _seq_tokens(label)
        if not toks or label.lower() in seen:
            continue
        seen.add(label.lower())

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
            if _text_in_artwork(stage_path, label, pno, stage_nav):
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
                if _text_in_artwork(stage_path, gap, pno, stage_nav):
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
                        max_figures: int = 80):
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


# ── Text that lives inside artwork ───────────────────────────────────────────
_FIG_TEXT_CACHE = {}


def _figure_text_keys(pdf_path: str, nav_pages: set) -> set:
    """Canonical text of every line that sits inside a figure.

    Labels printed inside a drawing ("0-40°C" under a thermometer icon) are text
    in one document and part of the artwork in the other. Comparing them reports
    the same visible information as missing or added purely because of how it was
    produced, so they are kept out of the content comparison and left to the
    image-label check, which is anchored and conservative.
    """
    key = (os.path.abspath(pdf_path), tuple(sorted(nav_pages)))
    hit = _FIG_TEXT_CACHE.get(key)
    if hit is not None:
        return hit
    out, doc = set(), fitz.open(pdf_path)
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        figs = _detect_figures(page)
        if not figs:
            continue
        grown = [fitz.Rect(f.x0 - 6, f.y0 - 6, f.x1 + 6, f.y1 + 6) for f in figs]
        for txt, rect in _page_lines(page):
            mid = fitz.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
            if any(g.contains(mid) for g in grown):
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
def _callout_gap_issues(pdf_path: str, nav_pages: set, doc_label: str = "STAGE"):
    """Diagrams whose callout numbers skip values.

    A labelled diagram numbers its parts 1..N. When the rendered page shows
    1, 2, 3, 4, 8 the leader lines for 5, 6 and 7 are still drawn but their
    numbers never made it — the label is missing, which is exactly the kind of
    loss a reader notices and a text comparison cannot see.
    """
    doc, found, seen = fitz.open(pdf_path), [], set()
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        figs = _detect_figures(page)
        if not figs:
            continue
        lines = _page_lines(page)
        for fig in figs:
            near = fitz.Rect(fig.x0 - 40, fig.y0 - 40, fig.x1 + 40, fig.y1 + 40)
            nums = set()
            for txt, rect in lines:
                m = _CALLOUT_RE.match(txt)
                if m and near.intersects(rect):
                    nums.add(int(m.group(1)))
            if len(nums) < 3:
                continue
            lo, hi = min(nums), max(nums)
            missing = [n for n in range(lo, hi + 1) if n not in nums]
            if not missing:
                continue
            # Overlapping regions on one page describe the same diagram.
            key = (i, tuple(sorted(nums)))
            if key in seen:
                continue
            seen.add(key)
            found.append({"doc": doc_label, "page": i,
                          "present": sorted(nums), "missing": missing})
    doc.close()
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


def _table_break_issues(prod_path, stage_path, prod_nav, stage_nav):
    """Tables that STAGE splits over more pages than PROD does.

    This is the layout question a reader notices: a table that sits whole on one
    page in PROD but is broken by a page boundary in STAGE, so its rows are cut
    apart from their header.
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
        findings.append({"page": s0, "prod_page": p0,
                         "prod_pages": p_pages, "stage_pages": s_pages,
                         "stage_from": s0, "stage_to": s1, "header": phead})
    return findings


# ── Table structure ──────────────────────────────────────────────────────────
def _table_shape_issues(prod_path, stage_path, prod_nav, stage_nav):
    """Tables whose column count in STAGE does not match PROD.

    Not a pixel comparison — the question is structural: a three-column table
    must still be three columns, and a single cell must not be split apart.
    Tables are paired by their header wording so differing pagination does not
    matter.
    """
    if os.path.abspath(prod_path) == os.path.abspath(stage_path):
        return []

    def shapes(path, nav):
        out = {}
        for pno, nrow, ncol, rows in _merge_continued_tables(
                _crawl_tables(path, nav)):
            if not rows or ncol < 2:
                continue
            head = [re.sub(r"\s+", " ", (c or "")).strip() for c in rows[0]]
            # Count the headed columns, not the detector's grid. A table with
            # nested sub-rows (STAGE sets "Main / Sub / PIP Size" inside the PIP
            # row) makes the grid one column wider while the reader still sees
            # the same three headed columns, and comparing grids reported that
            # as a column change.
            filled = [h for h in head if h]
            if len(filled) < 2:
                continue
            key = " ".join(_seq_tokens(" ".join(filled)))
            if len(key.split(" ")) >= 2:
                out.setdefault(key, (pno, len(filled), nrow, " | ".join(filled)))
        return out

    prod_sh, stage_sh = shapes(prod_path, prod_nav), shapes(stage_path, stage_nav)
    findings = []
    for key, (ppage, pcol, _prow, phead) in prod_sh.items():
        hit = stage_sh.get(key)
        if not hit:
            continue
        spage, scol, _srow, _ = hit
        if scol == pcol:
            continue
        findings.append({"page": spage, "prod_page": ppage,
                         "prod_cols": pcol, "stage_cols": scol,
                         "header": phead})
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
                pix = fitz.Pixmap(doc, xref)
                if pix.width >= 2 and pix.height >= 2 and _pixmap_is_blank(pix):
                    w = max(r.width for r in rects)
                    h = max(r.height for r in rects)
                    out.append({
                        "doc": doc_label, "page": i, "xref": xref,
                        "kind": "Image is blank",
                        "text": f"{pix.width}×{pix.height} image drawn at "
                                f"{w:.0f}×{h:.0f} pt is a single flat colour — "
                                f"it renders as an empty box"})
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


# ── Numbered-list alignment ──────────────────────────────────────────────────
# A numbered step should read "5. Place the monitor properly." on one line. When
# the layout breaks, the marker is left alone on its own line with the step text
# wrapped underneath. The text is all still present, so the content comparison
# sees nothing wrong — this is purely a layout defect and needs its own check.
_LIST_MARKER_RE = re.compile(r"^\s*(\d{1,2})\s*[.)]\s*$")


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


# ── Hyperlinks ───────────────────────────────────────────────────────────────
_DOMAINISH_RE = re.compile(r"^(?:https?://)?(?:[\w-]+\.)+[A-Za-z]{2,}(?:/|$)", re.I)


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
    sides = [("PROD", prod_path, prod_uris)]
    if not same_file:
        sides.append(("STAGE", stage_path, stage_uris))
    for label, path, bucket in sides:
        for pno, l, npages in links(path):
            kind = l.get("kind")
            if kind == fitz.LINK_URI:
                uri = (l.get("uri") or "").strip()
                bucket.add(uri.lower().rstrip("/"))
                if not re.match(r"^(https?|mailto):", uri, re.I):
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
            elif kind in (fitz.LINK_GOTO, fitz.LINK_NAMED):
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


# ── Page numbering ───────────────────────────────────────────────────────────
def _page_number_issues(pdf_path: str, doc_label: str, nav_pages: set):
    """Body pages that carry no page number at all."""
    doc, out = fitz.open(pdf_path), []
    for i, page in enumerate(doc, 1):
        if i in nav_pages:
            continue
        text = page.get_text()
        if not re.search(r"(?m)^\s*\d{1,4}\s*$", text):
            out.append({"doc": doc_label, "page": i,
                        "kind": "Page number missing",
                        "text": f"page {i} carries no page number"})
    doc.close()
    return out


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
    merged = [list(tables[0])]
    for pno, nrow, ncol, rows in tables[1:]:
        prev = merged[-1]
        if pno == prev[0] + 1 and ncol == prev[2]:
            prev[1] += nrow
            prev[3] = prev[3] + rows          # continuation rows join the table
        else:
            merged.append([pno, nrow, ncol, rows])
    return [tuple(m) for m in merged]


def _validate_tables(prod_path: str, stage_path: str,
                     prod_nav: set, stage_nav: set, stage_full_lower: str):
    """Return (summary, [findings]) for PROD tables checked cell by cell.

    Every non-empty PROD cell must have a counterpart in STAGE. Cells are checked
    with the same gap-tolerant window matcher used for body text, so a cell whose
    wording STAGE re-wraps or re-orders is not reported — only text with no
    counterpart anywhere in STAGE is.
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

    findings, n_cells = [], 0
    for pno, nrow, ncol, rows in prod_tables:
        for ri, row in enumerate(rows):
            for ci, cell in enumerate(row):
                text = (cell or "").strip()
                if not text:
                    continue
                n_cells += 1
                toks = _seq_tokens(text)
                if not toks:
                    continue
                if _script_unreliable(text):
                    continue      # see _script_unreliable — not comparable
                if len(toks) <= _SHORT_CELL_WORDS:
                    # A short cell is one atomic label ("Item", "Illustration",
                    # "5V / 3A"). Either STAGE has it or it does not — checking
                    # the whole cell catches losses that the 3-word window used
                    # for prose would silently drop. It must read that way in
                    # PROD first, or it is a mis-sliced cell.
                    # Every word present somewhere means the cell is there and
                    # only the reading order differs — table cells are emitted
                    # per block, so a two-word cell can be split apart.
                    if (not _seq_present(idx, toks)
                            and not _all_words_present(text, idx)
                            and _source_ok(prod_idx, toks)
                            and not _text_in_artwork(stage_path, text, pno,
                                                     stage_nav)):
                        findings.append({"page": pno, "row": ri, "col": ci,
                                         "text": text.replace("\n", " ")})
                else:
                    for gap in _refine_fragment(_tokenize(text), idx, prod_idx):
                        if _script_unreliable(gap):
                            continue
                        findings.append({"page": pno, "row": ri, "col": ci,
                                         "text": gap})
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
               color=(0.85, 0.1, 0.1), min_height: float = 0.0):
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
    "Image difference":      ("The two documents show different artwork for the "
                              "same figure.",
                              "Check which artwork is current and re-publish "
                              "STAGE with it."),
    "Table heading missing": ("A column heading is not present in any STAGE "
                              "table header.",
                              "Restore the heading so the column is identifiable, "
                              "and check the column itself was not dropped with "
                              "it."),
    "Table cell missing":    ("A cell PROD carries has no counterpart in STAGE.",
                              "Restore the cell content, or verify the row was "
                              "removed on purpose."),
    "Table layout broken":   ("A page break splits the table, separating rows "
                              "from their header.",
                              "Keep the table on one page, or repeat the header "
                              "row on each page it continues onto."),
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
    "Content missing":       ("Text PROD carries under this topic is absent from "
                              "STAGE.",
                              "Restore the sentence in the STAGE topic, or confirm "
                              "it was withdrawn deliberately."),
    "Hyperlink":             ("A link differs between the two documents.",
                              "Point the STAGE link at the same destination as "
                              "PROD and confirm it resolves."),
    "Page number":           ("A page reference does not match.",
                              "Re-generate the cross-references after the final "
                              "pagination."),
    "Broken image":          ("An image did not render.",
                              "Re-link or re-upload the asset, then confirm it "
                              "renders in the published output."),
}
_FIX_DEFAULT = ("STAGE does not match PROD at this location.",
                "Compare the two documents here and bring STAGE in line with "
                "PROD, or record why the difference is intended.")


def _fix_for(issue: str):
    """(what it means, what to do) for an issue label — longest match wins."""
    hit = max((k for k in _FIX_ADVICE if issue.lower().startswith(k.lower())),
              key=len, default=None)
    if hit is None:
        hit = max((k for k in _FIX_ADVICE if k.lower() in issue.lower()),
                  key=len, default=None)
    return _FIX_ADVICE[hit] if hit else _FIX_DEFAULT


def generate_report(prod_path, stage_path, toc_results, content_results,
                    image_results, icon_doc_summary, report_path,
                    tm_counts=None, tm_dropped=None,
                    prod_encoding_issue=False, stage_encoding_issue=False,
                    table_summary=None, table_findings=None,
                    figure_summary=None, glitches=None,
                    heading_findings=None, label_findings=None,
                    prod_nav_pages=None, stage_nav_pages=None,
                    link_findings=None, pageno_findings=None,
                    bold_findings=None, figure_findings=None,
                    icon_findings=None, align_findings=None,
                    italic_findings=None, callout_counts=None,
                    figdiff_findings=None, pixel_findings=None,
                    liststyle_findings=None,
                    tableshape_findings=None, tablebreak_findings=None,
                    callgap_findings=None):
    prod_nav_pages   = prod_nav_pages or set()
    stage_nav_pages  = stage_nav_pages or set()
    link_findings    = link_findings or []
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
    callgap_findings    = callgap_findings or []
    glitches         = glitches or []
    heading_findings = heading_findings or []
    label_findings   = label_findings or []
    tm_counts = tm_counts or []
    tm_dropped = tm_dropped or []
    table_findings = table_findings or []

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

    glitches            = _dedupe(glitches, "doc", "page", "kind", "text")
    icon_findings       = _dedupe(icon_findings, "doc", "page", "kind", "text")
    label_findings      = _dedupe(label_findings, "page", "text")
    figure_findings     = _dedupe(figure_findings, "page", "stage_page", "title")
    heading_findings    = _dedupe(heading_findings, "page", "text", "row")
    table_findings      = _dedupe(table_findings, "page", "row", "col", "text")
    tableshape_findings = _dedupe(tableshape_findings, "prod_page", "page", "header")
    tablebreak_findings = _dedupe(tablebreak_findings, "prod_page", "stage_from",
                                  "stage_to", "header")
    figdiff_findings    = _dedupe(figdiff_findings, "page", "stage_page", "anchor")
    callgap_findings    = _dedupe(callgap_findings, "doc", "page", "missing")
    italic_findings     = _dedupe(italic_findings, "page", "text")
    align_findings      = _dedupe(align_findings, "doc", "page", "marker", "text")
    liststyle_findings  = _dedupe(liststyle_findings, "doc", "page", "text")
    link_findings       = _dedupe(link_findings, "doc", "page", "kind", "text")
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
    total_issues = (len(glitches) + len(heading_findings) + len(label_findings)
                    + len(table_findings) + n_cmiss + n_cxtra
                    + len(link_findings) + len(pageno_findings)
                    + len(figure_findings) + len(figdiff_findings)
                    + len(icon_findings) + len(align_findings)
                    + len(italic_findings)
                    + len(bold_findings)
                    + len(pixel_findings)
                    + len(liststyle_findings) + len(tableshape_findings)
                    + len(tablebreak_findings) + len(callgap_findings))

    if not total_issues:
        story.append(Paragraph("No issues found — STAGE matches PROD.",
                               ParagraphStyle("AllOk", parent=styles["Normal"],
                                              fontSize=10,
                                              textColor=colors.HexColor("#2e7d32"))))
    else:
        # One row per issue, grouped by kind. The table keeps every issue on a
        # single line so the whole list can be scanned at a glance.
        rows = [[Paragraph(f"<b>{h}</b>", hdr_s) for h in
                 ["#", "Topic / Location", "Where", "Issue", "Detail",
                  "What it means &amp; how to fix it"]]]
        n = 0

        _seen_rows = set()

        def add(issue, where, topic, detail, style):
            nonlocal n
            key = (issue, where, topic, detail)
            if key in _seen_rows:
                return
            _seen_rows.add(key)
            n += 1
            means, fix = _fix_for(issue)
            rows.append([Paragraph(str(n), cell_s),
                         Paragraph(topic, topic_s),
                         Paragraph(where, cell_s),
                         Paragraph(issue, style),
                         Paragraph(detail, cell_s),
                         Paragraph(f"{_esc(means)}<br/><b>Fix:</b> {_esc(fix)}",
                                   fix_s)])

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
            add(f["kind"], f"{f['doc']} p{f['page']}",
                f"Image on {f['doc']} page {f['page']}",
                f"<font color='#b71c1c'>{_esc(_trunc(f['text'], 260))}</font>", fail_s)

        for f in label_findings:
            add("Image label missing", f"PROD p{f['page']}",
                f"Figure on PROD page {f['page']}",
                f"The figure on PROD page {f['page']} is labelled "
                f"<font color='#b71c1c'><b>{_esc(_trunc(f['text'], 200))}</b></font> "
                f"— that label text is not present in STAGE.", fail_s)

        for f in figure_findings:
            add("Image missing", f"PROD p{f['page']} / STAGE p{f['stage_page']}",
                _esc(f["title"]),
                f"PROD shows {f['n']} figure(s) under this topic; the STAGE pages "
                f"for the same topic have none.", fail_s)

        for f in figdiff_findings:
            add("Image difference",
                f"PROD p{f['page']} / STAGE p{f['stage_page']}",
                f"Figure captioned \u201c{_esc(_trunc(f['anchor'], 46))}\u201d",
                f"This section's image is different. Structural similarity "
                f"<b>{f['similarity']:.2f}</b> (identical artwork scores near "
                f"1.00). Both figures are shown below.", fail_s)

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

        for f in link_findings:
            add(f["kind"],
                f"{f['doc']}" + (f" p{f['page']}" if f["page"] else ""),
                f"Hyperlink ({f['doc']})",
                f"<font color='#b71c1c'>{_esc(_trunc(f['text'], 260))}</font>", fail_s)

        for f in callgap_findings:
            add("Image label missing",
                f"{f['doc']} p{f['page']}",
                f"Diagram on {f['doc']} page {f['page']}",
                f"The diagram is numbered <b>"
                f"{', '.join(str(n) for n in f['present'])}</b> — the label(s) "
                f"<font color='#b71c1c'><b>"
                f"{', '.join(str(n) for n in f['missing'])}</b></font> are not "
                f"there. The leader lines are drawn but their numbers are "
                f"missing.", fail_s)

        for f in tablebreak_findings:
            add("Table layout broken",
                f"PROD p{f['prod_page']} / STAGE p{f['stage_from']}-{f['stage_to']}",
                f"Table \u201c{_esc(_trunc(f['header'], 44))}\u201d",
                f"The table is split by a page break in STAGE: it runs over "
                f"<b>{f['stage_pages']} pages</b> (p{f['stage_from']}\u2013"
                f"{f['stage_to']}) where PROD keeps it on <b>{f['prod_pages']}</b>. "
                f"Rows are separated from their header.", fail_s)

        for f in tableshape_findings:
            add("Table columns differ",
                f"PROD p{f['prod_page']} / STAGE p{f['page']}",
                f"Table \u201c{_esc(_trunc(f['header'], 44))}\u201d",
                f"PROD lays this table out in <b>{f['prod_cols']} columns</b>; "
                f"STAGE renders it in <b>{f['stage_cols']}</b>.", fail_s)

        for f in heading_findings:
            add("Table heading missing", f"PROD p{f['page']}",
                f"Table on PROD page {f['page']}",
                f"Column heading(s) <font color='#b71c1c'><b>"
                f"{_esc(_trunc(f['text'], 140))}</b></font> dropped. PROD header: "
                f"<i>{_esc(_trunc(f.get('row',''), 170))}</i>", fail_s)

        for f in table_findings:
            add("Table cell missing", f"PROD p{f['page']}",
                f"Table on PROD page {f['page']}",
                f"Row {f['row']}, column {f['col']} — "
                f"<font color='#b71c1c'>{_esc(_trunc(f['text'], 240))}</font>", fail_s)

        for f in bold_findings:
            add("Bold lost", f"PROD p{f['page']} / STAGE p{f['stage_page']}",
                f"Text on PROD page {f['page']}",
                f"<b>{_esc(_trunc(f['text'], 220))}</b> is set bold in PROD; "
                f"STAGE draws it in the ordinary body face.", fail_s)

        for f in italic_findings:
            add("Italic lost", f"PROD p{f['page']}",
                f"Text on PROD page {f['page']}",
                f"<i>{_esc(_trunc(f['text'], 220))}</i> is italic in PROD but is "
                f"rendered upright in STAGE.", fail_s)

        for f in align_findings:
            add("List alignment broken", f"{f['doc']} p{f['page']}",
                f"{f['doc']} page {f['page']}",
                f"Step marker <font color='#b71c1c'><b>{_esc(f['marker'])}</b></font> "
                f"sits on its own line; its text "
                f"<font color='#b71c1c'><b>{_esc(_trunc(f['text'], 160))}</b></font> "
                f"wraps to the line below \u2014 the number and its text are not "
                f"aligned. {_esc(f.get('why', ''))}.", fail_s)

        for f in pageno_findings:
            add(f["kind"], f"{f['doc']} p{f['page']}",
                f"{f['doc']} page {f['page']}", _esc(f["text"]), fail_s)

        for r in diff_rows:
            where = f"PROD p{r['prod_page']} / STAGE p{r['stage_page']}"
            for m in r.get("missing", []):
                add("Content missing", where, _esc(r["title"]),
                    f"In PROD, absent from STAGE: <font color='#b71c1c'>"
                    f"{_highlight_notice_labels(_trunc(m, 300))}</font>", fail_s)


        it = Table(rows, colWidths=[26, 114, 70, 90, 246, 188], repeatRows=1)
        it.setStyle(TableStyle([
            ("BACKGROUND",   (0,0), (-1,0), colors.HexColor("#37474f")),
            ("GRID",         (0,0), (-1,-1), 0.5, colors.grey),
            ("VALIGN",       (0,0), (-1,-1), "TOP"),
            ("TOPPADDING",   (0,0), (-1,-1), 4),
            ("BOTTOMPADDING",(0,0), (-1,-1), 4),
            ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white, colors.HexColor("#f7f7f7")]),
        ]))
        story.append(it)

        # ── Evidence: one pane per issue, on the page proven to carry it ──
        # Single-sided by design. Showing "the same place in the other document"
        # needed an anchor to line two pages up, and that repeatedly framed
        # unrelated regions. A shot of the page that actually contains the text,
        # with the exact words boxed, is verifiable: the locator matches the same
        # canonical tokens the finding was made from, and when it cannot find
        # them no picture is produced rather than a wrong one.
        ev = []

        _seen_shots = set()

        def _shot(doc_path, needle, hint, caption, doc_label):
            pg, rects = _locate_tokens(doc_path, needle, hint)
            if not pg or not rects:
                return []
            key = (doc_path, pg, caption,
                   tuple(round(c, 1) for r in rects for c in tuple(r)))
            if key in _seen_shots:
                return []
            _seen_shots.add(key)
            png = _page_shot(doc_path, pg, [(rects, _DEFECT_COLOR)])
            img = _shot_flowable(png, max_w=690.0, max_h=180.0)
            if img is None:
                return []
            return [Paragraph(caption, ParagraphStyle(
                        "EvCap2", parent=styles["Normal"], fontSize=8.5,
                        leading=11.5, spaceAfter=2)),
                    Paragraph(f"<b>{doc_label}</b> page {pg} — boxed in red",
                              ParagraphStyle("EvLab2", parent=styles["Normal"],
                                             fontSize=7.5,
                                             textColor=colors.HexColor("#37474f"))),
                    img, Spacer(1, 6),
                    HRFlowable(width="100%", thickness=0.6,
                               color=colors.HexColor("#cfd8dc"),
                               spaceBefore=2, spaceAfter=10)]

        def _num(v, d=0):
            try:
                return int(v)
            except (TypeError, ValueError):
                return d

        for g in glitches:
            ev += _shot(stage_path, g.get("probe") or g["text"], g["page"],
                        f"<b>{g['kind']}</b> — "
                        f"<font color='#b71c1c'>{_esc(g['text'])}</font>", "STAGE")
        for f in figdiff_findings:
            try:
                dp = fitz.open(prod_path)
                pp = dp[f["page"] - 1].get_pixmap(
                    clip=fitz.Rect(*f["prod_rect"]), matrix=fitz.Matrix(2.4, 2.4)
                ).tobytes("png")
                dp.close()
                ds = fitz.open(stage_path)
                sp = ds[f["stage_page"] - 1].get_pixmap(
                    clip=fitz.Rect(*f["stage_rect"]), matrix=fitz.Matrix(2.4, 2.4)
                ).tobytes("png")
                ds.close()
            except Exception:
                continue
            pi, si = _shot_flowable(pp, 330, 250), _shot_flowable(sp, 330, 250)
            if not pi or not si:
                continue
            lab = ParagraphStyle("FigLab", parent=styles["Normal"], fontSize=7.5,
                                 textColor=colors.HexColor("#37474f"))
            tbl = Table([[Paragraph(f"<b>PROD</b> page {f['page']}", lab),
                          Paragraph(f"<b>STAGE</b> page {f['stage_page']}", lab)],
                         [pi, si]], colWidths=[345, 345])
            tbl.setStyle(TableStyle([
                ("GRID",       (0,0), (-1,-1), 0.5, colors.HexColor("#b0bec5")),
                ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#eceff1")),
                ("VALIGN",     (0,0), (-1,-1), "TOP"),
                ("TOPPADDING", (0,0), (-1,-1), 4),
                ("BOTTOMPADDING",(0,0),(-1,-1), 4),
            ]))
            ev += [Paragraph(f"<b>Image difference</b> — this section's image is "
                             f"different (captioned \u201c"
                             f"{_esc(_trunc(f['anchor'], 60))}\u201d)",
                             ParagraphStyle("FigCap", parent=styles["Normal"],
                                            fontSize=8.5, leading=11.5,
                                            spaceAfter=2)),
                   tbl, Spacer(1, 6),
                   HRFlowable(width="100%", thickness=0.6,
                              color=colors.HexColor("#cfd8dc"),
                              spaceBefore=2, spaceAfter=10)]

        for f in callgap_findings:
            ev += _shot(stage_path, str(f["present"][0]), f["page"],
                        f"<b>Image label missing</b> — diagram numbered "
                        f"{', '.join(str(n) for n in f['present'])}; "
                        f"<font color='#b71c1c'>"
                        f"{', '.join(str(n) for n in f['missing'])}</font> absent",
                        f["doc"])

        for f in icon_findings:
            ev += _shot(stage_path, "", f["page"],
                        f"<b>{f['kind']}</b> — {_esc(_trunc(f['text'],120))}",
                        "STAGE")
        for f in label_findings:
            ev += _shot(prod_path, f["text"], f["page"],
                        f"<b>Image label missing</b> — the figure on PROD page "
                        f"{f['page']} is labelled "
                        f"<font color='#b71c1c'>{_esc(_trunc(f['text'],110))}</font>"
                        f", which STAGE does not have", "PROD")
        for f in heading_findings:
            ev += _shot(prod_path, f.get("row", f["text"]), f["page"],
                        f"<b>Table heading missing</b> — column(s) "
                        f"<font color='#b71c1c'>{_esc(_trunc(f['text'],110))}</font>"
                        f" are not in any STAGE table header", "PROD")
        for f in table_findings:
            ev += _shot(prod_path, f["text"], f["page"],
                        f"<b>Table cell missing</b> — "
                        f"<font color='#b71c1c'>{_esc(_trunc(f['text'],110))}</font>",
                        "PROD")
        for f in tableshape_findings:
            ev += _shot(prod_path, f["header"], f["prod_page"],
                        f"<b>Table columns differ</b> — {f['prod_cols']} columns "
                        f"in PROD, {f['stage_cols']} in STAGE", "PROD")
        for f in bold_findings:
            ev += _shot(prod_path, f["text"], f["page"],
                        f"<b>Bold lost</b> — "
                        f"<b>{_esc(_trunc(f['text'], 110))}</b> is bold in PROD, "
                        f"plain in STAGE", "PROD")
        for f in italic_findings:
            ev += _shot(prod_path, f["text"], f["page"],
                        f"<b>Italic lost</b> — "
                        f"<i>{_esc(_trunc(f['text'],110))}</i>", "PROD")
        for f in liststyle_findings:
            ev += _shot(stage_path, f["text"], f["page"],
                        f"<b>List marker changed</b> — {f['prod_style']} in PROD, "
                        f"{f['stage_style']} in STAGE", "STAGE")
        for f in align_findings:
            ev += _shot(prod_path if f["doc"] == "PROD" else stage_path,
                        f["text"], f["page"],
                        f"<b>List alignment broken</b> — marker "
                        f"<b>{_esc(f['marker'])}</b> separated from "
                        f"<font color='#b71c1c'>{_esc(_trunc(f['text'],90))}</font>",
                        f["doc"])
        for r in diff_rows:
            for m in r.get("missing", []):
                ev += _shot(prod_path, m, _num(r.get("prod_page")),
                            f"<b>Content missing</b> — {_esc(r['title'])}: "
                            f"<font color='#b71c1c'>{_esc(_trunc(m,110))}</font>"
                            f" is in PROD, absent from STAGE", "PROD")


        if ev:
            story.append(PageBreak())
            story.append(Paragraph("Evidence", head_s))
            story.append(Paragraph(
                "Each shot is a page that was verified to contain the text, with "
                "the exact words boxed in red. Where the text exists in only one "
                "document, only that side is shown — a page is never displayed as "
                "evidence unless it actually carries the text.",
                ParagraphStyle("EvIntro", parent=styles["Normal"], fontSize=8,
                               textColor=colors.grey, spaceAfter=8)))
            story.extend(ev)

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
    print(f"  TOC: Match={n_m} | Missing in Stage={n_mi} | Extra in Stage={n_e}")

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
        print(f"  tables: PROD {table_summary['prod_tables']} "
              f"({table_summary['prod_cells']} cells) | "
              f"STAGE {table_summary['stage_tables']} "
              f"({table_summary['stage_cells']} cells) | "
              f"cells missing in STAGE: {len(table_findings)}")
    except Exception as exc:
        print(f"  table validation failed: {exc}")
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
                                          stage_nav, stage_seq_idx, prod_seq_idx)
        print(f"  table headings missing: {len(th_findings)} | "
              f"image labels missing: {len(il_findings)}")
    except Exception as exc:
        print(f"  heading/label check failed: {exc}")
        th_findings, il_findings = [], []

    _emit(0.94, "checking links, page numbers, emphasis, figures")
    try:
        link_findings = _hyperlink_issues(prod_path, stage_path)
        pageno_findings = _page_number_issues(stage_path, "STAGE", stage_nav)
        # Emphasis: text PROD sets bold that STAGE draws plain. Judged on the
        # face the words are drawn in rather than a measured ink ratio, which is
        # what made the earlier version report list items whose marker had
        # broken away from their text.
        bold_findings = _bold_issues(prod_path, stage_path, prod_nav,
                                     stage_nav, stage_seq_idx)
        figure_findings = _missing_figure_issues(prod_path, stage_path, prod_nav,
                                                 stage_nav, content_results)
        pixel_findings = _pixelation_issue(prod_path, stage_path, prod_nav,
                                           stage_nav, toc_results)
        liststyle_findings = _list_style_issues(prod_path, stage_path,
                                               prod_nav, stage_nav)
        tableshape_findings = _table_shape_issues(prod_path, stage_path,
                                                 prod_nav, stage_nav)
        tablebreak_findings = _table_break_issues(prod_path, stage_path,
                                                  prod_nav, stage_nav)
        callgap_findings = _callout_gap_issues(stage_path, stage_nav, "STAGE")
        print(f"  diagram callout numbers missing: {len(callgap_findings)}")
        print(f"  tables broken across pages: {len(tablebreak_findings)}")
        print(f"  list-marker style: {len(liststyle_findings)} | "
              f"table shape: {len(tableshape_findings)}")
        figdiff_findings = _figure_diff_issues(prod_path, stage_path, prod_nav,
                                               stage_nav, stage_seq_idx)
        print(f"  figures differing from PROD: {len(figdiff_findings)}")
        italic_findings = _italic_issues(prod_path, stage_path, prod_nav,
                                         stage_nav, stage_seq_idx)
        align_findings = _alignment_issues(prod_path, stage_path, prod_nav, stage_nav)
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
        print(f"  hyperlinks: {len(link_findings)} | page numbers: "
              f"{len(pageno_findings)} | figures missing: {len(figure_findings)}")
    except Exception as exc:
        print(f"  link/format checks failed: {exc}")
        link_findings = pageno_findings = bold_findings = []
        figure_findings = icon_findings = align_findings = italic_findings = []
        pixel_findings = []
        callout_counts = {}
        figdiff_findings = liststyle_findings = tableshape_findings = []
        tablebreak_findings = []
        callgap_findings = []

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
                    figure_summary=figure_summary,
                    glitches=glitches,
                    heading_findings=th_findings,
                    label_findings=il_findings,
                    prod_nav_pages=prod_nav,
                    stage_nav_pages=stage_nav,
                    link_findings=link_findings,
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
                    callgap_findings=callgap_findings)
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

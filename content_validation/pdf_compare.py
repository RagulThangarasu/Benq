"""PDF-to-PDF comparison: every difference located, boxed and reported twice.

This is a whole-document comparator rather than a lane of checks. It answers one
question for each pair of manuals - "what reads differently?" - and answers it in
two passes that do not share a blind spot:

    TEXT PASS  reads the embedded text layer of both files.
    IMAGE PASS renders each artwork region and reads it with OCR.

The image pass is what makes this different from a text diff. A print-authored
manual draws its figure callouts as live text; a web-generated one ships the same
figure as a flattened bitmap. Compared on the text layer alone, every label in
the document looks deleted, and a label that really was dropped from the artwork
looks like all the others. Reading the pixels separates the two.

Every difference carries the rectangles it occupies on each side, so the report
can show the reader the actual page with the difference boxed - and both reports,
HTML and PDF, are written from that same list in one run.
"""
from __future__ import annotations

import base64
import html
import io
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from dataclasses import dataclass, field

import fitz

# ── tuning ───────────────────────────────────────────────────────────────────
OCR_DPI       = 400     # figure regions are rendered this large before OCR
SHOT_DPI      = 150     # evidence crops in the reports
SHOT_PAD      = 26      # points of context around a boxed difference
FIG_MIN_PT    = 46      # smallest side of a region worth calling artwork
PAGE_SIM_MIN  = 0.20    # below this, a page has no counterpart at all
LABEL_MIN_LEN = 3       # OCR fragments shorter than this are noise
BOX_RGB       = (0.83, 0.11, 0.09)

_HAS_OCR = shutil.which("tesseract") is not None

# Unicode block -> the tesseract traineddata that reads it. Only the languages
# actually installed are ever requested; asking for one that is missing makes
# tesseract exit and the page read as empty, which turns into a false "label
# missing" for every label on it.
_SCRIPT_LANGS = [
    ((0x0400, 0x04FF), ("rus", "ukr", "bul")),      # Cyrillic
    ((0x0370, 0x03FF), ("ell",)),                   # Greek
    ((0x0590, 0x05FF), ("heb",)),                   # Hebrew
    ((0x0600, 0x06FF), ("ara",)),                   # Arabic
    ((0x0E00, 0x0E7F), ("tha",)),                   # Thai
    ((0x3040, 0x30FF), ("jpn",)),                   # Japanese kana
    ((0xAC00, 0xD7AF), ("kor",)),                   # Hangul
    ((0x4E00, 0x9FFF), ("chi_tra", "chi_sim", "jpn", "kor")),
    ((0x0100, 0x024F), ("deu", "fra", "spa", "ita", "por", "pol",
                        "ces", "hun", "ron", "nld", "swe", "tur")),
]


def _installed_langs() -> set:
    if not _HAS_OCR:
        return set()
    try:
        out = subprocess.run(["tesseract", "--list-langs"],
                             capture_output=True, text=True, timeout=30)
        return {l.strip() for l in (out.stdout or "").splitlines()[1:] if l.strip()}
    except Exception:
        return set()


_LANGS_AVAILABLE = _installed_langs()


def ocr_langs_for(text: str) -> tuple:
    """(tesseract -l value, scripts whose language pack is missing).

    English is always included: part numbers, model names and menu paths stay
    Latin in every localisation.
    """
    wanted, missing = ["eng"], []
    seen = set()
    for ch in text or "":
        o = ord(ch)
        for (lo, hi), langs in _SCRIPT_LANGS:
            if lo <= o <= hi and (lo, hi) not in seen:
                seen.add((lo, hi))
                have = [l for l in langs if l in _LANGS_AVAILABLE]
                if have:
                    wanted.extend(have)
                else:
                    missing.append(langs[0])
                break
    return "+".join(dict.fromkeys(wanted)), tuple(dict.fromkeys(missing))

# Cross-references are renumbered or dropped wholesale by a re-flowed build.
# Removing them before comparing keeps the report about wording, not pagination.
_XREF = re.compile(r"\s*(?:on|see)?\s*pages?(?:\s+\d+(?:\s*[-\u2013]\s*\d+)?|\s*$)", re.I)
_DOTS = re.compile(r"\.{4,}")


# ── normalisation ────────────────────────────────────────────────────────────
# Scripts that write without spaces between words. A run of these is segmented
# per character, because "word" is not a unit the writing system provides.
_SYLLABIC = (
    (0x3040, 0x30FF),    # Hiragana, Katakana
    (0x3400, 0x4DBF),    # CJK Unified Ideographs Extension A
    (0x4E00, 0x9FFF),    # CJK Unified Ideographs
    (0xF900, 0xFAFF),    # CJK Compatibility Ideographs
    (0xAC00, 0xD7AF),    # Hangul syllables
    (0x1100, 0x11FF),    # Hangul Jamo
    (0x0E00, 0x0E7F),    # Thai
)
_WORD_RUN = re.compile(r"\w+", re.UNICODE)
_ONLY_DIGITS = re.compile(r"^\d{1,3}$")


def _syllabic(ch: str) -> bool:
    o = ord(ch)
    return any(lo <= o <= hi for lo, hi in _SYLLABIC)


def flat(text: str) -> str:
    """Letters and digits of any script, lowercased.

    Line-break hyphenation, bullet glyphs, spacing and punctuation all differ
    between a print build and a web build without the wording changing, so none
    of them may reach the comparison. Everything a writing system uses to spell
    with is kept, whatever the script: restricting this to a-z silently emptied
    every Cyrillic, Greek, Hebrew, Arabic and CJK page, and two empty pages
    compare as identical.
    """
    t = unicodedata.normalize("NFKC", text or "").lower()
    t = t.replace("\u2122", "").replace("\u00ae", "")
    t = re.sub(r"-\s*\n", "", t)
    t = _XREF.sub(" ", t)
    return "".join(c for c in t if c.isalnum())


def _dehyphen(text: str) -> str:
    """Rejoin a word a line break split in two ("con-\nnected" -> "connected")."""
    return re.sub(r"-\s*\n\s*", "", text or "")


def words(text: str) -> list:
    """Comparable tokens, in any script.

    Two rules beyond "split on non-letters":

    * hyphens are removed rather than treated as breaks. Where a word falls
      decides how it is hyphenated and the two builds break lines in different
      places - "USB-C" against "USB-\nC", "eco-friendly" against
      "eco-\nfriendly". Splitting on the hyphen makes those different words.

    * Chinese, Japanese, Korean and Thai are segmented per character. They are
      written without spaces, so a whole line arrives as one token and a single
      changed character makes the entire line look replaced. Per character, the
      comparison localises the change the way it does in a spaced script.
    """
    t = unicodedata.normalize("NFKC", _dehyphen(text)).lower()
    t = t.replace("-", "").replace("\u00ad", "").replace("\u2010", "")
    out = []
    for run in _WORD_RUN.findall(t):
        if any(_syllabic(c) for c in run):
            buf = ""
            for ch in run:
                if _syllabic(ch):
                    if buf:
                        out.append(buf)
                        buf = ""
                    out.append(ch)
                else:
                    buf += ch
            if buf:
                out.append(buf)
        else:
            out.append(run)
    return [w for w in out if not _ONLY_DIGITS.match(w)]


def letters(text: str) -> int:
    """How many real letters, of any script, a string carries."""
    return sum(1 for c in (text or "") if c.isalpha())


def _is_nav(page) -> bool:
    """Contents, index and Q&A pages - they restate headings, not content."""
    text = page.get_text()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if len(lines) < 6:
        return False
    if len(_DOTS.findall(text)) >= 5:
        return True
    # A contents page ends most of its lines with the page number it points at.
    # A numbered component list ("1. Speakers") starts them with the number, and
    # is real content - telling the two apart matters, because treating a parts
    # list as navigation deletes it from the comparison entirely.
    trailing = sum(1 for l in lines if re.search(r"\S\s+\d{1,3}$", l))
    return trailing >= len(lines) * 0.5


_QA_TITLE_RE = re.compile(r"q\s*&\s*a\s*index|qa index|faq index", re.I)


def _front_matter_pages(doc) -> set:
    """Page numbers of a Q&A / FAQ index called out in the PDF bookmarks.

    It is a list of links to other sections, reworded per channel same as any
    other navigation aid, so its wording is compared nowhere - same reasoning
    validate_toc_content.py applies via SKIP_SECTIONS, expressed here as pages
    rather than sections because this comparator has no notion of a section.
    """
    try:
        toc = doc.get_toc()
    except Exception:
        toc = []
    pages = set()
    for idx, item in enumerate(toc):
        if not _QA_TITLE_RE.search(item[1] or ""):
            continue
        start = item[2]
        end = next((nxt[2] for nxt in toc[idx + 1:] if nxt[2] > start), start + 1)
        pages.update(range(start, end))
    return pages


# ── the two documents, read once ─────────────────────────────────────────────
@dataclass
class Side:
    tag: str
    path: str
    doc: fitz.Document
    pages: list = field(default_factory=list)     # per-page plain text
    flats: list = field(default_factory=list)     # per-page flat() text
    nav: set = field(default_factory=set)         # 1-based nav page numbers
    figs: dict = field(default_factory=dict)      # page -> [fitz.Rect]
    fig_ocr: dict = field(default_factory=dict)   # (page, i) -> ocr text
    missing_langs: set = field(default_factory=set)   # traineddata this doc needs
    page_missing: dict = field(default_factory=dict)  # page -> packs it needed

    @property
    def whole(self) -> str:
        return "".join(self.flats)


def load(tag: str, path: str) -> Side:
    doc = fitz.open(path)
    s = Side(tag=tag, path=path, doc=doc)
    s.nav.add(1)                        # cover / title page - never content
    s.nav |= _front_matter_pages(doc)   # Q&A / FAQ index - navigation, not content
    for i, page in enumerate(doc):
        text = page.get_text()
        s.pages.append(text)
        s.flats.append(flat(text))
        if _is_nav(page):
            s.nav.add(i + 1)
    return s


# ── page correspondence ──────────────────────────────────────────────────────
def map_pages(prod: Side, stage: Side) -> dict:
    """PROD page number -> STAGE page number, by token-set overlap.

    Re-pagination is the normal case, not the exception: the same manual runs to
    a different length once it is re-flowed. Comparing page 20 against page 20
    would report the whole document as changed.
    """
    p_tok = [set(words(t)) for t in prod.pages]
    s_tok = [set(words(t)) for t in stage.pages]
    out = {}
    for i, a in enumerate(p_tok):
        if not a:
            continue
        best, score = 0, 0.0
        for j, b in enumerate(s_tok):
            if not b:
                continue
            sim = len(a & b) / len(a | b)
            if sim > score:
                best, score = j + 1, sim
        out[i + 1] = (best, round(score, 3)) if score >= PAGE_SIM_MIN else (0, 0.0)
    return out


def paired(pmap: dict, pno: int, confident: float = 0.0) -> int:
    """The STAGE page for a PROD page, or 0 when the match is not trustworthy."""
    page, score = pmap.get(pno, (0, 0.0))
    return page if score >= confident else 0


# ── OCR ──────────────────────────────────────────────────────────────────────
OCR_ROTATIONS = (0, 90, 270)     # illustrations letter their parts sideways
OCR_MODES     = ("11", "6")      # sparse callouts, then block text
OCR_MIN_WORDS = 6                # below this, escalate to the harder passes


def _ocr_once(pix, psm: str, lang: str = "eng") -> str:
    fd, png = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        pix.save(png)
        r = subprocess.run(["tesseract", png, "stdout", "-l", lang,
                            "--psm", psm],
                           capture_output=True, text=True, timeout=90)
        return r.stdout or ""
    except Exception:
        return ""
    finally:
        try:
            os.unlink(png)
        except OSError:
            pass


def _ocr(page, rect: fitz.Rect, lang: str = "eng") -> str:
    """Read a region of a page optically, in every orientation.

    A label printed up the side of a bottle or along a cable is still a label a
    reader can see. Read upright only, it comes back empty - and a label that is
    there gets reported as one that was dropped, which is the worst kind of
    finding a comparison can make.
    """
    if not _HAS_OCR:
        return ""
    zoom = OCR_DPI / 72.0

    def shot(deg, invert):
        try:
            pix = page.get_pixmap(clip=rect,
                                  matrix=fitz.Matrix(zoom, zoom).prerotate(deg))
        except Exception:
            return None
        if invert:
            pix.invert_irect()
        return pix

    upright = shot(0, False)
    out = [_ocr_once(upright, psm, lang) for psm in OCR_MODES] if upright else []
    if len([w for w in words(" ".join(out)) if letters(w)]) >= OCR_MIN_WORDS:
        return "\n".join(out)

    # The region read as near-empty. That is either artwork with no lettering at
    # all, or lettering this pass cannot see: set sideways, or knocked out white
    # on a dark badge. Both are ordinary in a technical illustration, and both
    # produce a false "label missing" if left unread, so it is worth the passes.
    for deg in OCR_ROTATIONS:
        for invert in (False, True):
            if deg == 0 and not invert:
                continue
            pix = shot(deg, invert)
            if pix is not None:
                out.append(_ocr_once(pix, "11", lang))
    return "\n".join(out)


def _artwork(page) -> list:
    """Regions of the page that a reader sees as a picture.

    Both shapes count: a placed bitmap, and a vector illustration that has no
    image object at all. Without the second, a print-authored manual looks like
    it contains no figures.
    """
    rects = []
    for info in page.get_images(full=True):
        for r in page.get_image_rects(info[0]):
            if min(r.width, r.height) >= FIG_MIN_PT:
                rects.append(fitz.Rect(r))
    drawings = page.get_drawings()
    if drawings:
        boxes = [fitz.Rect(d["rect"]) for d in drawings
                 if min(d["rect"].width, d["rect"].height) > 4]
        for r in _cluster(boxes):
            if min(r.width, r.height) >= FIG_MIN_PT and not any(r in q for q in rects):
                rects.append(r)
    return _cluster(rects)


def _cluster(rects: list, gap: float = 14.0) -> list:
    """Merge rectangles that touch or nearly touch into one region."""
    out = []
    for r in sorted(rects, key=lambda x: (round(x.y0), round(x.x0))):
        r = fitz.Rect(r)
        merged = False
        for i, q in enumerate(out):
            g = fitz.Rect(q); g = g + (-gap, -gap, gap, gap)
            if g.intersects(r):
                out[i] = q | r
                merged = True
                break
        if not merged:
            out.append(r)
    return out


def read_figures(side: Side, pages=None, on_page=None) -> None:
    """Populate `figs` and `fig_ocr` for the given pages (default: all).

    `on_page(done, total)` is called after each page. Reading every figure of a
    manual optically is the longest phase of a comparison, and without this the
    caller could only announce it once and then sit silent until it ended.
    """
    todo = list(pages if pages is not None else range(1, side.doc.page_count + 1))
    total = len(todo)
    for done, pno in enumerate(todo, 1):
        if on_page:
            on_page(done, total)
        if pno in side.figs or pno < 1 or pno > side.doc.page_count:
            continue
        page = side.doc[pno - 1]
        rects = _artwork(page)
        side.figs[pno] = rects
        lang, missing = ocr_langs_for(side.pages[pno - 1])
        if missing:
            side.missing_langs.update(missing)
            side.page_missing[pno] = set(missing)
        for i, r in enumerate(rects):
            side.fig_ocr[(pno, i)] = _ocr(page, r, lang)


# ── differences ──────────────────────────────────────────────────────────────
@dataclass
class Diff:
    did: str
    kind: str                 # the headline: what sort of difference this is
    severity: str             # high | medium | low
    detail: str
    text: str                 # the wording at issue
    prod_page: int = 0
    stage_page: int = 0
    prod_rects: list = field(default_factory=list)
    stage_rects: list = field(default_factory=list)
    lane: str = "text"        # text | image
    merged: int = 0           # how many further lines were folded into this one
    stage_text: str = ""      # the replacement wording, when this is a change


def _locate(side: Side, pno: int, needle: str) -> list:
    """Word rectangles for `needle` on one page, for boxing it in a crop."""
    if not pno or pno > side.doc.page_count:
        return []
    want = words(needle)
    if not want:
        return []
    toks, rects = [], []
    for w in side.doc[pno - 1].get_text("words"):
        for t in words(w[4]):
            toks.append(t)
            rects.append(fitz.Rect(w[0], w[1], w[2], w[3]))
    for s in (i for i, t in enumerate(toks) if t == want[0]):
        if toks[s:s + len(want)] == want:
            return rects[s:s + len(want)]
    return []


def _corpus(side: Side) -> str:
    """Every word of the document in reading order, space-delimited.

    A line is compared against this rather than against the facing page, so
    re-flow, re-pagination and a paragraph moving to another chapter are all
    invisible - only wording that is gone anywhere is a difference.
    """
    return " " + " ".join(w for i, text in enumerate(side.pages, 1)
                          if i not in side.nav
                          for w in words(_XREF.sub(" ", _dehyphen(text)))) + " "


def _probe(line: str) -> list:
    """The words of a line, minus the two that a line break can truncate.

    A print build wraps mid-word ("con-" / "nected") and mid-sentence; the first
    and last token of any long line are therefore fragments of their neighbours'
    lines, not wording of their own. Dropping them is what separates a paragraph
    that re-flowed from one that was deleted.
    """
    w = words(_XREF.sub(" ", line))
    return w[1:-1] if len(w) >= 4 else w


def text_differences(prod: Side, stage: Side, pmap: dict, on_page=None) -> list:
    """Wording present on one side and nowhere on the other."""
    out = []
    p_corpus, s_corpus = _corpus(prod), _corpus(stage)
    s_art = _page_ocr_corpus(stage, on_page=on_page)
    p_art = ""      # PROD is read exactly; only STAGE needs an optical pass

    def sweep(src: Side, corpus: str, artwork: str):
        seen, found = set(), []
        for pno, text in enumerate(src.pages, 1):
            if pno in src.nav:
                continue
            for line in text.splitlines():
                s = line.strip()
                if _DOTS.search(s):
                    continue
                probe = _probe(s)
                if len(probe) < 2:
                    continue
                needle = " " + " ".join(probe) + " "
                if needle in corpus or " ".join(probe) in artwork:
                    continue
                key = needle
                if key in seen:
                    continue
                seen.add(key)
                found.append((pno, s))
        return found

    for pno, s in sweep(prod, s_corpus, s_art):
        out.append(Diff(
            did="", kind="Text missing from STAGE", severity="high",
            detail="This wording is in PROD and appears nowhere in STAGE - "
                   "not on the facing page, not elsewhere in the document, "
                   "and not inside any of its artwork.",
            text=s, prod_page=pno, stage_page=paired(pmap, pno),
            prod_rects=_locate(prod, pno, s)))
    for pno, s in sweep(stage, p_corpus, p_art):
        out.append(Diff(
            did="", kind="Text added in STAGE", severity="medium",
            detail="This wording is in STAGE and appears nowhere in PROD.",
            text=s, stage_page=pno,
            stage_rects=_locate(stage, pno, s)))
    return out


def _page_ocr_corpus(side: Side, on_page=None) -> str:
    """Flat OCR text of every figure on every page, read lazily and cached."""
    read_figures(side, on_page=on_page)
    return " " + " ".join(w for t in side.fig_ocr.values()
                          for w in words(t)) + " "


def _text_coverage(page, rect: fitz.Rect) -> float:
    """How much of a region is occupied by body text lines.

    The artwork detector clusters vector strokes, and a bordered table or a
    shaded note panel is made of strokes too. A region that is mostly words is a
    text block wearing a box, and comparing it as a picture reports the whole
    paragraph as a missing label.
    """
    if rect.get_area() <= 0:
        return 1.0
    covered = 0.0
    for block in page.get_text("blocks"):
        r = fitz.Rect(block[:4]) & rect
        if r.is_valid and not r.is_empty and (block[4] or "").strip():
            covered += r.get_area()
    return covered / rect.get_area()


CALLOUT_MAX_WORDS = 5      # a figure label is a caption, not a sentence


def _figure_labels(side: Side, pno: int, rect: fitz.Rect) -> list:
    """(word, rect) for every callout drawn inside an artwork region.

    Only lines short enough to be a label count. A sentence that happens to fall
    inside a bordered panel is body text, and reporting its every word as a lost
    figure label buries the two or three that really were lost.
    """
    out = []
    page = side.doc[pno - 1]
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            text = "".join(s.get("text", "") for s in line.get("spans", []))
            toks = words(text)
            if not toks or len(toks) > CALLOUT_MAX_WORDS:
                continue
            r = fitz.Rect(line["bbox"])
            if not r.get_area():
                continue
            if (r & rect).get_area() / r.get_area() <= 0.7:
                continue
            for tok in toks:
                out.append((tok, r))
    return out


def _label_worthy(token: str) -> bool:
    """Is this token substantial enough to report as a lost figure label?

    The minimum length applies to spaced scripts only. A Chinese or Japanese
    label is frequently one character, and requiring three would drop every CJK
    figure label on the floor.
    """
    if not letters(token):
        return False
    if any(_syllabic(c) for c in token):
        return True
    return len(token) >= LABEL_MIN_LEN


def _stage_vocab(stage: Side, spno: int, span: int = 1) -> set:
    """Every word a reader can see near the STAGE counterpart page.

    Only what STAGE's own artwork carries counts - read optically, and as text
    where STAGE draws its labels as text. The neighbouring pages are included
    because a re-flowed build routinely pushes a figure over a page boundary.
    Comparing against the whole page instead would be useless here: a word like
    "Headphone" appears in the parts list of every monitor manual, and a figure
    that genuinely lost that label would pass.
    """
    lo = max(1, spno - span)
    hi = min(stage.doc.page_count, spno + span)
    pages = list(range(lo, hi + 1))
    read_figures(stage, pages)
    vocab = set()
    for pno in pages:
        for i, rect in enumerate(stage.figs.get(pno, [])):
            vocab |= set(words(stage.fig_ocr.get((pno, i), "")))
            vocab |= {w for w, _r in _figure_labels(stage, pno, rect)}
    return vocab


def _read_well(stage: Side, spno: int, doc_vocab: set) -> bool:
    """Did OCR actually read this page's artwork, or just return noise?

    Counting characters would say yes to any smudge. Counting only the tokens
    that are also real words of this manual separates a region OCR understood
    from one it guessed at - and a negative result from a region it guessed at
    is not evidence of anything.
    """
    got = set(words(_stage_ink(stage, spno, span=0)))
    return len(got & doc_vocab) >= OCR_MIN_WORDS


def _stage_ink(stage: Side, spno: int, span: int = 1) -> str:
    """Everything OCR managed to read from the STAGE artwork near this page.

    How much came back is the measure of whether a negative result means
    anything. A region that read as ten words and does not contain the label is
    evidence; a region that read as nothing is not.
    """
    lo = max(1, spno - span)
    hi = min(stage.doc.page_count, spno + span)
    return " ".join(stage.fig_ocr.get((pno, i), "")
                    for pno in range(lo, hi + 1)
                    for i in range(len(stage.figs.get(pno, []))))


def figure_differences(prod: Side, stage: Side, pmap: dict,
                       already: set | None = None, on_page=None) -> list:
    """Lettering the PROD artwork carries and the STAGE artwork does not.

    PROD's labels are read exactly, as text - there is no reason to OCR a
    document that will tell you its own words. Only STAGE is read optically, and
    only to answer one question: is this label somewhere in that bitmap?
    """
    out = []
    already = already or set()
    if not _HAS_OCR:
        return out
    doc_vocab = set(words(" ".join(stage.pages)))


    for pno in range(1, prod.doc.page_count + 1):
        if on_page:
            on_page(pno, prod.doc.page_count)
        if pno in prod.nav:
            continue
        spno = paired(pmap, pno, confident=0.35)
        if not spno:
            continue
        page = prod.doc[pno - 1]
        for rect in _artwork(page):
            if _text_coverage(page, rect) > 0.20:
                continue
            labels = _figure_labels(prod, pno, rect)
            if not labels:
                continue
            vocab = _stage_vocab(stage, spno)
            missing = [(w, r) for w, r in labels
                       if _label_worthy(w)
                       and w not in vocab and w not in already]
            if not missing:
                continue
            # A script this machine cannot OCR makes a negative read on THAT
            # page meaningless. It says nothing about any other page.
            near = set()
            for q in range(max(1, spno - 1), min(stage.doc.page_count, spno + 1) + 1):
                near |= stage.page_missing.get(q, set())
            confident = _read_well(stage, spno, doc_vocab) and not near
            seen, shown, rects = set(), [], []
            for w, r in missing:
                if w not in seen:
                    seen.add(w)
                    shown.append(w)
                rects.append(r)
            out.append(Diff(
                did="",
                kind=("Figure label missing from STAGE" if confident
                      else "Figure label not found in STAGE - needs a human look"),
                severity="high" if confident else "medium",
                detail=("The illustration is on both sides, but this lettering is "
                        "drawn on the PROD artwork and reads nowhere in STAGE - "
                        "not as text on the page, and not in the pixels of the "
                        "STAGE artwork when it is rendered and read optically."
                        if confident else
                        "This lettering is drawn on the PROD artwork and was not "
                        "found in STAGE. Treat it as a question, not a verdict: "
                        "the STAGE artwork could not be read with confidence, so "
                        "the label may well be there - too small, too fine, too "
                        "low in contrast, or in a script this machine has no "
                        "OCR language pack for. The two crops below settle it at "
                        "a glance."),
                text=", ".join(shown[:14]),
                prod_page=pno, stage_page=spno,
                prod_rects=rects, stage_rects=stage.figs.get(spno, [])[:1],
                lane="image"))
    return out


def _group(diffs: list) -> list:
    """Fold consecutive findings of one kind on one page into a single row.

    A section that was dropped wholesale is one difference, not the forty lines
    it happened to occupy. Keeping them apart makes a report that is technically
    complete and practically unreadable.
    """
    out = []
    for d in diffs:
        prev = out[-1] if out else None
        same = (prev and prev.kind == d.kind and prev.lane == d.lane
                and prev.prod_page == d.prod_page
                and prev.stage_page == d.stage_page)
        if same:
            if prev.lane == "image":
                have = {w.strip() for w in prev.text.split(",")}
                add = [w.strip() for w in d.text.split(",") if w.strip() not in have]
                prev.text = prev.text + (", " + ", ".join(add) if add else "")
            else:
                prev.text = prev.text + " / " + d.text
            prev.prod_rects = prev.prod_rects + d.prod_rects
            prev.stage_rects = prev.stage_rects + d.stage_rects
            prev.merged += 1
        else:
            out.append(d)
    for d in out:
        if d.merged:
            d.detail = ("%d consecutive lines on this page differ the same way. "
                        % (d.merged + 1)) + d.detail
    return out


def _pair(diffs: list) -> list:
    """Match a loss to the addition that replaced it, and report one change.

    A corrected typo shows up twice - the old wording missing, the new wording
    added. Read separately they look like two defects; read together they are one
    edit, and the reader can see what it was.
    """
    losses = [d for d in diffs if d.kind == "Text missing from STAGE"]
    adds = [d for d in diffs if d.kind == "Text added in STAGE"]
    used, out = set(), []
    for a in losses:
        av = set(words(a.text))
        best, score = None, 0.0
        for b in adds:
            if id(b) in used or not b.stage_page:
                continue
            if a.stage_page and abs(b.stage_page - a.stage_page) > 1:
                continue
            bv = set(words(b.text))
            if not av or not bv:
                continue
            sim = len(av & bv) / len(av | bv)
            if sim > score:
                best, score = b, sim
        if best is not None and score >= 0.55:
            used.add(id(best))
            a.kind = "Wording changed"
            a.severity = "medium"
            a.detail = ("PROD reads one way and STAGE another. Both wordings are "
                        "shown; nothing else in either document carries the "
                        "PROD version.")
            a.stage_page = best.stage_page
            a.stage_rects = best.stage_rects
            a.stage_text = best.text
    for d in diffs:
        if d.kind == "Text added in STAGE" and id(d) in used:
            continue
        out.append(d)
    return out


def compare(prod_path: str, stage_path: str, progress=None) -> tuple:
    def say(pct, msg):
        if progress:
            progress(pct, msg)

    say(5, "Reading PROD")
    prod = load("PROD", prod_path)
    say(15, "Reading STAGE")
    stage = load("STAGE", stage_path)
    say(25, "Matching pages")
    pmap = map_pages(prod, stage)
    # The two long phases report per page. Announcing a phase once and then
    # working through a whole manual in silence left the bar frozen for minutes,
    # which reads as a hung run rather than a slow one.
    def span(lo, hi, what):
        def on_page(done, total):
            frac = done / total if total else 1.0
            say(int(lo + (hi - lo) * frac), f"{what} — page {done} of {total}")
        return on_page

    say(35, "Comparing wording")
    diffs = text_differences(prod, stage, pmap,
                             on_page=span(35, 55, "Reading STAGE artwork"))
    reported = {w for d in diffs for w in words(d.text)}
    say(55, "Reading artwork" + ("" if _HAS_OCR else " (OCR unavailable - skipped)"))
    diffs += figure_differences(prod, stage, pmap, already=reported,
                                on_page=span(55, 85, "Comparing artwork"))

    diffs = _pair(_group(diffs))
    order = {"high": 0, "medium": 1, "low": 2}
    diffs.sort(key=lambda d: (order.get(d.severity, 3), d.lane != "image",
                              d.prod_page or d.stage_page))
    for n, d in enumerate(diffs, 1):
        d.did = f"D{n}"
    seen, unique = set(), []
    for d in diffs:
        key = (d.kind, d.lane, d.prod_page, d.stage_page, flat(d.text))
        if key not in seen:
            seen.add(key)
            unique.append(d)
    diffs = unique
    say(85, f"{len(diffs)} differences")
    return diffs, prod, stage, pmap

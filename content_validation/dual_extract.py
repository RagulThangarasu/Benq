"""PDF → structured document model, in a single pass.

One walk over a PDF produces two views of it:

    * a CONTENT tree — headings, paragraphs, list items and table rows, with the
      wording normalised. This is what the content lane compares, and it is
      deliberately free of geometry so that re-pagination and re-flow cannot
      register as differences.

    * a LAYOUT model — page geometry: where the text sits, which regions are
      artwork, how many columns each table has and over how many pages it runs.
      This is what the visual lane compares.

Both come from the same pass, so a document is parsed once no matter how many
checks run against it. Everything is plain data (dict/list/str) so it can be
serialised to JSON, cached, diffed or inspected on its own.

This module is self-contained: it shares no code with validate_toc_content.py,
which continues to serve the existing "content" and "style" modes untouched.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field, asdict

import fitz

# ── tuning ───────────────────────────────────────────────────────────────────
MIN_READABLE_PT = 6.5      # below this, text is artwork lettering, not content
HEADING_RATIO   = 1.18     # this much larger than body text ⇒ a heading
FIG_DPI         = 100      # render scale used to find artwork
FIG_MIN_PT      = 46       # smallest side of a real figure, in points

_WORD_RE   = re.compile(r"[^\W_]+", re.UNICODE)
_BARE_NUM  = re.compile(r"^\d{1,3}$")
_NUM_MARK  = re.compile(r"^\s*(\d{1,2})\s*[.)]\s+(\S.*)$")
_ALPHA_MARK= re.compile(r"^\s*([a-zA-Z])\s*[.)]\s+(\S.*)$")
_BULL_MARK = re.compile(r"^\s*[•●▪‣⁃·*]\s+(\S.*)$")
_TOC_LINE  = re.compile(r"\.{3,}\s*\d{1,4}\s*$")
_PUA       = re.compile(r"[-]")
_ENTITY    = re.compile(r"&#x?[0-9A-Fa-f]{2,6};?|&(?:amp|lt|gt|quot|nbsp);")

try:                                     # optional: only the visual lane needs it
    import numpy as _np
    import cv2 as _cv2
    _CV = True
except Exception:                        # pragma: no cover
    _CV = False


# ── text helpers ─────────────────────────────────────────────────────────────
def norm_words(text: str) -> list:
    """Comparable words: letters/digits only, lowercased, bare numbers dropped.

    Bare numbers go because list markers and page numbers are renumbered by
    layout and would otherwise read as content changes.
    """
    out = []
    for m in _WORD_RE.finditer(unicodedata.normalize("NFKC", text or "")):
        w = m.group(0).lower()
        if not _BARE_NUM.match(w):
            out.append(w)
    return out


def norm_key(text: str) -> str:
    return " ".join(norm_words(text))


def _is_dark(span) -> bool:
    if span.get("flags", 0) & (1 << 4):
        return True
    return bool(re.search(r"bold|black|heavy|semib|demib|extrab",
                          span.get("font", "") or "", re.I))


def _is_italic(span) -> bool:
    if span.get("flags", 0) & (1 << 1):
        return True
    return bool(re.search(r"italic|oblique", span.get("font", "") or "", re.I))


# ── data model ───────────────────────────────────────────────────────────────
@dataclass
class Block:
    """One piece of content: a heading, paragraph, list item or table row."""
    kind: str                 # heading | para | list | table_row | table_head
    text: str
    key: str                  # normalised wording, used for all matching
    page: int
    level: int = 0            # heading depth
    marker: str = ""          # list marker style: number | letter | bullet
    marker_inline: bool = True   # marker on the same line as its text
    bold: bool = False
    italic: bool = False
    size: float = 0.0
    table_id: str = ""        # which table a row belongs to
    columns: int = 0
    left: float = 0.0         # x of the line's start, for alignment comparison
    right: float = 0.0        # x of the line's end


@dataclass
class TableInfo:
    table_id: str
    header: str
    columns: int
    first_page: int
    last_page: int
    rows: int = 0


@dataclass
class FigureInfo:
    page: int
    rect: tuple
    caption: str = ""
    caption_key: str = ""
    callouts: list = field(default_factory=list)


@dataclass
class Document:
    path: str
    pages: int
    blocks: list = field(default_factory=list)
    tables: dict = field(default_factory=dict)
    figures: list = field(default_factory=list)
    links: list = field(default_factory=list)
    glitches: list = field(default_factory=list)
    body_size: float = 0.0
    nav_pages: set = field(default_factory=set)

    # -- content view -------------------------------------------------------
    def sections(self) -> list:
        """[(heading Block or None, [content Blocks])] in document order.

        A section runs from a heading to the next heading of the same or higher
        level, so a parent carries its sub-headings' content too.
        """
        heads = [i for i, b in enumerate(self.blocks) if b.kind == "heading"]
        out = []
        if not heads:
            return [(None, list(self.blocks))]
        if heads[0] > 0:
            out.append((None, self.blocks[:heads[0]]))
        for n, i in enumerate(heads):
            lvl = self.blocks[i].level
            end = len(self.blocks)
            for j in heads[n + 1:]:
                if self.blocks[j].level <= lvl:
                    end = j
                    break
            out.append((self.blocks[i], self.blocks[i + 1:end]))
        return out

    def to_json(self) -> str:
        d = asdict(self)
        d["nav_pages"] = sorted(self.nav_pages)
        d["tables"] = {k: asdict(v) for k, v in self.tables.items()}
        d["figures"] = [asdict(f) for f in self.figures]
        d["blocks"] = [asdict(b) for b in self.blocks]
        return json.dumps(d, ensure_ascii=False, indent=1)

    def to_markdown(self) -> str:
        """The document as Markdown, preserving supported block formatting."""
        lines = []
        active_table = ""

        def format_text(block: Block) -> str:
            text = block.text.strip()
            if block.bold and block.italic:
                return f"***{text}***"
            if block.bold:
                return f"**{text}**"
            if block.italic:
                return f"*{text}*"
            return text

        def table_cells(block: Block) -> list[str]:
            cells = [cell.strip() for cell in block.text.split(" | ")]
            cells.extend([""] * max(0, block.columns - len(cells)))
            return cells[:block.columns] if block.columns else cells

        for b in self.blocks:
            if b.kind == "heading":
                lines.append(f"{'#' * max(1, min(6, b.level))} {format_text(b)}")
            elif b.kind == "list":
                prefix = "1." if b.marker == "number" else "-"
                lines.append(f"{prefix} {format_text(b)}")
            elif b.kind == "table_head":
                cells = table_cells(b)
                lines.append("| " + " | ".join(cells) + " |")
                lines.append("| " + " | ".join("---" for _ in cells) + " |")
                active_table = b.table_id
            elif b.kind == "table_row":
                if b.table_id != active_table:
                    lines.append("")
                    active_table = b.table_id
                lines.append("| " + " | ".join(table_cells(b)) + " |")
            else:
                lines.append(format_text(b))
            if b.kind not in ("table_head", "table_row"):
                active_table = ""
        return "\n\n".join(line for line in lines if line)


# ── page classification ──────────────────────────────────────────────────────
def _nav_pages(doc) -> set:
    """Contents / index pages, which are navigation rather than content."""
    out = {1}
    for i, page in enumerate(doc, 1):
        text = page.get_text()
        if len(re.findall(r"\.{4,}", text)) >= 8:
            out.add(i)
            continue
        lines = [l for l in text.splitlines() if l.strip()]
        if len(lines) >= 8:
            numbered = sum(1 for l in lines if re.search(r"\s\d{1,3}\s*$", l))
            if numbered >= len(lines) * 0.6:
                out.add(i)
    return out


def _page_lines(page):
    """[(text, Rect, [spans])] for every non-empty line, in reading order."""
    out = []
    blocks = sorted(
        (b for b in page.get_text("dict")["blocks"] if b.get("type") == 0),
        key=lambda b: (round(b["bbox"][1], 1), round(b["bbox"][0], 1)))
    for blk in blocks:
        for line in blk.get("lines", []):
            spans = [s for s in line.get("spans", [])
                     if s.get("size", 0) >= MIN_READABLE_PT
                     and (s.get("text") or "").strip()]
            if not spans:
                continue
            txt = "".join(s["text"] for s in spans).strip()
            if txt:
                out.append((txt, fitz.Rect(line["bbox"]), spans))
    return out


def _body_size(doc, nav) -> float:
    """The most common font size — the document's body text."""
    counts = {}
    for i, page in enumerate(doc, 1):
        if i in nav:
            continue
        for _t, _r, spans in _page_lines(page):
            for s in spans:
                k = round(s.get("size", 0), 1)
                counts[k] = counts.get(k, 0) + len(s.get("text", ""))
    return max(counts, key=counts.get) if counts else 10.0


# ── figures ──────────────────────────────────────────────────────────────────
def _detect_figures(page, dpi=FIG_DPI):
    """Artwork regions, found by rendering the page and erasing the text.

    Asking the rendered page "what ink is left once the words are gone" answers
    what a reader sees as a picture. Reading it off the PDF's vector grouping
    does not: that groups a shaded note panel, or an illustration and the table
    beneath it, into one object.
    """
    if not _CV:
        return []
    z = dpi / 72.0
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(z, z), colorspace=fitz.csGRAY)
        img = _np.frombuffer(pix.samples, dtype=_np.uint8).reshape(
            pix.height, pix.width).copy()
    except Exception:
        return []
    ox, oy = page.rect.x0, page.rect.y0
    for _t, r, _s in _page_lines(page):
        x0 = max(0, int((r.x0 - ox) * z) - 1); x1 = max(0, int((r.x1 - ox) * z) + 2)
        y0 = max(0, int((r.y0 - oy) * z) - 1); y1 = max(0, int((r.y1 - oy) * z) + 2)
        img[y0:y1, x0:x1] = 255
    ink = ((img < 230).astype(_np.uint8)) * 255
    kern = _cv2.getStructuringElement(_cv2.MORPH_RECT, (5, 5))
    closed = _cv2.morphologyEx(ink, _cv2.MORPH_CLOSE, kern, iterations=1)
    n, _lab, stats, _c = _cv2.connectedComponentsWithStats(closed, 8)
    out = []
    for i in range(1, n):
        x, y, w, h, area = stats[i]
        if area < 400:
            continue
        w_pt, h_pt = w / z, h / z
        if max(w_pt, h_pt) < FIG_MIN_PT or min(w_pt, h_pt) < 20:
            continue
        out.append(fitz.Rect(ox + x / z, oy + y / z,
                             ox + (x + w) / z, oy + (y + h) / z))
    return out


def _caption_for(rect, lines, limit=130.0):
    """The nearest line of real words above or below a figure."""
    best, best_d = "", 1e9
    cx = (rect.x0 + rect.x1) / 2
    for txt, r, _s in lines:
        if not (3 <= len(norm_words(txt)) <= 16):
            continue
        if r.y1 <= rect.y0:
            d = rect.y0 - r.y1
        elif r.y0 >= rect.y1:
            d = r.y0 - rect.y1
        else:
            continue
        d += abs(((r.x0 + r.x1) / 2) - cx) * 0.25
        if d < best_d and d <= limit:
            best, best_d = txt, d
    return best


# ── the one pass ─────────────────────────────────────────────────────────────
def extract(pdf_path: str, with_layout: bool = True) -> Document:
    """Parse a PDF once into a Document carrying both views."""
    doc = fitz.open(pdf_path)
    nav = _nav_pages(doc)
    body = _body_size(doc, nav)
    out = Document(path=pdf_path, pages=doc.page_count, nav_pages=nav,
                   body_size=body)

    outline = {}
    try:
        for lvl, title, pno in (doc.get_toc() or []):
            outline.setdefault(norm_key(title), lvl)
    except Exception:
        pass

    for i, page in enumerate(doc, 1):
        if i in nav:
            continue
        lines = _page_lines(page)

        # -- glitches: things wrong with the page itself -------------------
        for txt, _r, spans in lines:
            for m in _ENTITY.finditer(txt):
                out.glitches.append({"page": i, "kind": "HTML entity in text",
                                     "text": m.group(0)})
            for m in _PUA.finditer(txt):
                out.glitches.append({"page": i, "kind": "Private-use glyph",
                                     "text": f"U+{ord(m.group(0)):04X}"})

        # -- content blocks -------------------------------------------------
        for idx, (txt, rect, spans) in enumerate(lines):
            if _TOC_LINE.search(txt):
                continue
            size = max((s.get("size", 0) for s in spans), default=0)
            bold = any(_is_dark(s) for s in spans)
            ital = all(_is_italic(s) for s in spans)
            key = norm_key(txt)
            if not key:
                continue

            lvl = outline.get(key)
            if lvl is None and size >= body * HEADING_RATIO and len(key.split()) <= 14:
                lvl = 1 if size >= body * 1.45 else 2
            if lvl:
                out.blocks.append(Block("heading", txt, key, i, level=lvl,
                                        bold=bold, italic=ital, size=size,
                                        left=rect.x0, right=rect.x1))
                continue

            marker, body_txt, inline = "", txt, True
            m = _NUM_MARK.match(txt) or _ALPHA_MARK.match(txt)
            if m:
                marker = "number" if m is _NUM_MARK.match(txt) else "letter"
                marker = "number" if _NUM_MARK.match(txt) else "letter"
                body_txt = m.group(2)
            elif _BULL_MARK.match(txt):
                marker, body_txt = "bullet", _BULL_MARK.match(txt).group(1)
            elif re.fullmatch(r"\s*\d{1,2}\s*[.)]\s*", txt):
                # A marker stranded on its own line: its text is the next line.
                nxt = lines[idx + 1][0] if idx + 1 < len(lines) else ""
                if nxt and not re.fullmatch(r"\s*\d{1,2}\s*[.)]\s*", nxt):
                    out.blocks.append(Block(
                        "list", nxt, norm_key(nxt), i, marker="number",
                        marker_inline=False, bold=bold, italic=ital, size=size,
                        left=rect.x0, right=rect.x1))
                continue

            out.blocks.append(Block(
                "list" if marker else "para", body_txt, norm_key(body_txt), i,
                marker=marker, bold=bold, italic=ital, size=size,
                left=rect.x0, right=rect.x1))

        # -- tables ----------------------------------------------------------
        try:
            found = page.find_tables()
        except Exception:
            found = None
        for tbl in (getattr(found, "tables", []) or []):
            try:
                rows = tbl.extract()
            except Exception:
                continue
            if not rows or tbl.col_count < 2:
                continue
            head = [re.sub(r"\s+", " ", (c or "")).strip() for c in rows[0]]
            shown = " | ".join(h for h in head if h)
            tid = norm_key(shown)
            if len(tid.split()) < 2:
                continue
            info = out.tables.get(tid)
            if info is None:
                out.tables[tid] = TableInfo(tid, shown, tbl.col_count, i, i,
                                            len(rows))
                out.blocks.append(Block("table_head", shown, tid, i,
                                        table_id=tid, columns=tbl.col_count))
            else:
                info.last_page = max(info.last_page, i)
                info.rows += len(rows)
            for r in rows[1:]:
                cells = [re.sub(r"\s+", " ", (c or "")).strip() for c in r]
                line = " | ".join(c for c in cells if c)
                if norm_key(line):
                    out.blocks.append(Block("table_row", line, norm_key(line), i,
                                            table_id=tid,
                                            columns=tbl.col_count))

        # -- figures ----------------------------------------------------------
        if with_layout:
            for rect in _detect_figures(page):
                cap = _caption_for(rect, lines)
                near = fitz.Rect(rect.x0 - 40, rect.y0 - 40,
                                 rect.x1 + 40, rect.y1 + 40)
                calls = sorted({int(t) for t, r, _s in lines
                                if re.fullmatch(r"\s*\d{1,2}\s*", t)
                                and near.intersects(r)})
                out.figures.append(FigureInfo(
                    i, (rect.x0, rect.y0, rect.x1, rect.y1),
                    cap, norm_key(cap), calls))

        # -- links -------------------------------------------------------------
        for l in page.get_links():
            kind = l.get("kind")
            rec = {"page": i, "kind": kind}
            if kind == fitz.LINK_URI:
                rec["uri"] = (l.get("uri") or "").strip()
            elif kind == fitz.LINK_LAUNCH:
                rec["file"] = (l.get("file") or "").strip()
            elif kind in (fitz.LINK_GOTO, fitz.LINK_NAMED):
                rec["target"] = l.get("page", -1)
            out.links.append(rec)

    doc.close()
    return out


def fingerprint(pdf_path: str) -> str:
    """Cheap identity for caching an extraction."""
    st = os.stat(pdf_path)
    return hashlib.md5(
        f"{os.path.abspath(pdf_path)}:{st.st_size}:{int(st.st_mtime)}".encode()
    ).hexdigest()

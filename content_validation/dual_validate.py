"""Content + Visual validation, built on the single-pass extractor.

Two independent lanes run over the same parsed documents:

    CONTENT  — compares the normalised content trees. Wording only: whether every
               piece of PROD's text is present in STAGE, matched section by
               section. Geometry is deliberately invisible here, so re-flow and
               re-pagination cannot register as content loss.

    VISUAL   — compares the layout models. Table shape and page breaks, list
               marker style and alignment, figure presence and captions, emphasis,
               and defects a page carries on its own (broken text layer, blank
               images, dead links).

PROD is the reference throughout: defects are reported against STAGE.

Every finding carries `evidence` — the page and the exact words — so the report
can box it without searching for it a second time.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import fitz

from . import dual_extract as DX


@dataclass
class Finding:
    lane: str          # content | visual
    kind: str
    doc: str           # PROD | STAGE — where the defect is
    page: int
    topic: str
    detail: str
    evidence: str = ""      # text to box in the screenshot
    severity: str = "issue"


# ── content lane ─────────────────────────────────────────────────────────────
_STOP_KINDS = {"table_head"}


def _section_text(blocks) -> list:
    """Comparable words of a section, prose and table rows alike."""
    words = []
    for b in blocks:
        if b.kind in _STOP_KINDS:
            continue
        words.extend(b.key.split())
    return [w for w in words if w]


def _original_span(owner, frag) -> str:
    """The owner block's own wording for `frag`, with its casing intact.

    Falls back to the block's text when the slice cannot be identified, so a
    finding always quotes something a reader can search for.
    """
    words = re.split(r"(\s+)", owner.text)
    toks = [w for w in words if w.strip()]
    for i in range(len(toks)):
        acc, j = [], i
        while j < len(toks) and len(acc) < len(frag):
            acc.extend(DX.norm_words(toks[j]))
            j += 1
        if acc[:len(frag)] == frag:
            return re.sub(r"\s+", " ", " ".join(toks[i:j])).strip()
    # Alignment failed: quote exactly what was tested rather than widening to
    # the whole block, which would show words STAGE does have and read as a
    # false report.
    return " ".join(frag)


def _section_words_with_source(blocks):
    """([word, ...], [owning Block, ...]) — parallel lists.

    Keeping the owning block lets a finding quote the sentence as the document
    actually prints it, rather than the lowercased comparison form.
    """
    words, owners = [], []
    for b in blocks:
        if b.kind in _STOP_KINDS:
            continue
        for w in b.key.split():
            if w:
                words.append(w)
                owners.append(b)
    return words, owners


def _shingles(words, n=6):
    return {" ".join(words[i:i + n]) for i in range(max(0, len(words) - n + 1))}


def _page_word_index(pdf_path: str, nav_pages: set):
    """{page: {word: [positions]}} from RAW page text.

    Raw, per page, because a fragment is only worth reporting if it genuinely
    reads that way somewhere — a section stream is sorted and can string
    scattered labels into a sentence that appears nowhere on the page.
    """
    idx, doc = [], fitz.open(pdf_path)
    for i in range(doc.page_count):
        if (i + 1) in nav_pages:
            continue
        d = {}
        for pos, w in enumerate(DX.norm_words(doc[i].get_text())):
            d.setdefault(w, []).append(pos)
        idx.append(d)
    doc.close()
    return idx


def _reads_verbatim(page_idx, words) -> bool:
    """True when `words` run consecutively on at least one page."""
    if not words:
        return False
    for d in page_idx:
        starts = d.get(words[0])
        if not starts:
            continue
        for s in starts:
            if all((s + k) in d.get(w, ()) for k, w in enumerate(words)):
                return True
            # positions are absolute; walk them properly
            ok, pos = True, s
            for w in words[1:]:
                nxt = [p for p in d.get(w, ()) if p == pos + 1]
                if not nxt:
                    ok = False
                    break
                pos += 1
            if ok:
                return True
    return False


def content_lane(prod: DX.Document, stage: DX.Document,
                 min_run: int = 4) -> list:
    """Text present in PROD that STAGE does not have."""
    stage_words = _section_text(stage.blocks)
    stage_set = _shingles(stage_words)
    stage_flat = " ".join(stage_words)
    # Word boundaries are not reliable across two renderers: a line break lost in
    # extraction turns "60Hz USB" into "60HzUSB", so the same text tokenises
    # differently on each side. Comparing the letters with the spaces removed is
    # immune to that, and is the last word on whether STAGE has the text.
    stage_chars = "".join(stage_words)
    # Last word on the matter: the raw STAGE pages. The section streams are
    # normalised and sliced, and a phrase can look absent there while sitting
    # plainly on the page. Anything that reads verbatim in STAGE is not missing.
    stage_pages = _page_word_index(stage.path, stage.nav_pages)
    prod_pages = _page_word_index(prod.path, prod.nav_pages)

    findings, seen = [], set()
    for head, blocks in prod.sections():
        topic = head.text if head else "(before the first heading)"
        page = head.page if head else (blocks[0].page if blocks else 1)
        words, owners = _section_words_with_source(blocks)
        if len(words) < min_run:
            continue
        covered = [" ".join(words[i:i + 6]) in stage_set
                   for i in range(len(words))]
        run = []
        for i, ok in enumerate(covered + [True]):
            if not ok:
                run.append(i)
                continue
            if len(run) >= min_run:
                frag = words[run[0]:run[-1] + 1]
                phrase = " ".join(frag)
                if (phrase not in stage_flat
                        and "".join(frag) not in stage_chars
                        and phrase not in seen):
                    # must genuinely read this way in PROD, or it is an artifact
                    # of the section stream rather than a sentence PROD prints
                    if (_reads_verbatim(prod_pages, frag)
                            and not _reads_verbatim(stage_pages, frag)):
                        seen.add(phrase)
                        # Quote the sentence as PROD prints it, not the
                        # lowercased comparison form.
                        owner = owners[run[0]]
                        shown = _original_span(owner, frag)
                        findings.append(Finding(
                            "content", "Content missing", "STAGE",
                            owner.page or page, topic,
                            f"Present in PROD, absent from STAGE: “{shown[:200]}”",
                            evidence=shown))
            run = []
    return findings


# ── visual lane ──────────────────────────────────────────────────────────────
def _tables_lane(prod, stage) -> list:
    out = []
    for tid, p in prod.tables.items():
        s = stage.tables.get(tid)
        if s is None:
            continue
        if s.columns != p.columns:
            out.append(Finding(
                "visual", "Table columns differ", "STAGE", s.first_page,
                f"Table “{p.header}”",
                f"PROD lays this table out in {p.columns} columns; "
                f"STAGE renders it in {s.columns}.", evidence=p.header))
        p_span = p.last_page - p.first_page + 1
        s_span = s.last_page - s.first_page + 1
        if s_span > p_span:
            out.append(Finding(
                "visual", "Table layout broken", "STAGE", s.first_page,
                f"Table “{p.header}”",
                f"The table is split by a page break: it runs over {s_span} "
                f"pages (p{s.first_page}–{s.last_page}) where PROD keeps it on "
                f"{p_span}. Rows are separated from their header.",
                evidence=p.header))

    # a header row PROD has that no STAGE table header carries
    stage_heads = set(stage.tables)
    stage_flat = " ".join(b.key for b in stage.blocks)
    for tid, p in prod.tables.items():
        if tid in stage_heads:
            continue
        if all(w in stage_flat for w in tid.split()):
            continue          # the wording is there; only detection differs
        out.append(Finding(
            "visual", "Table heading missing", "STAGE", p.first_page,
            f"Table “{p.header}”",
            f"PROD has a table headed “{p.header}”; no STAGE table carries "
            f"that header.", evidence=p.header))
    return out


def _lists_lane(prod, stage) -> list:
    out = []
    prod_lists = {b.key: b for b in prod.blocks if b.kind == "list" and b.key}
    stage_lists = {b.key: b for b in stage.blocks if b.kind == "list" and b.key}

    for key, pb in prod_lists.items():
        sb = stage_lists.get(key)
        if sb is None:
            continue
        if pb.marker and sb.marker and pb.marker != sb.marker:
            out.append(Finding(
                "visual", "List marker changed", "STAGE", sb.page,
                f"List item on STAGE page {sb.page}",
                f"PROD marks this item with a {pb.marker}; STAGE uses a "
                f"{sb.marker}. Item: “{pb.text[:120]}”", evidence=sb.text))
        if pb.marker_inline and not sb.marker_inline:
            out.append(Finding(
                "visual", "List alignment broken", "STAGE", sb.page,
                f"List item on STAGE page {sb.page}",
                f"The number is stranded on its own line and “{sb.text[:90]}” "
                f"wraps below it. PROD sets this step on one line.",
                evidence=sb.text))
    return out


def _figures_lane(prod, stage) -> list:
    """Figure captions and callout numbering.

    Figures themselves are matched only when a caption identifies exactly one
    figure on each side. Anything looser pairs unrelated artwork, and a wrong
    picture is worse than none.
    """
    out = []
    p_caps, s_caps = {}, {}
    for f in prod.figures:
        if f.caption_key:
            p_caps.setdefault(f.caption_key, []).append(f)
    for f in stage.figures:
        if f.caption_key:
            s_caps.setdefault(f.caption_key, []).append(f)

    stage_flat = " ".join(b.key for b in stage.blocks)
    for key, figs in p_caps.items():
        if len(figs) != 1:
            continue
        pf = figs[0]
        if key not in s_caps:
            if all(w in stage_flat for w in key.split()):
                continue       # the caption text is there, just not by a figure
            out.append(Finding(
                "visual", "Image label missing", "STAGE", pf.page,
                f"Figure on PROD page {pf.page}",
                f"The figure captioned “{pf.caption[:110]}” has no counterpart "
                f"caption in STAGE.", evidence=pf.caption))
            continue
        s_figs = s_caps[key]
        if len(s_figs) != 1:
            continue
        sf = s_figs[0]
        p_rect, s_rect = fitz.Rect(pf.rect), fitz.Rect(sf.rect)
        prod_width, stage_width = p_rect.width, s_rect.width
        prod_height, stage_height = p_rect.height, s_rect.height
        width_delta = abs(prod_width - stage_width)
        height_delta = abs(prod_height - stage_height)
        width_changed = width_delta > 5.0 and width_delta / max(prod_width, 1) > 0.10
        height_changed = height_delta > 5.0 and height_delta / max(prod_height, 1) > 0.10
        if width_changed or height_changed:
            differences = []
            if width_changed:
                differences.append(f"width PROD {prod_width:.1f} pt, STAGE {stage_width:.1f} pt")
            if height_changed:
                differences.append(f"height PROD {prod_height:.1f} pt, STAGE {stage_height:.1f} pt")
            out.append(Finding(
                "visual", "Image dimensions differ", "STAGE", sf.page,
                f"Figure “{sf.caption[:110]}”",
                "The on-page image size differs: " + "; ".join(differences) + ".",
                evidence=sf.caption))
        if pf.callouts and sf.callouts and set(pf.callouts) != set(sf.callouts):
            miss = sorted(set(pf.callouts) - set(sf.callouts))
            if miss:
                out.append(Finding(
                    "visual", "Image label missing", "STAGE", sf.page,
                    f"Diagram on STAGE page {sf.page}",
                    f"PROD labels this diagram "
                    f"{', '.join(str(n) for n in pf.callouts)}; STAGE is missing "
                    f"{', '.join(str(n) for n in miss)}.",
                    evidence=sf.caption or str(sf.callouts[0])))

    # numbering that skips values, within STAGE itself
    for f in stage.figures:
        if len(f.callouts) < 3:
            continue
        lo, hi = min(f.callouts), max(f.callouts)
        gaps = [n for n in range(lo, hi + 1) if n not in f.callouts]
        if gaps:
            out.append(Finding(
                "visual", "Image label missing", "STAGE", f.page,
                f"Diagram on STAGE page {f.page}",
                f"The diagram is numbered "
                f"{', '.join(str(n) for n in f.callouts)} — the label(s) "
                f"{', '.join(str(n) for n in gaps)} are not there. The leader "
                f"lines are drawn but their numbers are missing.",
                evidence=str(f.callouts[0])))
    return out


_EMPH_RATIO = 1.08     # this much larger than body text ⇒ emphasised


def _emphasis_lane(prod: DX.Document, stage: DX.Document) -> list:
    """Text PROD sets larger than its body copy that STAGE sets at body size.

    Emphasis is judged ONLY by size relative to each document's own body text.
    Font family and weight are ignored on purpose: the two documents are set in
    different typefaces, so "is this face called Bold" and "is the bold flag set"
    answer a question about the toolchain rather than about what the reader sees.
    Raising a line above the body size is the one signal both documents share.
    """
    out = []
    p_body = prod.body_size or 10.0
    s_body = stage.body_size or 10.0

    # Largest size each wording is set at, on each side. The same words often
    # appear once as a prominent line and again as ordinary prose, and only the
    # most prominent occurrence answers "does this document emphasise it".
    def biggest(document):
        m = {}
        for b in document.blocks:
            if not b.key:
                continue
            if b.key not in m or b.size > m[b.key][0]:
                m[b.key] = (b.size, b.page, b.text)
        return m

    p_big, s_big = biggest(prod), biggest(stage)

    for key, (p_size, p_page, p_text) in p_big.items():
        if p_size < p_body * _EMPH_RATIO:
            continue                       # not emphasised in PROD
        hit = s_big.get(key)
        if hit is None:
            continue                       # absent from STAGE: a content issue
        s_size, s_page, _s_text = hit
        if s_size >= s_body * _EMPH_RATIO:
            continue                       # emphasised in STAGE too
        out.append(Finding(
            "visual", "Emphasis lost", "STAGE", s_page,
            f"Text on STAGE page {s_page}",
            f"“{p_text[:110]}” is set at {p_size:.1f} pt in PROD, above its "
            f"{p_body:.1f} pt body text, but STAGE sets it at {s_size:.1f} pt — "
            f"the same as its own {s_body:.1f} pt body text, so it no longer "
            f"reads as emphasised.",
            evidence=p_text))
    return out


_ALIGN_TOL = 14.0       # points of drift tolerated before it is a difference.
                        # Two manuals set to different designs differ slightly
                        # on nearly every line; only a shift big enough to change
                        # the reading — a list losing its indent — is a defect.


def _alignment_lane(prod: DX.Document, stage: DX.Document) -> list:
    """Text that STAGE indents differently from PROD.

    Compared against a baseline, not in absolute terms: the two documents have
    different page margins, so every line differs by some constant. The median
    shift IS that constant, and only lines that deviate from it have actually
    moved relative to their own page.
    """
    p_pos, s_pos = {}, {}
    for store, document in ((p_pos, prod), (s_pos, stage)):
        for b in document.blocks:
            # List items only. Indentation carries meaning for a list — it is
            # what shows nesting — whereas a paragraph's exact left edge is a
            # design choice that differs between two manuals on every line.
            if b.kind != "list" or not b.key:
                continue
            if len(b.key.split()) < 5:
                continue                  # too short to place reliably
            store.setdefault(b.key, b)

    pairs = []
    for key, pb in p_pos.items():
        sb = s_pos.get(key)
        if sb is None or not pb.left or not sb.left:
            continue
        pairs.append((key, pb.page, sb.page, sb.left - pb.left,
                      sb.right - pb.right))
    if len(pairs) < 6:
        return []                 # too little overlap for a baseline to mean anything

    shifts = sorted(p[3] for p in pairs)
    baseline = shifts[len(shifts) // 2]

    out, seen = [], set()
    for key, pp, sp, dl, dr in pairs:
        drift = dl - baseline
        if abs(drift) <= _ALIGN_TOL:
            continue
        bucket = (sp, round(drift / 6.0))
        if bucket in seen:
            continue              # one row per group of lines that moved together
        seen.add(bucket)
        pts = abs(round(drift))
        side = "left" if drift < 0 else "right"
        centred = abs(drift + (dr - baseline)) <= _ALIGN_TOL
        if centred:
            cause = " It is centred rather than set to the margin."
        elif drift < 0:
            cause = " The indent PROD gives it is lost."
        else:
            cause = " STAGE indents it where PROD sets it to the margin."
        out.append(Finding(
            "visual", "Text alignment changed", "STAGE", sp,
            f"Text on STAGE page {sp}",
            f"“{key[:80]}” starts {pts} pt further {side} than in PROD "
            f"(PROD p{pp}).{cause}", evidence=key))
    return out


def _integrity_lane(stage) -> list:
    """Defects STAGE carries on its own, needing no comparison."""
    out = []
    for g in stage.glitches:
        out.append(Finding(
            "visual", g["kind"], "STAGE", g["page"],
            f"STAGE page {g['page']}",
            f"{g['text']} appears in the text layer — copy, search and screen "
            f"readers will read the wrong characters.", evidence=g["text"]))
    for l in stage.links:
        if l["kind"] == fitz.LINK_URI:
            uri = l.get("uri", "")
            if uri and not re.match(r"^(https?|mailto):", uri, re.I):
                out.append(Finding(
                    "visual", "Hyperlink broken", "STAGE", l["page"],
                    f"Link on STAGE page {l['page']}",
                    f"“{uri}” has no usable scheme, so it cannot open.",
                    evidence=uri))
        elif l["kind"] in (fitz.LINK_GOTO, fitz.LINK_NAMED):
            t = l.get("target", -1)
            if t is None or t < 0:
                out.append(Finding(
                    "visual", "Hyperlink broken", "STAGE", l["page"],
                    f"Link on STAGE page {l['page']}",
                    "An internal link points at a destination that does not "
                    "resolve.", evidence=""))
    return out


def visual_lane(prod: DX.Document, stage: DX.Document) -> list:
    return (_tables_lane(prod, stage) + _lists_lane(prod, stage)
            + _figures_lane(prod, stage) + _emphasis_lane(prod, stage)
            + _alignment_lane(prod, stage)
            + _integrity_lane(stage))


# ── entry point ──────────────────────────────────────────────────────────────
def validate(prod_path: str, stage_path: str, progress=None):
    """Run both lanes. Returns (findings, prod Document, stage Document)."""
    def tick(frac, msg):
        if progress:
            try:
                progress(frac, msg)
            except Exception:
                pass

    tick(0.05, "reading PROD")
    prod = DX.extract(prod_path)
    tick(0.40, "reading STAGE")
    stage = DX.extract(stage_path)
    tick(0.70, "content validation")
    findings = content_lane(prod, stage)
    tick(0.85, "visual validation")
    findings += visual_lane(prod, stage)
    tick(0.95, "done")

    # One row per distinct problem. The same short label ("TIP:", "NOTE:")
    # appears on many pages, and reporting each occurrence separately buries the
    # rest of the report under repeats of one defect.
    seen, unique = set(), []
    for f in findings:
        k = (f.kind, DX.norm_key(f.evidence) or f.detail)
        if k in seen:
            continue
        seen.add(k)
        unique.append(f)
    findings = unique

    order = {"Content missing": 0, "Image label missing": 1,
             "Table layout broken": 2, "Table columns differ": 3,
             "Table heading missing": 4, "List alignment broken": 5,
             "List marker changed": 6, "Text alignment changed": 7,
             "Emphasis lost": 8,
             "Bold lost": 8, "Italic lost": 9,
             "Hyperlink broken": 10}
    findings.sort(key=lambda f: (order.get(f.kind, 99), f.page))
    return findings, prod, stage


def metrics(findings, prod: DX.Document, stage: DX.Document) -> dict:
    """Headline percentages for the top of the report."""
    p_heads = [b for b in prod.blocks if b.kind == "heading"]
    s_keys = {b.key for b in stage.blocks if b.kind == "heading"}
    matched = sum(1 for h in p_heads if h.key in s_keys)
    topics = {f.topic for f in findings if f.lane == "content"}
    n_sections = max(1, len(prod.sections()))
    p_words = len(_section_text(prod.blocks)) or 1
    lost = sum(len(f.evidence.split()) for f in findings
               if f.kind == "Content missing")
    return {
        "headings_matched": 100.0 * matched / max(1, len(p_heads)),
        "headings": (matched, len(p_heads)),
        "topics_clean": 100.0 * (n_sections - len(topics)) / n_sections,
        "topics": (n_sections - len(topics), n_sections),
        "text_reproduced": max(0.0, 100.0 * (p_words - lost) / p_words),
        "issues": len(findings),
        "content_issues": sum(1 for f in findings if f.lane == "content"),
        "visual_issues": sum(1 for f in findings if f.lane == "visual"),
    }

"""Local-only AI PDF-to-PDF content validator (Ollama, offline, no API keys).

Built for speed and completeness:

1. Extract the full text of PROD and STAGE, split into paragraphs.
2. Run ONE mechanical paragraph-level diff over the whole document -- this
   captures every difference exactly and instantly, and is not thrown off by
   section/page renumbering the way per-section slicing is.
3. Send the changed blocks to a local Ollama model in a FEW batched calls
   (not one per block), asking it to label each block GENUINE (a real content
   change STAGE made vs the PROD baseline) or FALSE (PDF text-extraction noise
   -- reordered captions, on-screen-menu word-salad, hyphenation).
4. Write a report that lists every genuine difference. Blocks the model does
   not clearly label are kept as genuine so nothing is lost.

PROD is the baseline; STAGE is expected to match it.

Usage:
    python -m content_validation.ai_validate PROD.pdf STAGE.pdf [-o report.md]

Because only the changed blocks (not whole sections) are sent, and they go in a
handful of batched calls, `llama3.1:8b` finishes a ~60-page manual pair in
roughly 90-120 seconds while still giving real per-change judgement.

Env:
    OLLAMA_MODEL    default llama3.1:8b   (llama3.2:3b / :1b are faster but their
                                           genuine/noise calls are unreliable)
    OLLAMA_HOST     default http://127.0.0.1:11434
    AI_BATCH_CHARS  default 6000          (diff chars per model call)
    AI_JOBS         default 3             (parallel model calls)
    SKIP_SECTIONS   default "q&a|questions and answers"  (regex, case-insensitive)
"""
from __future__ import annotations

import argparse
import concurrent.futures as _cf
import datetime as _dt
import difflib
import json
import os
import re
import sys
import time
import urllib.request

import pymupdf

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "llama3.1:8b")
AI_BATCH_CHARS = int(os.environ.get("AI_BATCH_CHARS", "6000"))
AI_JOBS = int(os.environ.get("AI_JOBS", "5"))
SKIP_RE = re.compile(os.environ.get("SKIP_SECTIONS", r"q\s*&\s*a|questions?\s+and\s+answers?"),
                     re.I)

_WORD = re.compile(r"[^\W\d_]{2,}", re.UNICODE)


# --------------------------------------------------------------------------- #
# text extraction
# --------------------------------------------------------------------------- #
def _toc_ranges(doc):
    """[(title, first_page, last_page)] from the PDF outline."""
    toc = doc.get_toc()
    out = []
    for i, (lvl, title, start) in enumerate(toc):
        end = toc[i + 1][2] if i + 1 < len(toc) else doc.page_count + 1
        out.append((title.strip(), start, max(start, end - 1)))
    return out


def _paragraphs(path):
    """Return [(para_text, page_no, section_title)] for the whole document."""
    doc = pymupdf.open(path)
    ranges = _toc_ranges(doc)

    def section_of(pageno):
        hit = ""
        for title, a, b in ranges:
            if a <= pageno <= b:
                hit = title
        return hit

    paras = []
    for pno in range(doc.page_count):
        blocks = doc[pno].get_text("blocks")
        blocks.sort(key=lambda b: (round(b[1] / 3), b[0]))
        for b in blocks:
            txt = re.sub(r"-\n(?=\w)", "", b[4].strip())  # de-hyphenate
            txt = re.sub(r"\s+", " ", txt.replace("\n", " ")).strip()
            if len(_WORD.findall(txt)) < 2:               # page nos, "1.", bullets
                continue
            paras.append((txt, pno + 1, section_of(pno + 1)))
    return paras


# --------------------------------------------------------------------------- #
# mechanical diff
# --------------------------------------------------------------------------- #
_STOP_PHRASE = re.compile(r"\bon page\s+\d+\b|\bpage\s+\d+\b|\.{2,}", re.I)


def _key(t):
    """Normalise away formatting so only real wording differences remain."""
    t = t.lower()
    t = _STOP_PHRASE.sub(" ", t)              # cross-ref page nos, TOC dot leaders
    t = re.sub(r"[•▪◦·‣■•▪]", " ", t)   # bullet glyphs
    t = re.sub(r"[^a-z0-9]+", " ", t)
    t = re.sub(r"\b\d+\b(?=(?:\s+\d+\b)*\s*$)", " ", t)  # trailing bare page nos
    return re.sub(r"\s+", " ", t).strip()


def _wordset(t):
    return _key(t).split()


def _ratio(a, b):
    return difflib.SequenceMatcher(None, _wordset(a), _wordset(b),
                                   autojunk=False).ratio()


def _looks_real(text):
    """True if the text reads like prose or carries a spec value / number."""
    for ln in re.split(r"(?<=[.:;!?])\s+|\s{2,}", text):
        w = _WORD.findall(ln)
        if len(w) >= 5:
            return True
        if re.search(r"\d", ln) and len(w) >= 3:
            return True
    return False


def _prose_score(text):
    """(sentence_count, real_word_count) — used to skip the model on obvious cases."""
    sents = [s for s in re.split(r"(?<=[.!?])\s+", text) if len(_WORD.findall(s)) >= 4]
    words = [w for w in _WORD.findall(text) if len(w) >= 3]
    return len(sents), len(words)


def _word_delta(a, b):
    """Human-readable 'what changed' between two texts, word level."""
    aw, bw = a.split(), b.split()
    sm = difflib.SequenceMatcher(None, [w.lower() for w in aw],
                                 [w.lower() for w in bw], autojunk=False)
    rem, add = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            rem.append(" ".join(aw[i1:i2]))
        if tag in ("replace", "insert"):
            add.append(" ".join(bw[j1:j2]))
    return " / ".join(x for x in rem if x), " / ".join(x for x in add if x)


def _diff_blocks(prod, stage):
    """Whole-document paragraph diff -> list of change blocks."""
    pk = [_key(t) for t, _, _ in prod]
    sk = [_key(t) for t, _, _ in stage]
    sm = difflib.SequenceMatcher(None, pk, sk, autojunk=False)
    blocks = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue
        pt = [prod[i] for i in range(i1, i2)]
        st = [stage[j] for j in range(j1, j2)]
        sect = (pt[0][2] if pt else "") or (st[0][2] if st else "")
        page = (pt[0][1] if pt else None) or (st[0][1] if st else None)
        if SKIP_RE.search(sect or ""):
            continue
        ptext = " ".join(t for t, _, _ in pt)
        stext = " ".join(t for t, _, _ in st)
        kind = {"replace": "changed", "delete": "removed",
                "insert": "added"}[tag]
        b = {"kind": kind, "section": sect, "page": page,
             "prod": ptext, "stage": stext, "label": "genuine", "note": ""}
        if kind == "changed":
            if _key(ptext) == _key(stext):
                continue                      # identical after normalisation
            rem, add = _word_delta(ptext, stext)
            b["prod_delta"], b["stage_delta"] = rem, add
            real = [w for w in _WORD.findall(rem + " " + add) if len(w) >= 3]
            if _ratio(ptext, stext) >= 0.97:
                b["label"] = "false"
                b["note"] = "formatting only (line wrap / bullet / hyphenation)"
            elif len(real) < 3:
                b["label"] = "false"
                b["note"] = "list numbering / cross-reference / caption only"
            else:
                ns, nw = _prose_score(rem + " " + add)
                if ns >= 2 and nw >= 14:      # clearly real prose -> skip model
                    b["skip_ai"] = True
                    b["note"] = "wording changed in body text"
        elif kind in ("added", "removed"):
            real = [w for w in _WORD.findall(ptext + " " + stext) if len(w) >= 3]
            if len(real) < 3:
                b["label"] = "false"
                b["note"] = "list numbering / label fragment only"
            else:
                ns, nw = _prose_score(ptext + " " + stext)
                if ns >= 2 and nw >= 14:
                    b["skip_ai"] = True
                    b["note"] = ("sentence(s) removed" if kind == "removed"
                                 else "sentence(s) added in STAGE")
        blocks.append(b)
    return blocks


# --------------------------------------------------------------------------- #
# AI labelling (batched)
# --------------------------------------------------------------------------- #
_SYSTEM = (
    "You are checking whether a newer revision (STAGE) of a product manual still "
    "matches the baseline revision (PROD). You get a numbered list of blocks "
    "that a mechanical diff found different. For each block decide:\n"
    "  genuine = STAGE really changed the content vs PROD: different wording in "
    "a real sentence, different number / spec value / model name / unit, or a "
    "sentence or bullet added or removed.\n"
    "  false = not a real change, only PDF text-extraction noise: reordered "
    "screenshot captions or menu labels, word-salad, hyphenation, page numbers, "
    "bullet symbols, whitespace.\n"
    "When unsure, answer genuine. Reply with JSON only, no prose:\n"
    '{"results":[{"id":<int>,"label":"genuine"|"false","note":"<= 15 words"}]}'
)


def _ollama(prompt):
    body = json.dumps({
        "model": OLLAMA_MODEL, "system": _SYSTEM, "prompt": prompt,
        "stream": False, "format": "json",
        "options": {"temperature": 0, "num_ctx": 6144, "num_predict": 800},
    }).encode()
    req = urllib.request.Request(f"{OLLAMA_HOST}/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["response"]


def _parse_results(raw):
    try:
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        return json.loads(m.group(0)).get("results", []) if m else []
    except Exception:  # noqa: BLE001
        out, dec = [], json.JSONDecoder()
        for mm in re.finditer(r"\{[^{}]*\"id\"[^{}]*\}", raw):
            try:
                out.append(dec.raw_decode(mm.group(0))[0])
            except Exception:  # noqa: BLE001
                pass
        return out


def _label(blocks):
    """Confirm genuine/false on the blocks that need judgement, batched."""
    todo = [(i, b) for i, b in enumerate(blocks) if b["label"] != "false"
            and not b.get("skip_ai") and (b["prod"] or b["stage"])]
    idx = {i: b for i, b in todo}
    if not todo:
        return

    def run(batch):
        lines = []
        for i, b in batch:
            pv = b.get("prod_delta", b["prod"]) or b["prod"] or "(nothing)"
            sv = b.get("stage_delta", b["stage"]) or b["stage"] or "(nothing)"
            lines.append(f"[{i}] section: {b['section'] or '-'} | {b['kind']}\n"
                         f"  PROD:  {pv[:600]}\n  STAGE: {sv[:600]}")
        raw = _ollama("Blocks:\n" + "\n\n".join(lines))
        for r in _parse_results(raw):
            try:
                bi = idx[int(r["id"])]
            except Exception:  # noqa: BLE001
                continue
            lbl = str(r.get("label", "")).strip().lower()
            if lbl in ("false", "noise", "artifact"):
                bi["label"] = "false"
            note = str(r.get("note", "")).strip()
            if note and not re.search(r"<[^>]+>|genuine\|false", note):
                bi["note"] = note

    groups, cur, size = [], [], 0
    for i, b in todo:
        piece = len(b.get("prod_delta", b["prod"])) + \
            len(b.get("stage_delta", b["stage"])) + 40
        if cur and size + piece > AI_BATCH_CHARS:
            groups.append(cur)
            cur, size = [], 0
        cur.append((i, b))
        size += piece
    if cur:
        groups.append(cur)
    with _cf.ThreadPoolExecutor(max_workers=max(1, AI_JOBS)) as ex:
        list(ex.map(run, groups))


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def _c(x, n=320):
    return str(x if x not in (None, "") else "-").replace("\n", " ").replace(
        "|", "\\|")[:n]


def _report_md(prod_path, stage_path, blocks, npara, secs):
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    genuine = [b for b in blocks if b["label"] == "genuine"]
    false_ = [b for b in blocks if b["label"] == "false"]
    sects = sorted({b["section"] for b in genuine if b["section"]})

    L = ["# PDF-to-PDF Content Validation Report", "",
         f"- **Generated:** {now}  (validated in {secs:.0f}s)",
         f"- **PROD (baseline):** `{os.path.basename(prod_path)}` — STAGE must "
         "match PROD; every difference below is a STAGE deviation.",
         f"- **STAGE (checked):** `{os.path.basename(stage_path)}`",
         f"- **AI validator:** Ollama / `{OLLAMA_MODEL}` (local, offline)",
         "",
         "## High-level result", "", "| | |", "|--|--|",
         f"| Paragraphs compared (PROD + STAGE) | {npara} |",
         f"| Diff blocks found | {len(blocks)} |",
         f"| **Genuine differences (STAGE vs PROD)** | **{len(genuine)}** |",
         f"| &nbsp;&nbsp;— text changed | {sum(b['kind']=='changed' for b in genuine)} |",
         f"| &nbsp;&nbsp;— content only in PROD (dropped by STAGE) | {sum(b['kind']=='removed' for b in genuine)} |",
         f"| &nbsp;&nbsp;— content only in STAGE (added) | {sum(b['kind']=='added' for b in genuine)} |",
         f"| Sections affected | {len(sects)} |",
         f"| Diff blocks judged FALSE (extraction noise) | {len(false_)} |",
         "",
         ("**STAGE matches the PROD baseline.**" if not genuine else
          f"**STAGE does NOT match PROD** — {len(genuine)} genuine difference(s) "
          f"across {len(sects)} section(s)."),
         "", "---", "",
         "## Genuine differences", "",
         "`PROD →` / `STAGE →` show only the words that actually differ; "
         "full paragraph text is in the JSON.", "",
         "| # | Page | Section | Change | PROD → | STAGE → | AI note |",
         "|---|------|---------|--------|--------|---------|---------|"]
    for i, b in enumerate(genuine, 1):
        pv = b.get("prod_delta") if b.get("prod_delta") is not None else b["prod"]
        sv = b.get("stage_delta") if b.get("stage_delta") is not None else b["stage"]
        if b["kind"] == "removed":
            pv, sv = b["prod"], "(removed)"
        if b["kind"] == "added":
            pv, sv = "(new)", b["stage"]
        L.append(f"| {i} | {b['page'] or '-'} | {_c(b['section'], 60)} | "
                 f"{b['kind']} | {_c(pv)} | {_c(sv)} | "
                 f"{_c(b['note'], 120)} |")
    L += ["", "---", "",
          "<details><summary>Appendix: diff blocks judged FALSE / extraction "
          f"noise — {len(false_)}</summary>", "",
          "| # | Page | Section | PROD | STAGE | AI note |",
          "|---|------|---------|------|-------|---------|"]
    for i, b in enumerate(false_, 1):
        L.append(f"| {i} | {b['page'] or '-'} | {_c(b['section'], 60)} | "
                 f"{_c(b['prod'])} | {_c(b['stage'])} | {_c(b['note'], 120)} |")
    L += ["", "</details>"]
    return "\n".join(L)


def _report_pdf(path, prod_path, stage_path, blocks, npara, secs):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle)

    genuine = [b for b in blocks if b["label"] == "genuine"]
    false_ = [b for b in blocks if b["label"] == "false"]
    sects = sorted({b["section"] for b in genuine if b["section"]})
    now = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    ss = getSampleStyleSheet()
    body = ParagraphStyle("body", parent=ss["BodyText"], fontSize=7.3,
                          leading=9, spaceAfter=0)
    head = ParagraphStyle("h", parent=ss["Heading2"], fontSize=13, spaceBefore=10)
    def cell(t, cap=420):
        t = str(t if t not in (None, "") else "-")
        if len(t) > cap:
            t = t[:cap] + " …"
        return Paragraph(t.replace("&", "&amp;").replace("<", "&lt;"), body)

    doc = SimpleDocTemplate(path, pagesize=landscape(A4),
                            leftMargin=12 * mm, rightMargin=12 * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm,
                            title="PDF-to-PDF Content Validation Report")
    story = [Paragraph("PDF-to-PDF Content Validation Report", ss["Title"])]
    verdict = ("STAGE matches the PROD baseline." if not genuine else
               f"STAGE does NOT match PROD — {len(genuine)} genuine "
               f"difference(s) across {len(sects)} section(s).")
    for line in (
            f"Generated: {now} (validated in {secs:.0f}s)",
            f"PROD (baseline): {os.path.basename(prod_path)}",
            f"STAGE (checked): {os.path.basename(stage_path)}",
            f"AI validator: Ollama / {OLLAMA_MODEL} (local, offline)",
            f"Paragraphs compared: {npara} | diff blocks: {len(blocks)} | "
            f"genuine: {len(genuine)} | extraction-noise: {len(false_)}",
            f"<b>{verdict}</b>"):
        story.append(Paragraph(line, body))

    def make_table(rows, cols, widths):
        data = [cols] + rows
        t = Table(data, colWidths=[w * mm for w in widths], repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2f2f2f")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, 0), 7.5),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#999999")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f2f2f2")]),
        ]))
        return t

    story.append(Paragraph("Genuine differences (STAGE vs PROD baseline)", head))
    rows = []
    for i, b in enumerate(genuine, 1):
        pv = b.get("prod_delta") if b.get("prod_delta") is not None else b["prod"]
        sv = b.get("stage_delta") if b.get("stage_delta") is not None else b["stage"]
        if b["kind"] == "removed":
            pv, sv = b["prod"], "(removed)"
        if b["kind"] == "added":
            pv, sv = "(new)", b["stage"]
        rows.append([cell(i), cell(b["page"]), cell(b["section"]),
                     cell(b["kind"]), cell(pv), cell(sv), cell(b["note"])])
    story.append(make_table(
        rows, ["#", "Pg", "Section", "Change", "PROD →", "STAGE →", "AI note"],
        [8, 10, 40, 16, 70, 70, 40]))

    story.append(Paragraph(
        f"Appendix - diff blocks judged extraction noise / not real ({len(false_)})",
        head))
    rows = []
    for i, b in enumerate(false_, 1):
        rows.append([cell(i), cell(b["page"]), cell(b["section"]),
                     cell(b["prod"]), cell(b["stage"]), cell(b["note"])])
    story.append(make_table(
        rows, ["#", "Pg", "Section", "PROD", "STAGE", "note"],
        [8, 10, 38, 92, 92, 30]))
    doc.build(story)


# --------------------------------------------------------------------------- #
def validate(prod_path, stage_path):
    prod, stage = _paragraphs(prod_path), _paragraphs(stage_path)
    blocks = _diff_blocks(prod, stage)
    print(f"{len(blocks)} diff blocks; labelling with {OLLAMA_MODEL} ...",
          file=sys.stderr)
    if blocks:
        _label(blocks)
    return blocks, len(prod) + len(stage)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("prod")
    ap.add_argument("stage")
    ap.add_argument("-o", "--out", default="ai_validation_report.md")
    a = ap.parse_args(argv)
    t0 = time.time()
    blocks, npara = validate(a.prod, a.stage)
    secs = time.time() - t0
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as fh:
        fh.write(_report_md(a.prod, a.stage, blocks, npara, secs))
    base = os.path.splitext(a.out)[0]
    with open(base + ".json", "w") as fh:
        json.dump(blocks, fh, indent=2)
    try:
        _report_pdf(base + ".pdf", a.prod, a.stage, blocks, npara, secs)
        pdf_msg = f"\nWrote {base}.pdf"
    except Exception as e:  # noqa: BLE001
        pdf_msg = f"\nPDF skipped: {e}"
    g = sum(b["label"] == "genuine" for b in blocks)
    print(f"\nDone in {secs:.0f}s — {g} genuine differences\n"
          f"Wrote {a.out}\nWrote {base}.json{pdf_msg}")


if __name__ == "__main__":
    main()

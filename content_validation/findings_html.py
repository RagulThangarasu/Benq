"""One HTML report, whatever produced the findings.

Every validation mode in this app answers the same question in its own vocabulary
- a Finding dataclass here, a dict there, a dozen keyword lists somewhere else.
This module defines the one shape they all reduce to, an adapter per mode, and a
single renderer. Turning the HTML report on therefore costs a mode an adapter,
not a second report.

The page is self-contained: evidence images are embedded, so it opens from a
download, an email attachment or a file share with nothing else alongside it.
"""
from __future__ import annotations

import base64
import html
import os
import re
from dataclasses import dataclass, field

# ── the one shape ────────────────────────────────────────────────────────────
@dataclass
class Row:
    kind: str                       # what sort of difference
    text: str                       # the wording or thing at issue
    detail: str = ""                # why it is a difference
    severity: str = "medium"        # high | medium | low
    prod_page: int = 0
    stage_page: int = 0
    lane: str = "text"              # text | image | style
    stage_text: str = ""            # the replacement, when this is a change
    prod_png: bytes = b""           # evidence, already boxed
    stage_png: bytes = b""


SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


# ── adapters ─────────────────────────────────────────────────────────────────
def from_compare(diffs, shots=None) -> list:
    """content_validation.pdf_compare.Diff -> Row."""
    shots = shots or {}
    out = []
    for d in diffs:
        p, s = shots.get(d.did, (b"", b""))
        out.append(Row(kind=d.kind, text=d.text, detail=d.detail,
                       severity=d.severity, prod_page=d.prod_page,
                       stage_page=d.stage_page, lane=d.lane,
                       stage_text=getattr(d, "stage_text", ""),
                       prod_png=p, stage_png=s))
    return out


def from_dual(findings) -> list:
    """content_validation.dual_validate.Finding -> Row."""
    out = []
    for f in findings:
        on_stage = getattr(f, "doc", "STAGE") == "STAGE"
        out.append(Row(
            kind=f.kind,
            text=getattr(f, "evidence", "") or getattr(f, "topic", ""),
            detail=f"{getattr(f, 'topic', '')} - {f.detail}".strip(" -"),
            severity="high" if f.lane == "content" else "medium",
            prod_page=0 if on_stage else f.page,
            stage_page=f.page if on_stage else 0,
            lane="image" if f.lane == "visual" else "text"))
    return out


def from_style(findings) -> list:
    """content_validation.style_validation dict findings -> Row."""
    out = []
    for f in findings:
        out.append(Row(
            kind=str(f.get("category", "Style difference")),
            text=str(f.get("evidence") or f.get("text") or f.get("detail", ""))[:400],
            detail=str(f.get("detail") or f.get("message", "")),
            severity=str(f.get("severity", "medium") or "medium").lower()
                     if str(f.get("severity", "")).lower() in SEV_ORDER else "medium",
            prod_page=_page(f.get("prod_page")),
            stage_page=_page(f.get("stage_page") or f.get("page")),
            lane="style"))
    return out


# Which of generate_report's keyword lists carry findings, and what to call them.
_TOC_LISTS = {
    "table_findings": "Table difference", "glitches": "Encoding or garbling",
    "heading_findings": "Heading difference", "label_findings": "Image label",
    "link_findings": "Hyperlink difference", "pageno_findings": "Page number",
    "bold_findings": "Emphasis difference", "figure_findings": "Figure difference",
    "icon_findings": "Icon difference", "align_findings": "Alignment difference",
    "italic_findings": "Emphasis difference", "figdiff_findings": "Figure difference",
    "pixel_findings": "Visual difference", "liststyle_findings": "List style",
    "tableshape_findings": "Table shape", "tablebreak_findings": "Table break",
    "tablecont_findings": "Table continuation header missing",
    "tablemerge_findings": "Table cell layout difference",
    "callgap_findings": "Image callout numbering",
    "textalign_findings": "Text alignment",
    "linkloss_findings": "Hyperlink difference",
    "labelmerge_findings": "Paragraph alignment",
}

# The issue label each list carries, in the same vocabulary the PDF report's
# _REPORTED_ISSUES scope uses. Without this the HTML report rendered every list
# generate_report was handed, including the kinds the PDF deliberately leaves
# out, so narrowing the PDF's scope had no effect here. Items that carry their
# own "kind" (link, page-number, icon and encoding findings) use that instead.
_ISSUE_FOR_LIST = {
    "table_findings": "Table cell missing",
    "heading_findings": "Table heading missing",
    "label_findings": "Image label missing",
    "bold_findings": "Bold lost",
    "figure_findings": "Image missing",
    "align_findings": "List alignment broken",
    "italic_findings": "Italic lost",
    "figdiff_findings": "Image not correctly updated",
    "pixel_findings": "Image quality",
    "liststyle_findings": "List marker changed",
    "tableshape_findings": "Table column layout differs",
    "tablebreak_findings": "Table broken",
    "tablecont_findings": "Table broken",
    "tablemerge_findings": "Table cell layout differs",
    "callgap_findings": "Diagram callout number missing",
    "textalign_findings": "Text alignment changed",
    "labelmerge_findings": "Paragraph merged with heading",
}


def _issue_label(key: str, item) -> str:
    """The scope label for one finding — its own "kind" wins, else the list's."""
    if isinstance(item, dict):
        kind = (item.get("kind") or "").strip()
        if kind:
            return kind
    if key == "linkloss_findings":
        doc = item.get("doc") if isinstance(item, dict) else None
        return "Hyperlink lost" if doc != "STAGE" else "Hyperlink added in STAGE"
    return _ISSUE_FOR_LIST.get(key, "")


def _page(value) -> int:
    """A page number out of whatever the validator put there.

    These lists are built for a PDF table, where a missing page is written as
    "-" or "n/a" and nobody minds. Read as data they have to come back as 0.
    """
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _stringify(item) -> tuple:
    """(text, detail, prod page, stage page) out of whatever shape an item is."""
    if isinstance(item, dict):
        text = item.get("evidence") or item.get("text") or item.get("title") or ""
        detail = item.get("detail") or item.get("message") or item.get("reason") or ""
        return (str(text), str(detail),
                _page(item.get("prod_page")),
                _page(item.get("stage_page") or item.get("page")))
    if isinstance(item, (list, tuple)):
        parts = [str(x) for x in item]
        pages = [x for x in item if isinstance(x, int)]
        return (" | ".join(parts[:3]), " | ".join(parts[3:6]),
                pages[0] if pages else 0, pages[1] if len(pages) > 1 else 0)
    return (str(item), "", 0, 0)


def from_toc_kwargs(captured: dict) -> list:
    """The keyword lists generate_report() was called with -> Row.

    Filtered through the same scope the PDF report uses, so both formats show
    the same findings. A kind the PDF leaves out is left out here too.
    """
    from .validate_toc_content import _is_reported

    out = []
    for key, kind in _TOC_LISTS.items():
        for item in (captured.get(key) or []):
            issue = _issue_label(key, item)
            if not issue or not _is_reported(issue):
                continue
            text, detail, pp, sp = _stringify(item)
            if not text.strip():
                continue
            out.append(Row(kind=issue, text=text[:400], detail=detail[:600],
                           severity="high" if "missing" in issue.lower() else "medium",
                           prod_page=pp, stage_page=sp,
                           lane="image" if "figure" in kind.lower()
                                or "image" in kind.lower() else "text"))
    # Section rows are not findings in themselves — only the fragments a section
    # is actually missing are. Emitting one row per section listed every passing
    # topic as a "Content difference".
    for item in (captured.get("content_results") or []):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        for frag in (item.get("missing") or []):
            if not str(frag).strip():
                continue
            out.append(Row(kind="Content missing", text=str(frag)[:400],
                           detail=f"In PROD under “{title}”, absent from STAGE"[:600],
                           severity="high",
                           prod_page=_page(item.get("prod_page")),
                           stage_page=_page(item.get("stage_page"))))
    for item in (captured.get("toc_results") or []):
        if not isinstance(item, dict):
            continue
        # A heading that survived the move but changed outline depth. Nothing
        # else reports it: the text matches, so every content check passes.
        if item.get("level_status") == "Changed":
            out.append(Row(
                kind="TOC level changed",
                text=str(item.get("title") or "")[:400],
                detail=(f"Outline depth L{item.get('prod_level')} in PROD, "
                        f"L{item.get('stage_level')} in STAGE")[:600],
                severity="medium",
                prod_page=_page(item.get("prod_page")),
                stage_page=_page(item.get("stage_page"))))
        if item.get("toc_status") != "Missing in Stage":
            continue
        out.append(Row(kind="Content missing",
                       text=str(item.get("title") or "")[:400],
                       detail=(f"Topic present in PROD at outline level "
                               f"L{item.get('prod_level')}, absent from STAGE"
                               if isinstance(item.get("prod_level"), int)
                               else "Topic present in PROD, absent from STAGE")[:600],
                       severity="high",
                       prod_page=_page(item.get("prod_page")),
                       stage_page=_page(item.get("stage_page"))))
    return out


# ── renderer ─────────────────────────────────────────────────────────────────
def _e(t):
    return html.escape(re.sub(r"\s+", " ", str(t or "")).strip())


def _uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def render(rows: list, out_path: str, meta: dict) -> str:
    from .compare_report import _CSS          # one look across both reports

    rows = sorted(rows, key=lambda r: (SEV_ORDER.get(r.severity, 3),
                                       r.prod_page or r.stage_page))
    counts = {"total": len(rows), "high": 0, "medium": 0, "low": 0}
    for r in rows:
        counts[r.severity] = counts.get(r.severity, 0) + 1

    b = ["<title>%s Validation Report</title>" % _e(meta.get("name", "PDF")),
         '<meta charset="utf-8">',
         '<link rel="preconnect" href="https://fonts.googleapis.com">',
         '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
         'family=Archivo:wght@500;600;700&family=Source+Serif+4:opsz,wght@8..60,400;'
         '8..60,600&family=IBM+Plex+Mono:wght@400;500&display=swap">',
         "<style>%s</style>" % _CSS,
         '<div class="wrap"><header>',
         '<div class="kicker">%s</div>' % _e(meta.get("mode_label", "Validation")),
         "<h1>%s</h1>" % _e(meta.get("title", "Validation Report")),
         '<div class="facts"><span>PROD <b>%s</b></span><span>STAGE <b>%s</b></span>'
         '<span>Mode <b>%s</b></span><span>Run <b>%s</b></span></div></header>'
         % (_e(meta.get("prod", "")), _e(meta.get("stage", "")),
            _e(meta.get("mode", "")), _e(meta.get("run", "")))]

    if rows:
        b.append('<div class="tiles">')
        for k, lbl, cls in (("total", "Issues", ""), ("high", "High", "high"),
                            ("medium", "Medium", "med"), ("low", "Low", "")):
            b.append('<div class="tile %s"><span class="v">%d</span>'
                     '<span class="k">%s</span></div>' % (cls, counts[k], lbl))
        b.append("</div>")
    else:
        b.append('<div class="none"><b>No issues found.</b> This validation '
                 'reported nothing to fix.</div>')

    for n, r in enumerate(rows, 1):
        rid = "R%d" % n
        b.append('<article class="diff" id="%s"><div class="dhead">'
                 '<span class="did %s">%s</span><h3>%s</h3>'
                 '<span class="sev %s">%s</span></div>'
                 % (rid, r.severity, rid, _e(r.kind), r.severity, r.severity))
        if r.stage_text:
            b.append('<blockquote><b>PROD</b> %s<br><b>STAGE</b> %s</blockquote>'
                     % (_e(r.text[:400]), _e(r.stage_text[:400])))
        elif r.text:
            b.append("<blockquote>%s</blockquote>" % _e(r.text[:400]))
        if r.detail:
            b.append('<p class="det">%s</p>' % _e(r.detail))
        b.append("<dl><dt>PROD</dt><dd>%s</dd><dt>STAGE</dt><dd>%s</dd></dl>"
                 % ("page %d" % r.prod_page if r.prod_page else "not located",
                    "page %d" % r.stage_page if r.stage_page else "not located"))
        if r.prod_png or r.stage_png:
            b.append('<div class="pair">')
            for tag, png, page in (("PROD", r.prod_png, r.prod_page),
                                   ("STAGE", r.stage_png, r.stage_page)):
                cls = "shot stage" if tag == "STAGE" else "shot"
                b.append('<div class="%s"><div class="cap"><span>%s%s</span>'
                         '<span>%s</span></div>'
                         % (cls, tag, " p%d" % page if page else "",
                            "difference boxed" if png else "nothing to show"))
                b.append('<img src="%s" alt="%s page %d">' % (_uri(png), tag, page)
                         if png else
                         '<div class="absent">No counterpart in this document.</div>')
                b.append("</div>")
            b.append("</div>")
        b.append("</article>")

    b.append('<footer>%s &rarr; %s &middot; %s<br>Companion PDF: %s</footer></div>'
             % (_e(meta.get("prod", "")), _e(meta.get("stage", "")),
                _e(meta.get("mode_label", "")), _e(meta.get("pdf_name", ""))))

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(b))
    return out_path

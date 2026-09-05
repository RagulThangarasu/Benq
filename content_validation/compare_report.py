"""Both reports, written from one comparison.

The HTML and the PDF are not two formats of a summary - they are two renderings
of the same list of differences, each carrying the same evidence: the actual page
from each document, cropped to the difference and boxed in red. Whatever one
shows, the other shows.
"""
from __future__ import annotations

import base64
import html
import io
import os
import re

import fitz
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                TableStyle, PageBreak, Image as RLImage,
                                KeepTogether)
from reportlab.platypus.flowables import HRFlowable

from .pdf_compare import SHOT_DPI, SHOT_PAD, BOX_RGB

INK   = colors.HexColor("#1B1720")
MUTED = colors.HexColor("#6B6478")
RULE  = colors.HexColor("#E2DEE8")
ACC   = colors.HexColor("#6B3FA0")
BAD   = colors.HexColor("#A82015")
WARN  = colors.HexColor("#8A5A00")
SEV   = {"high": BAD, "medium": WARN, "low": MUTED}
MAX_SHOT_PT = 190.0     # tallest evidence crop, in points, in the PDF

# Helvetica has no Cyrillic, Greek, Hebrew, Arabic or CJK. A report about a
# localised manual set in it prints the findings as black boxes - the one thing
# a report may never do is make the evidence unreadable. Register a font that
# covers the scripts these manuals actually ship in, and fall back to Helvetica
# only when the machine has none.
_UNICODE_FONT_CANDIDATES = (
    ("ArialUnicode", "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"),
    ("ArialUnicode", "/Library/Fonts/Arial Unicode.ttf"),
    ("DejaVuSans", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    ("NotoSans", "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"),
    ("NotoSans", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
)


def _register_unicode_font():
    """(regular, bold) font names that can print the scripts in these manuals."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for name, path in _UNICODE_FONT_CANDIDATES:
        if not os.path.isfile(path):
            continue
        try:
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name, path))
            # These faces ship no separate bold; reuse the regular so a bold
            # run stays legible rather than falling back to a Latin-only face.
            bold = name + "-Bold"
            if bold not in pdfmetrics.getRegisteredFontNames():
                try:
                    pdfmetrics.registerFont(TTFont(bold, path))
                except Exception:
                    bold = name
            from reportlab.pdfbase.pdfmetrics import registerFontFamily
            registerFontFamily(name, normal=name, bold=bold,
                               italic=name, boldItalic=bold)
            return name, bold
        except Exception:
            continue
    return "Helvetica", "Helvetica-Bold"


FONT, FONT_BOLD = _register_unicode_font()


# ── evidence crops ───────────────────────────────────────────────────────────
def crop(side, page_no: int, rects, dpi: int = SHOT_DPI) -> bytes:
    """PNG of a page region with every rect boxed in red.

    The box is drawn on a scratch copy so the source PDF is never touched.
    """
    if not page_no or page_no < 1 or page_no > side.doc.page_count:
        return b""
    src = side.doc[page_no - 1]
    scratch = fitz.open()
    scratch.insert_pdf(side.doc, from_page=page_no - 1, to_page=page_no - 1)
    page = scratch[0]

    if rects:
        area = fitz.Rect(rects[0])
        for r in rects[1:]:
            area |= fitz.Rect(r)
        for r in rects:
            page.draw_rect(fitz.Rect(r) + (-1.5, -1.5, 1.5, 1.5),
                           color=BOX_RGB, width=1.1)
        clip = fitz.Rect(area) + (-SHOT_PAD, -SHOT_PAD, SHOT_PAD, SHOT_PAD)
        clip &= page.rect
    else:
        clip = page.rect

    if clip.width < 60 or clip.height < 30:
        clip = fitz.Rect(clip) + (-40, -20, 40, 20)
        clip &= page.rect
    data = page.get_pixmap(clip=clip, dpi=dpi).tobytes("png")
    scratch.close()
    return data


def evidence(diffs, prod, stage) -> dict:
    """{diff id: (prod png, stage png)} - rendered once, used by both reports."""
    shots = {}
    for d in diffs:
        p = crop(prod, d.prod_page, d.prod_rects) if d.prod_page else b""
        s = crop(stage, d.stage_page, d.stage_rects) if d.stage_page else b""
        shots[d.did] = (p, s)
    return shots


def _counts(diffs) -> dict:
    c = {"total": len(diffs), "high": 0, "medium": 0, "low": 0,
         "image": 0, "text": 0}
    for d in diffs:
        c[d.severity] = c.get(d.severity, 0) + 1
        c[d.lane] += 1
    return c


# ── HTML ─────────────────────────────────────────────────────────────────────
_CSS = """
:root{--paper:#FBFAFC;--panel:#fff;--ink:#1B1720;--muted:#6B6478;--rule:#E2DEE8;
 --rule-soft:#EFECF3;--accent:#6B3FA0;--accent-soft:#F1EBF8;--bad:#A82015;
 --bad-soft:#FBEDEB;--warn:#8A5A00;--warn-soft:#FBF1DE;--ok:#2C6650;
 --ok-soft:#E9F2EE;--shot:#fff}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
 --paper:#151219;--panel:#1D1922;--ink:#EDE9F3;--muted:#A49CB2;--rule:#332C3D;
 --rule-soft:#272130;--accent:#C4A2ED;--accent-soft:#2A2136;--bad:#F09A8E;
 --bad-soft:#33201D;--warn:#E6BC72;--warn-soft:#2E2415;--ok:#8CCBAF;
 --ok-soft:#1B2B24;--shot:#F4F2F6}}
:root[data-theme="dark"]{--paper:#151219;--panel:#1D1922;--ink:#EDE9F3;
 --muted:#A49CB2;--rule:#332C3D;--rule-soft:#272130;--accent:#C4A2ED;
 --accent-soft:#2A2136;--bad:#F09A8E;--bad-soft:#33201D;--warn:#E6BC72;
 --warn-soft:#2E2415;--ok:#8CCBAF;--ok-soft:#1B2B24;--shot:#F4F2F6}
*{box-sizing:border-box}
/* The evidence is quoted verbatim, in whatever script the manual is written in.
   The fallbacks after the display faces are what actually render Cyrillic,
   Greek, Hebrew, Arabic and CJK; without them the quotes come out as boxes. */
body{margin:0;background:var(--paper);color:var(--ink);font-size:16px;
 line-height:1.6;font-family:"Source Serif 4",Georgia,"Noto Serif CJK SC",
 "Hiragino Mincho ProN","Songti SC","Microsoft YaHei","Noto Sans Hebrew",
 "Noto Sans Arabic",serif}
.wrap{max-width:1000px;margin:0 auto;padding:0 24px 90px}
h1,h2,h3,.ui,th,.tag,dt,.cap{font-family:"Archivo",-apple-system,"Segoe UI",
 "Noto Sans CJK SC","Hiragino Sans","PingFang SC","Microsoft YaHei",
 Helvetica,sans-serif}
code,.mono,.num{font-family:"IBM Plex Mono",ui-monospace,Menlo,
 "Noto Sans Mono CJK SC",monospace}
header{border-bottom:2px solid var(--ink);padding:52px 0 20px;display:flex;
 flex-direction:column;gap:12px}
.kicker{font-size:11px;font-weight:600;letter-spacing:.16em;text-transform:uppercase;
 color:var(--accent);font-family:"Archivo",sans-serif}
h1{margin:0;font-size:clamp(28px,4.6vw,42px);font-weight:700;letter-spacing:-.02em;
 line-height:1.07;text-wrap:balance}
.facts{display:flex;flex-wrap:wrap;gap:0 32px;font-family:"IBM Plex Mono",monospace;
 font-size:12px;color:var(--muted)}
.facts b{color:var(--ink);font-weight:500}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1px;
 background:var(--rule);border:1px solid var(--rule);border-radius:3px;
 overflow:hidden;margin-top:30px}
.tile{background:var(--panel);padding:16px 18px;display:flex;flex-direction:column;gap:3px}
.tile .v{font-family:"Archivo",sans-serif;font-size:28px;font-weight:700;
 letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.tile .k{font-size:10.5px;font-weight:600;letter-spacing:.11em;text-transform:uppercase;
 color:var(--muted);font-family:"Archivo",sans-serif}
.tile.high .v{color:var(--bad)} .tile.med .v{color:var(--warn)}
.none{margin-top:30px;border:1px solid var(--rule);border-left:4px solid var(--ok);
 background:var(--ok-soft);padding:20px 22px;border-radius:3px}
h2.sec{font-size:13px;font-weight:600;letter-spacing:.14em;text-transform:uppercase;
 color:var(--muted);margin:52px 0 0;padding-bottom:8px;border-bottom:1px solid var(--rule)}
.lede{max-width:70ch;color:var(--muted);margin:16px 0 0}
.diff{margin-top:36px;border-top:1px solid var(--rule);padding-top:22px}
.dhead{display:flex;align-items:baseline;gap:13px;flex-wrap:wrap}
.did{font-family:"IBM Plex Mono",monospace;font-size:12.5px;color:var(--paper);
 background:var(--bad);padding:3px 8px;border-radius:2px}
.did.medium{background:var(--warn)} .did.low{background:var(--muted)}
.diff h3{margin:0;font-size:19px;font-weight:600;letter-spacing:-.015em;flex:1 1 240px}
.sev{font-size:10.5px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;
 font-family:"Archivo",sans-serif}
.sev.high{color:var(--bad)} .sev.medium{color:var(--warn)} .sev.low{color:var(--muted)}
blockquote{unicode-bidi:plaintext;text-align:start;margin:16px 0 0;padding:12px 16px;border-left:3px solid var(--accent);
 background:var(--accent-soft);border-radius:0 3px 3px 0;font-size:15px}
.diff p.det{max-width:70ch;margin:14px 0 0;color:var(--muted);font-size:15px}
dl{display:grid;grid-template-columns:auto 1fr;gap:5px 16px;margin:16px 0 0;font-size:13.5px}
dt{font-size:10.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
 color:var(--muted);padding-top:2px}
dd{margin:0}
.pair{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:18px}
@media (max-width:720px){.pair{grid-template-columns:1fr}}
.shot{border:1px solid var(--rule);border-radius:3px;overflow:hidden;background:var(--panel);
 display:flex;flex-direction:column}
.cap{font-size:10.5px;font-weight:600;letter-spacing:.1em;text-transform:uppercase;
 padding:8px 11px;border-bottom:1px solid var(--rule);color:var(--muted);
 display:flex;justify-content:space-between;gap:8px}
.shot.stage .cap{color:var(--bad)}
.shot img{display:block;width:100%;height:auto;background:var(--shot)}
.absent{flex:1;display:flex;align-items:center;justify-content:center;text-align:center;
 padding:40px 18px;color:var(--muted);font-size:14px;
 background:repeating-linear-gradient(135deg,transparent 0 9px,var(--rule-soft) 9px 10px)}
footer{margin-top:60px;padding-top:18px;border-top:2px solid var(--ink);
 font-family:"IBM Plex Mono",monospace;font-size:12px;color:var(--muted);line-height:1.9}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
"""


def _e(t):
    return html.escape(re.sub(r"\s+", " ", t or "").strip())


def _uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def write_html(diffs, prod, stage, shots, out_path: str, meta: dict) -> str:
    c = _counts(diffs)
    P, S = os.path.basename(prod.path), os.path.basename(stage.path)
    b = []
    b.append('<title>%s Content Difference Report</title>' % _e(meta.get("name", "PDF")))
    b.append('<meta charset="utf-8">')
    b.append('<link rel="preconnect" href="https://fonts.googleapis.com">')
    b.append('<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
             'family=Archivo:wght@500;600;700&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600'
             '&family=IBM+Plex+Mono:wght@400;500&display=swap">')
    b.append("<style>%s</style>" % _CSS)
    b.append('<div class="wrap"><header>')
    b.append('<div class="kicker">PDF to PDF comparison</div>')
    b.append("<h1>%s</h1>" % _e(meta.get("title", "Content Difference Report")))
    b.append('<div class="facts"><span>PROD <b>%s &middot; %d pp</b></span>'
             '<span>STAGE <b>%s &middot; %d pp</b></span>'
             '<span>Pages matched <b>%d</b></span><span>Run <b>%s</b></span></div>'
             % (_e(P), prod.doc.page_count, _e(S), stage.doc.page_count,
                meta.get("matched", 0), _e(meta.get("run", ""))))
    b.append("</header>")

    if diffs:
        b.append('<div class="tiles">')
        for k, lbl, cls in (("total", "Differences", ""), ("high", "High", "high"),
                            ("medium", "Medium", "med"), ("low", "Low", ""),
                            ("image", "Found in artwork", ""),
                            ("text", "Found in text", "")):
            b.append('<div class="tile %s"><span class="v">%d</span>'
                     '<span class="k">%s</span></div>' % (cls, c[k], lbl))
        b.append("</div>")
    else:
        b.append('<div class="none"><b>No differences found.</b> Every line of PROD '
                 'reads somewhere in STAGE and every line of STAGE reads somewhere '
                 'in PROD, in the text layer or inside the artwork.</div>')

    b.append('<h2 class="sec">How the comparison was made</h2>')
    b.append('<p class="lede">Two passes that do not share a blind spot. The text pass '
             'reads the embedded text layer of both files. The image pass renders every '
             'artwork region at %d&nbsp;dpi and reads it optically, so lettering baked '
             'into a bitmap is compared on equal terms with lettering drawn as text. '
             'Wording is matched against the whole of the other document, so moving a '
             'paragraph is not a difference &mdash; only losing it is.</p>' % 400)

    for d in diffs:
        pp, sp = shots.get(d.did, (b"", b""))
        b.append('<article class="diff" id="%s"><div class="dhead">'
                 '<span class="did %s">%s</span><h3>%s</h3>'
                 '<span class="sev %s">%s</span></div>'
                 % (d.did, d.severity, d.did, _e(d.kind), d.severity, d.severity))
        if getattr(d, "stage_text", ""):
            b.append('<blockquote><b>PROD</b> %s<br><b>STAGE</b> %s</blockquote>'
                     % (_e(d.text[:400]), _e(d.stage_text[:400])))
        else:
            b.append("<blockquote>%s</blockquote>" % _e(d.text[:400]))
        b.append('<p class="det">%s</p>' % _e(d.detail))
        b.append("<dl><dt>PROD</dt><dd>%s</dd><dt>STAGE</dt><dd>%s</dd>"
                 "<dt>Found by</dt><dd>%s</dd></dl>"
                 % ("page %d" % d.prod_page if d.prod_page else "no counterpart",
                    "page %d" % d.stage_page if d.stage_page else "no counterpart",
                    "optical read of the artwork" if d.lane == "image"
                    else "text layer of both documents"))
        b.append('<div class="pair">')
        for tag, png, page in (("PROD", pp, d.prod_page), ("STAGE", sp, d.stage_page)):
            cls = "shot stage" if tag == "STAGE" else "shot"
            b.append('<div class="%s"><div class="cap"><span>%s%s</span>'
                     '<span>%s</span></div>'
                     % (cls, tag, " p%d" % page if page else "",
                        "difference boxed" if png else "nothing to show"))
            if png:
                b.append('<img src="%s" alt="%s page %d with the difference boxed">'
                         % (_uri(png), tag, page))
            else:
                b.append('<div class="absent">No counterpart in this document.</div>')
            b.append("</div>")
        b.append("</div></article>")

    b.append('<footer>%s &rarr; %s<br>Text layer: PyMuPDF &middot; '
             'Artwork: Tesseract OCR at %d dpi &middot; page mapping by token-set '
             'similarity across all %d &times; %d page pairs<br>'
             'Companion PDF: %s</footer></div>'
             % (_e(P), _e(S), 400, prod.doc.page_count, stage.doc.page_count,
                _e(meta.get("pdf_name", ""))))

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(b))
    return out_path


# ── PDF ──────────────────────────────────────────────────────────────────────
def _styles():
    base = ParagraphStyle("body", fontName=FONT, fontSize=9.4, leading=13.4,
                          textColor=INK)
    return {
        "h1": ParagraphStyle("h1", base, fontName=FONT_BOLD, fontSize=21,
                             leading=24, spaceAfter=4),
        "kick": ParagraphStyle("kick", base, fontName=FONT_BOLD, fontSize=7.5,
                               textColor=ACC, spaceAfter=5),
        "sec": ParagraphStyle("sec", base, fontName=FONT_BOLD, fontSize=8,
                              textColor=MUTED, spaceBefore=14, spaceAfter=6),
        "body": base,
        "muted": ParagraphStyle("muted", base, textColor=MUTED),
        "dh": ParagraphStyle("dh", base, fontName=FONT_BOLD, fontSize=12.5,
                             leading=15),
        "quote": ParagraphStyle("quote", base, fontName=FONT,
                                fontSize=9.6, leading=13.6, leftIndent=9,
                                borderPadding=0),
        "cap": ParagraphStyle("cap", base, fontName=FONT_BOLD, fontSize=7,
                              textColor=MUTED, spaceBefore=3),
    }


def _rl_image(png: bytes, max_w: float):
    if not png:
        return Paragraph("No counterpart in this document.", _styles()["muted"])
    buf = io.BytesIO(png)
    img = RLImage(buf)
    w, h = img.imageWidth, img.imageHeight
    scale = min(max_w / w, MAX_SHOT_PT / h, 1.0)
    img.drawWidth, img.drawHeight = w * scale, h * scale
    img.hAlign = "LEFT"
    return img


def write_pdf(diffs, prod, stage, shots, out_path: str, meta: dict) -> str:
    st = _styles()
    c = _counts(diffs)
    P, S = os.path.basename(prod.path), os.path.basename(stage.path)
    doc = SimpleDocTemplate(out_path, pagesize=letter,
                            leftMargin=48, rightMargin=48,
                            topMargin=46, bottomMargin=42,
                            title=meta.get("title", "Content Difference Report"))
    avail = letter[0] - 96
    half = (avail - 12) / 2.0
    F = []

    F.append(Paragraph("PDF TO PDF COMPARISON", st["kick"]))
    F.append(Paragraph(meta.get("title", "Content Difference Report"), st["h1"]))
    F.append(Spacer(1, 4))
    F.append(HRFlowable(width="100%", thickness=1.2, color=INK, spaceAfter=10))
    F.append(Table(
        [["PROD", "%s  ·  %d pp" % (P, prod.doc.page_count),
          "STAGE", "%s  ·  %d pp" % (S, stage.doc.page_count)],
         ["Pages matched", str(meta.get("matched", 0)), "Run", meta.get("run", "")]],
        colWidths=[70, avail / 2 - 70, 46, avail / 2 - 46],
        style=TableStyle([
            ("FONT", (0, 0), (-1, -1), FONT, 8),
            ("FONT", (0, 0), (0, -1), FONT_BOLD, 7),
            ("FONT", (2, 0), (2, -1), FONT_BOLD, 7),
            ("TEXTCOLOR", (0, 0), (0, -1), MUTED),
            ("TEXTCOLOR", (2, 0), (2, -1), MUTED),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
        ])))
    F.append(Spacer(1, 14))

    if diffs:
        cells = [["DIFFERENCES", "HIGH", "MEDIUM", "LOW", "IN ARTWORK", "IN TEXT"],
                 [c["total"], c["high"], c["medium"], c["low"], c["image"], c["text"]]]
        F.append(Table(cells, colWidths=[avail / 6] * 6, style=TableStyle([
            ("FONT", (0, 0), (-1, 0), FONT_BOLD, 6.6),
            ("TEXTCOLOR", (0, 0), (-1, 0), MUTED),
            ("FONT", (0, 1), (-1, 1), FONT_BOLD, 19),
            ("TEXTCOLOR", (1, 1), (1, 1), BAD),
            ("TEXTCOLOR", (2, 1), (2, 1), WARN),
            ("BOX", (0, 0), (-1, -1), 0.6, RULE),
            ("INNERGRID", (0, 0), (-1, -1), 0.6, RULE),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, 0), 7),
            ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
        ])))
    else:
        F.append(Paragraph(
            "<b>No differences found.</b> Every line of PROD reads somewhere in "
            "STAGE and every line of STAGE reads somewhere in PROD, in the text "
            "layer or inside the artwork.", st["body"]))

    F.append(Paragraph("HOW THE COMPARISON WAS MADE", st["sec"]))
    F.append(Paragraph(
        "Two passes that do not share a blind spot. The text pass reads the "
        "embedded text layer of both files. The image pass renders every artwork "
        "region at 400 dpi and reads it optically, so lettering baked into a "
        "bitmap is compared on equal terms with lettering drawn as text. Wording "
        "is matched against the whole of the other document, so moving a paragraph "
        "is not a difference &mdash; only losing it is.", st["muted"]))

    for d in diffs:
        pp, sp = shots.get(d.did, (b"", b""))
        blk = [
            HRFlowable(width="100%", thickness=0.6, color=RULE, spaceBefore=14,
                       spaceAfter=8),
            Table([[Paragraph('<font color="#FFFFFF"><b> %s </b></font>' % d.did,
                              st["body"]),
                    Paragraph("<b>%s</b>" % _e(d.kind), st["dh"]),
                    Paragraph('<font color="#%s"><b>%s</b></font>'
                              % (SEV[d.severity].hexval()[2:], d.severity.upper()),
                              st["cap"])]],
                  colWidths=[30, avail - 90, 60],
                  style=TableStyle([
                      ("BACKGROUND", (0, 0), (0, 0), SEV[d.severity]),
                      ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                      ("LEFTPADDING", (0, 0), (-1, -1), 4),
                      ("TOPPADDING", (0, 0), (-1, -1), 3),
                      ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                  ])),
            Spacer(1, 6),
            Paragraph(("<b>PROD</b> &ldquo;%s&rdquo;<br/><b>STAGE</b> &ldquo;%s&rdquo;"
                       % (_e(d.text[:400]), _e(d.stage_text[:400])))
                      if getattr(d, "stage_text", "")
                      else "&ldquo;%s&rdquo;" % _e(d.text[:400]), st["quote"]),
            Spacer(1, 5),
            Paragraph(_e(d.detail), st["muted"]),
            Spacer(1, 5),
            Paragraph("PROD %s &nbsp;&middot;&nbsp; STAGE %s &nbsp;&middot;&nbsp; "
                      "found by %s"
                      % ("page %d" % d.prod_page if d.prod_page else "no counterpart",
                         "page %d" % d.stage_page if d.stage_page else "no counterpart",
                         "optical read of the artwork" if d.lane == "image"
                         else "text layer of both documents"), st["cap"]),
            Spacer(1, 7),
            Table([[Paragraph("PROD%s" % (" p%d" % d.prod_page if d.prod_page else ""),
                              st["cap"]),
                    Paragraph("STAGE%s" % (" p%d" % d.stage_page if d.stage_page else ""),
                              st["cap"])],
                   [_rl_image(pp, half), _rl_image(sp, half)]],
                  colWidths=[half, half],
                  style=TableStyle([
                      ("VALIGN", (0, 0), (-1, -1), "TOP"),
                      ("BOX", (0, 0), (0, -1), 0.6, RULE),
                      ("BOX", (1, 0), (1, -1), 0.6, RULE),
                      ("LINEBELOW", (0, 0), (-1, 0), 0.6, RULE),
                      ("LEFTPADDING", (0, 0), (-1, -1), 5),
                      ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                      ("TOPPADDING", (0, 0), (-1, -1), 4),
                      ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                  ])),
        ]
        # the heading and its wording must not separate from each other; the
        # crops may fall to the next page if they have to.
        F.append(KeepTogether(blk[:6]))
        F.extend(blk[6:])

    def _chrome(canvas, _doc):
        canvas.saveState()
        canvas.setFont(FONT, 7)
        canvas.setFillColor(MUTED)
        canvas.drawString(48, 26, "%s  vs  %s" % (P, S))
        canvas.drawRightString(letter[0] - 48, 26, "Page %d" % canvas.getPageNumber())
        canvas.restoreState()

    doc.build(F, onFirstPage=_chrome, onLaterPages=_chrome)
    return out_path


# ── one call, both files ─────────────────────────────────────────────────────
def build(diffs, prod, stage, out_dir: str, stem: str, meta: dict) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    shots = evidence(diffs, prod, stage)
    html_path = os.path.join(out_dir, "%s_compare.html" % stem)
    pdf_path = os.path.join(out_dir, "%s_compare.pdf" % stem)
    meta = dict(meta)
    meta["pdf_name"] = os.path.basename(pdf_path)
    write_html(diffs, prod, stage, shots, html_path, meta)
    write_pdf(diffs, prod, stage, shots, pdf_path, meta)
    return {"html": html_path, "pdf": pdf_path, "counts": _counts(diffs)}

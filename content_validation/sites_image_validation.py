"""Validate a published AEM site against its source PDF — images & layout only.

Unlike style_validation.py (which compares two PDFs), this renders the live AEM
pages in a headless browser (Playwright) and flags the things the CMS does *not*
get right on its own:

  • Typography spec   — rendered font sizes vs the BenQ A4 spec (reused from
                        style_validation._BENQ_TYPO_SPEC, converted pt → px)
  • Oversized image   — an image rendered wider than its content container
  • Image cut / break  — an image that overflows or is clipped by its container
  • Table breaking    — a table that overflows horizontally (needs h-scroll)
  • Image dimension    — site image sizes vs the matching PDF section's images

Findings use the same dict schema as style_validation so the existing report /
summary / UI render them unchanged:
  {category, severity, topic, pages, expected, actual, issue, fix}
"""
from __future__ import annotations

import fitz  # PyMuPDF

from content_validation import validate_toc_content as vtc
from content_validation.style_validation import _BENQ_TYPO_SPEC

# 1pt = 1/72 in, CSS px = 1/96 in → px = pt * 96/72
_PT_TO_PX = 96.0 / 72.0

# Categories this validator can emit (drives report / summary ordering).
SITES_IMAGE_CATEGORY_ORDER = [
    "Typography spec",
    "Line height",
    "Image dimension",
    "Image padding",
    "Space above image",
    "Oversized image",
    "Image cut off",
    "Table breaking",
    "Image alignment",
    "Content alignment",
]

# Acceptable line-height ratio (line-height ÷ font-size). Below the floor the
# text is cramped (lines touch); above the ceiling it is unusually airy.
_LH_MIN = 1.15
_LH_MAX = 2.2

# BenQ manual body / bullets / headings read left-to-right and are left-aligned.
# These alignments on a text block are flagged as wrong for a manual.
_BAD_TEXT_ALIGN = {"right", "justify"}


def _f(cat, sev, topic, pages, expected, actual, issue, fix):
    return {"category": cat, "severity": sev, "topic": topic, "pages": str(pages),
            "expected": expected, "actual": actual, "issue": issue, "fix": fix}


# ── In-browser extraction ────────────────────────────────────────────────────
# Runs inside the rendered page and returns a JSON-able snapshot of every image,
# table and the representative typography. Kept dependency-free (pure DOM).
_PAGE_JS = r"""
() => {
  const main =
    document.querySelector('main, article, [role=main], .content, #content, .cmp-text') ||
    document.body;
  const mr = main.getBoundingClientRect();
  const docW = document.documentElement.clientWidth;

    const px = (v) => {
        const n = parseFloat(v);
        return Number.isFinite(n) ? n : 0;
    };

  const clippedBy = (el) => {
    // True if any ancestor hides overflow and the element pokes outside it.
    let p = el.parentElement;
    const r = el.getBoundingClientRect();
    while (p && p !== document.body) {
      const cs = getComputedStyle(p);
      if (/(hidden|clip)/.test(cs.overflowX + cs.overflowY)) {
        const pr = p.getBoundingClientRect();
        if (r.right > pr.right + 1 || r.left < pr.left - 1 ||
            r.bottom > pr.bottom + 1 || r.top < pr.top - 1) return true;
      }
      p = p.parentElement;
    }
    return false;
  };

  // line-height in px (resolve the CSS keyword 'normal' to ~1.2·font-size)
  const lhPx = (cs) => {
    const fs = parseFloat(cs.fontSize) || 0;
    return cs.lineHeight === 'normal' ? fs * 1.2 : (parseFloat(cs.lineHeight) || 0);
  };

  const imgAlign = (img, r, cs) => {
    if (cs.cssFloat === 'left' || cs.cssFloat === 'right') return cs.cssFloat;
    if (cs.marginLeft === 'auto' && cs.marginRight === 'auto') return 'center';
    const parent = img.parentElement;
    if (parent && /center/.test(getComputedStyle(parent).textAlign)) return 'center';
    const leftGap = r.left - mr.left, rightGap = mr.right - r.right;
    if (Math.abs(leftGap - rightGap) < 12) return 'center';
    if (leftGap <= 10) return 'left';
    if (rightGap <= 10) return 'right';
    return 'offset';
  };

    // Nearest text block above an image (with horizontal overlap) gives
    // practical "space above image" in rendered px.
    const textRects = [...main.querySelectorAll('p, li, h1, h2, h3')]
        .filter(e => (e.textContent || '').trim().length > 15)
        .map(e => e.getBoundingClientRect());

    const gapAboveImage = (r) => {
        let best = null;
        for (const tr of textRects) {
            if (tr.bottom > r.top + 1) continue;
            const overlap = Math.max(0, Math.min(r.right, tr.right) - Math.max(r.left, tr.left));
            if (overlap < Math.min(80, r.width * 0.35)) continue;
            const gap = r.top - tr.bottom;
            if (best === null || gap < best) best = gap;
        }
        return best === null ? null : Math.round(best);
    };

  const images = [...main.querySelectorAll('img')].map(img => {
    const r = img.getBoundingClientRect();
    const cs = getComputedStyle(img);
        const leftGap = Math.max(0, r.left - mr.left);
        const rightGap = Math.max(0, mr.right - r.right);
    return {
      alt: img.getAttribute('alt') || '',
      src: (img.currentSrc || img.src || '').split('/').pop(),
      w: Math.round(r.width), h: Math.round(r.height),
      naturalW: img.naturalWidth || 0, naturalH: img.naturalHeight || 0,
      left: Math.round(r.left - mr.left),
      right: Math.round(r.right - mr.left),
            gapLeft: Math.round(leftGap),
            gapRight: Math.round(rightGap),
            gapAbove: gapAboveImage(r),
      overflowsRight: r.right > mr.right + 2 || r.right > docW + 2,
      overflowsLeft: r.left < mr.left - 2,
      widerThanMain: r.width > mr.width + 2,
      clipped: clippedBy(img),
      display: cs.display, float: cs.cssFloat,
      marginAuto: cs.marginLeft === 'auto' && cs.marginRight === 'auto',
            marginTop: Math.round(px(cs.marginTop)),
            marginBottom: Math.round(px(cs.marginBottom)),
            marginLeft: Math.round(px(cs.marginLeft)),
            marginRight: Math.round(px(cs.marginRight)),
      align: imgAlign(img, r, cs),
    };
  }).filter(im => im.w >= 16 && im.h >= 16);

  const tables = [...main.querySelectorAll('table')].map(t => {
    const r = t.getBoundingClientRect();
    return {
      scrollW: t.scrollWidth, clientW: t.clientWidth,
      w: Math.round(r.width), mainW: Math.round(mr.width),
      overflows: t.scrollWidth > t.clientWidth + 2 || r.width > mr.width + 2,
    };
  });

  // Representative typography (size + line-height) for headings, body, bullets.
  const typo = {};
  for (const tag of ['h1', 'h2', 'h3']) {
    const el = main.querySelector(tag);
    if (el) {
      const cs = getComputedStyle(el);
      typo[tag] = { px: parseFloat(cs.fontSize), lh: lhPx(cs), weight: cs.fontWeight,
                    align: cs.textAlign,
                    color: cs.color, text: (el.textContent || '').trim().slice(0, 60) };
    }
  }
  const ps = [...main.querySelectorAll('p')].filter(e => (e.textContent || '').trim().length > 30);
  if (ps.length) {
    const cs = getComputedStyle(ps[0]);
    typo['body'] = { px: parseFloat(cs.fontSize), lh: lhPx(cs), weight: cs.fontWeight,
                     align: cs.textAlign, color: cs.color };
  }
  const lis = [...main.querySelectorAll('li')].filter(e => (e.textContent || '').trim().length > 15);
  if (lis.length) {
    const cs = getComputedStyle(lis[0]);
    typo['bullet'] = { px: parseFloat(cs.fontSize), lh: lhPx(cs), weight: cs.fontWeight,
                       align: cs.textAlign, count: lis.length,
                       text: (lis[0].textContent || '').trim().slice(0, 60) };
  }

  // Text-alignment census of body blocks (paragraphs + bullets + headings).
  const blocks = [...main.querySelectorAll('p, li, h1, h2, h3')]
    .filter(e => (e.textContent || '').trim().length > 15);
  const alignCount = {};
  let firstBad = null;
  for (const e of blocks) {
    const a = getComputedStyle(e).textAlign || 'start';
    alignCount[a] = (alignCount[a] || 0) + 1;
    if (!firstBad && (a === 'right' || a === 'justify'))
      firstBad = { align: a, text: (e.textContent || '').trim().slice(0, 60) };
  }

  return { mainW: Math.round(mr.width), images, tables, typo,
           hasImages: images.length > 0, alignCount, firstBad };
}
"""


def render_pages(pages, auth_token, progress_cb=None, viewport_w=1280):
    """Render each AEM page in headless Chromium and return DOM snapshots.

    pages       — [{title, url, path}] (from _aem_crawl_pages)
    auth_token  — base64 'user:pass' for the AEM Basic-auth header (or None)
    """
    from playwright.sync_api import sync_playwright

    headers = {"Authorization": "Basic " + auth_token} if auth_token else {}
    out = []
    total = max(1, len(pages))
    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox"])
        ctx = browser.new_context(
            viewport={"width": viewport_w, "height": 1600},
            extra_http_headers=headers,
            ignore_https_errors=True,
        )
        page = ctx.new_page()
        for i, pg in enumerate(pages):
            if progress_cb:
                progress_cb(0.15 + 0.7 * i / total, f"rendering {i+1}/{total}: {pg.get('title','')}")
            rec = {"title": pg.get("title") or pg.get("path") or pg.get("url"),
                   "url": pg.get("url"), "error": None, "data": None}
            try:
                page.goto(pg["url"], wait_until="networkidle", timeout=30000)
                rec["data"] = page.evaluate(_PAGE_JS)
            except Exception as e:  # one bad page must not abort the run
                rec["error"] = str(e)
            out.append(rec)
        browser.close()
    return out


# ── PDF-side reference: image widths per TOC section ─────────────────────────
def _pdf_section_images(pdf_path):
    """Return {norm_title: [image_width_px, …]} for each TOC section's figures.

    Image rects are in PDF points; converted to CSS px so they compare directly
    with the rendered site dimensions.
    """
    doc = fitz.open(pdf_path)
    toc = doc.get_toc() or []
    # page (1-based) → [widths in px] for non-decorative images
    by_page = {}
    for pno in range(1, doc.page_count + 1):
        page = doc[pno - 1]
        widths = []
        for blk in page.get_image_info(xrefs=False):
            bbox = blk.get("bbox")
            if not bbox:
                continue
            w = (bbox[2] - bbox[0]) * _PT_TO_PX
            h = (bbox[3] - bbox[1]) * _PT_TO_PX
            if min(w, h) < 24 or max(w, h) / max(min(w, h), 1) > 8:
                continue  # skip icons / thin rules
            widths.append(round(w))
        by_page[pno] = widths
    doc.close()

    # Assign each TOC title the images on its page span.
    sec = {}
    entries = [(t, p) for _lvl, t, p in toc]
    for i, (title, pg) in enumerate(entries):
        end = entries[i + 1][1] if i + 1 < len(entries) else max(by_page) + 1
        widths = []
        for p in range(pg, max(pg, end)):
            widths += by_page.get(p, [])
        sec[vtc._norm_key(title)] = widths
    return sec


# ── checks ───────────────────────────────────────────────────────────────────
def _check_typography(rendered, findings):
    """Rendered heading/body font sizes vs the BenQ A4 spec (pt → px)."""
    spec_px = {
        "h1":   _BENQ_TYPO_SPEC["H1 heading"]["size"] * _PT_TO_PX,
        "h2":   _BENQ_TYPO_SPEC["H2 heading"]["size"] * _PT_TO_PX,
        "h3":   _BENQ_TYPO_SPEC["H3 heading"]["size"] * _PT_TO_PX,
        "body": _BENQ_TYPO_SPEC["Body text"]["size"] * _PT_TO_PX,
    }
    label = {"h1": "H1 heading", "h2": "H2 heading", "h3": "H3 heading", "body": "Body text"}
    tol_px = 3.0  # ~2.25pt — generous; browsers round and rem scales differ
    # report each level at most once (first offending page) to avoid a wall of rows
    seen = set()
    for rec in rendered:
        typo = (rec.get("data") or {}).get("typo") or {}
        for tag, exp in spec_px.items():
            if tag in seen:
                continue
            info = typo.get(tag)
            if not info or not info.get("px"):
                continue
            actual = info["px"]
            if abs(actual - exp) > tol_px:
                seen.add(tag)
                sev = "High" if abs(actual - exp) > 2 * tol_px else "Medium"
                findings.append(_f(
                    "Typography spec", sev, label[tag], rec["title"],
                    f"{exp:.0f}px ({_BENQ_TYPO_SPEC[label[tag]]['size']:.0f}pt)",
                    f"{actual:.0f}px",
                    f"{label[tag]} renders at {actual:.0f}px on the site — the BenQ A4 "
                    f"spec is {_BENQ_TYPO_SPEC[label[tag]]['size']:.0f}pt (~{exp:.0f}px).",
                    f"Adjust the AEM style so {label[tag]} matches the spec size.",
                ))


def _check_line_height(rendered, findings):
    """Line-height (ratio to font-size) for every heading level and bullets."""
    label = {"h1": "H1 heading", "h2": "H2 heading", "h3": "H3 heading",
             "body": "Body text", "bullet": "Bullet / list item"}
    seen = set()  # report each level at most once (first offending page)
    for rec in rendered:
        typo = (rec.get("data") or {}).get("typo") or {}
        for tag, name in label.items():
            if tag in seen:
                continue
            info = typo.get(tag)
            if not info or not info.get("px") or not info.get("lh"):
                continue
            ratio = info["lh"] / info["px"]
            if ratio < _LH_MIN or ratio > _LH_MAX:
                seen.add(tag)
                cramped = ratio < _LH_MIN
                findings.append(_f(
                    "Line height",
                    "Medium" if cramped else "Low",
                    name, rec["title"],
                    f"{_LH_MIN:.2f}–{_LH_MAX:.1f}× font size",
                    f"{ratio:.2f}× ({info['lh']:.0f}px / {info['px']:.0f}px)",
                    f"{name} line-height is {ratio:.2f}× its font size — "
                    + ("lines are cramped/touching." if cramped else "spacing is unusually large."),
                    f"Set {name} line-height to roughly 1.2–1.5× the font size.",
                ))


def _check_alignment(rendered, findings):
    """Text & image alignment — both for pages with images and text-only pages."""
    for rec in rendered:
        data = rec.get("data") or {}

        # (a) Body / bullet / heading text that isn't left-aligned (manuals are LTR-left).
        bad = data.get("firstBad")
        if bad:
            findings.append(_f(
                "Content alignment", "Medium", f"{bad['align'].title()}-aligned text",
                rec["title"], "Left-aligned",
                f"{bad['align']}-aligned",
                f"Text on “{rec['title']}” is {bad['align']}-aligned "
                f"(e.g. “{bad['text']}…”) — BenQ manual content is left-aligned.",
                "Set the text alignment to left.",
            ))


def _check_spacing_and_padding(rendered, findings):
    """Rendered image spacing/padding checks: horizontal padding + vertical gap."""
    for rec in rendered:
        data = rec.get("data") or {}
        imgs = data.get("images", [])
        if not imgs:
            continue

        pad_flagged = False
        gap_flagged = False

        for im in imgs:
            name = im.get("alt") or im.get("src") or "image"
            align = im.get("align")
            lg = im.get("gapLeft")
            rg = im.get("gapRight")
            ga = im.get("gapAbove")

            # Horizontal padding expectations by alignment.
            if not pad_flagged and align == "left" and lg is not None and lg > 24:
                pad_flagged = True
                findings.append(_f(
                    "Image padding", "Low", name, rec["title"],
                    "Left-aligned image starts near the content edge (<= 24px)",
                    f"left gap {lg}px (right gap {rg}px)",
                    f"Image “{name}” has extra left padding ({lg}px), so it does not align "
                    f"with the content column.",
                    "Reduce left margin/padding so the image aligns with the content edge.",
                ))
            elif not pad_flagged and align == "right" and rg is not None and rg > 24:
                pad_flagged = True
                findings.append(_f(
                    "Image padding", "Low", name, rec["title"],
                    "Right-aligned image ends near the content edge (<= 24px)",
                    f"right gap {rg}px (left gap {lg}px)",
                    f"Image “{name}” has extra right padding ({rg}px), so right alignment "
                    f"looks offset from the content edge.",
                    "Reduce right margin/padding so right alignment matches the content edge.",
                ))
            elif not pad_flagged and align == "center" and lg is not None and rg is not None and abs(lg - rg) > 24:
                pad_flagged = True
                findings.append(_f(
                    "Image padding", "Low", name, rec["title"],
                    "Centered image has similar left/right padding",
                    f"left {lg}px vs right {rg}px",
                    f"Image “{name}” is marked centered but has unbalanced side padding "
                    f"({lg}px vs {rg}px).",
                    "Balance left/right spacing or use consistent centering rules.",
                ))

            # Vertical spacing above images should not be too tight or too loose.
            if not gap_flagged and ga is not None and (ga < 8 or ga > 120):
                gap_flagged = True
                findings.append(_f(
                    "Space above image", "Low", name, rec["title"],
                    "~8px to 120px from nearby text above",
                    f"{ga}px above image",
                    f"Image “{name}” has unusual top spacing ({ga}px) from the text above.",
                    "Adjust top margin/padding above the image to keep spacing consistent.",
                ))

        # Page-level consistency: similar images should not have very uneven top spacing.
        gaps = sorted(g for g in (im.get("gapAbove") for im in imgs) if isinstance(g, (int, float)))
        if len(gaps) >= 2 and (gaps[-1] - gaps[0]) > 72:
            findings.append(_f(
                "Space above image", "Low", "Inconsistent image spacing", rec["title"],
                "Consistent top spacing across images on the same page",
                f"range {gaps[0]}px to {gaps[-1]}px",
                f"Images on “{rec['title']}” have inconsistent vertical spacing above them "
                f"({gaps[0]}–{gaps[-1]}px).",
                "Use a consistent top spacing rule for image blocks on this page.",
            ))

        # (b) Image alignment. Flag images that sit at an odd offset (neither
        #     flush-left to the column nor centred), and inconsistent alignment
        #     when a page mixes left/centre/right images.
        imgs = data.get("images", [])
        aligns = {im.get("align") for im in imgs if im.get("align")}
        for im in imgs:
            if im.get("align") == "offset":
                name = im.get("alt") or im.get("src") or "image"
                findings.append(_f(
                    "Image alignment", "Low", name, rec["title"],
                    "Flush-left to the text column, or centred",
                    f"offset {im.get('left')}px from the left",
                    f"Image “{name}” is neither aligned to the content's left edge "
                    f"nor centred ({im.get('left')}px in from the left).",
                    "Align the image to the text column (left) or centre it.",
                ))
        if len(imgs) >= 2 and {"left", "center", "right"} & aligns and len(aligns - {"offset"}) > 1:
            findings.append(_f(
                "Image alignment", "Low", "Mixed image alignment", rec["title"],
                "Consistent alignment for all figures",
                ", ".join(sorted(a for a in aligns if a)),
                f"“{rec['title']}” mixes image alignments "
                f"({', '.join(sorted(a for a in aligns if a))}) — figures look inconsistent.",
                "Use one consistent alignment for the page's figures.",
            ))


def _check_images(rendered, findings):
    """Oversized images, images cut off / overflowing their container."""
    for rec in rendered:
        data = rec.get("data") or {}
        for im in data.get("images", []):
            name = im.get("alt") or im.get("src") or "image"
            if im.get("overflowsRight") or im.get("overflowsLeft") or im.get("clipped"):
                findings.append(_f(
                    "Image cut off", "High", name, rec["title"],
                    "Fully inside the content area",
                    f"{im['w']}×{im['h']}px — extends beyond / clipped by its container",
                    f"Image “{name}” overflows or is clipped on the rendered page "
                    f"(content width {data.get('mainW','?')}px). It is cut off.",
                    "Constrain the image to its container (max-width:100%) so it is not cut off.",
                ))
            elif im.get("widerThanMain"):
                findings.append(_f(
                    "Oversized image", "Medium", name, rec["title"],
                    f"≤ content width ({data.get('mainW','?')}px)",
                    f"{im['w']}px wide",
                    f"Image “{name}” is wider than the content container "
                    f"({im['w']}px vs {data.get('mainW','?')}px).",
                    "Reduce the image width or set max-width:100% so it fits the content column.",
                ))
            elif im.get("naturalW") and im["naturalW"] > 2.5 * max(im["w"], 1) and im["naturalW"] > 1000:
                findings.append(_f(
                    "Oversized image", "Low", name, rec["title"],
                    f"source ≈ display size ({im['w']}px)",
                    f"source {im['naturalW']}px shown at {im['w']}px",
                    f"Image “{name}” ships a {im['naturalW']}px source but renders at "
                    f"{im['w']}px — an oversized/heavy asset.",
                    "Serve a right-sized image to cut page weight.",
                ))


def _check_tables(rendered, findings):
    for rec in rendered:
        data = rec.get("data") or {}
        for i, t in enumerate(data.get("tables", []), 1):
            if t.get("overflows"):
                findings.append(_f(
                    "Table breaking", "High", f"Table {i}", rec["title"],
                    f"Fits the content width ({t.get('mainW','?')}px)",
                    f"content {t.get('scrollW','?')}px > visible {t.get('clientW','?')}px",
                    f"Table {i} is wider than the page and needs horizontal scrolling "
                    f"— it breaks out of the content column on the site.",
                    "Make the table responsive (e.g. allow wrapping or a scroll wrapper) "
                    "so it fits the content width.",
                ))


def _check_image_dimensions_vs_pdf(pdf_path, rendered, findings):
    """Compare each site page's image widths against the matching PDF section."""
    try:
        pdf_sec = _pdf_section_images(pdf_path)
    except Exception:
        return
    for rec in rendered:
        data = rec.get("data") or {}
        site_w = sorted([im["w"] for im in data.get("images", [])], reverse=True)
        if not site_w:
            continue
        key = vtc._norm_key(rec["title"] or "")
        pdf_w = sorted(pdf_sec.get(key, []), reverse=True)
        if not pdf_w:
            continue
        # Compare the largest image on each side (the section's main figure).
        sw, pw = site_w[0], pdf_w[0]
        if pw and abs(sw - pw) / pw > 0.25 and abs(sw - pw) > 40:
            findings.append(_f(
                "Image dimension", "Medium", "Main figure", rec["title"],
                f"≈ {pw}px wide (per PDF)",
                f"{sw}px wide on site",
                f"The main image on “{rec['title']}” renders at {sw}px but the PDF "
                f"figure is ≈{pw}px — a {abs(sw-pw)}px difference.",
                "Match the site image dimensions to the PDF reference.",
            ))


def validate_site_vs_pdf(pdf_path, pages, auth_token, progress_cb=None):
    """Render the AEM pages and flag image / layout / typography issues vs the PDF.

    Returns (findings, stats). Never raises for a single bad page — those are
    recorded and skipped.
    """
    if progress_cb:
        progress_cb(0.10, "launching browser")
    rendered = render_pages(pages, auth_token, progress_cb)

    if progress_cb:
        progress_cb(0.88, "analysing rendered pages")
    findings = []
    _check_typography(rendered, findings)
    _check_line_height(rendered, findings)
    _check_spacing_and_padding(rendered, findings)
    _check_alignment(rendered, findings)
    _check_images(rendered, findings)
    _check_tables(rendered, findings)
    _check_image_dimensions_vs_pdf(pdf_path, rendered, findings)

    ok_pages = [r for r in rendered if r.get("data")]
    stats = {
        "pages_rendered": len(ok_pages),
        "pages_failed":   sum(1 for r in rendered if r.get("error")),
        "images_checked": sum(len((r.get("data") or {}).get("images", [])) for r in ok_pages),
        "tables_checked": sum(len((r.get("data") or {}).get("tables", [])) for r in ok_pages),
        "render_errors":  [{"title": r["title"], "error": r["error"]}
                           for r in rendered if r.get("error")][:10],
    }
    if progress_cb:
        progress_cb(0.98, "done")
    return findings, stats

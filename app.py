import os

# Configure local TESSDATA_PREFIX before any other imports
_CUR_DIR = os.path.dirname(os.path.abspath(__file__))
_LOCAL_TESSDATA = os.path.join(_CUR_DIR, "tessdata")
if os.path.isdir(_LOCAL_TESSDATA):
    os.environ["TESSDATA_PREFIX"] = _LOCAL_TESSDATA

import collections
import contextlib
import gc
import hashlib
import json
import re
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from flask import (Flask, Response, jsonify, render_template, request,
                   send_from_directory, send_file)
import tempfile
import io
from werkzeug.utils import secure_filename
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import issue_shots as issue_shots_mod

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "tmp_uploads"
PAIRS_DIR = UPLOAD_FOLDER / "pairs"
QUEUE_FILE = PAIRS_DIR / "queue.json"
REPORTS_DIR = BASE_DIR / "reports"
ALLOWED_EXTENSIONS = {"pdf"}
PROGRESS_FILE = REPORTS_DIR / "progress.json"
# In-memory progress store to avoid writing progress to disk
PROGRESS_STORE: dict = {"total": 0, "completed": 0, "current": None, "reports": [],
                        "finished": True, "errors": [], "pct": 0, "run_id": None}
PROG_LOCK = threading.Lock()
# background job control
JOB_LOCK = threading.Lock()
JOB_THREAD = None
JOB_STARTED_AT = 0.0
# finished report held in memory for /result download
LAST_RESULT: dict = {"data": None, "name": None, "mime": None, "run_id": None}
# The PDF-to-PDF comparison renders the same findings twice. The PDF goes through
# LAST_RESULT like every other report; the HTML is kept here so it can be opened
# in the browser instead of downloaded, which is the whole point of having it.
LAST_HTML: dict = {"data": None, "name": None, "run_id": None}
WANT_HTML: bool = False
_MODE_LABELS = {"new": "New validation", "compare": "PDF to PDF comparison",
                "content": "Content validation", "style": "Style validation",
                "content_visual": "Content + Visual"}
# label -> the findings of every mode that ran on that pair, so the pair gets one
# page rather than one per mode.
HTML_ROWS: dict = {}
PAIR_PATHS: dict = {}      # label -> (PROD file name, STAGE file name)
MODES_RUN: set = set()
# track running subprocesses for cancellation
RUNNING_PROCS: list = []
# cancellation flag
CANCELLED = False

# ── BenQ PDF download (AEM) — separate page / job ──
BENQ_PDFS_DIR = BASE_DIR / "benq_pdfs"
# Source "Final Cleanup files" tree (FM/ and INDD/ hold one zip per product).
# Override with the BENQ_CLEANUP_DIR env var when the path differs.
CLEANUP_DIR = Path(os.environ.get(
    "BENQ_CLEANUP_DIR",
    "/Users/ragul/Downloads/Final Cleanup files for Pavan to add Structure and fix the alt attribute value",
))
DL_LOCK = threading.Lock()
DL_THREAD = None
DL_STORE: dict = {"running": False, "pct": 0, "current": None, "finished": True,
                  "ok": 0, "total": 0, "rows": [], "error": None}

UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
PAIRS_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["REPORTS_DIR"] = str(REPORTS_DIR)
# A folder pair is uploaded in one request, and a product manual set runs to
# well over 100 MB — the PROD folder alone is 109 MB. Past the cap Flask
# aborts the upload with a 413 whose body is an HTML error page, which the
# queue UI could only report as a JSON parse error.
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2 GB
# Reflect code/template/asset edits live — no server restart needed.
# Jinja templates re-read per request; static files aren't cached by the browser.
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0


@app.errorhandler(413)
def _too_large(_exc):
    """Answer an oversized upload as JSON, not as an HTML error page.

    The queue page reads every reply with response.json(). Flask's default 413
    body is HTML, so the browser reported the real cause — the upload was too
    big — as "Unexpected token '<'", which says nothing about what went wrong.
    """
    cap_mb = app.config["MAX_CONTENT_LENGTH"] / (1024 * 1024)
    cap = (f"{cap_mb / 1024:.0f} GB" if cap_mb >= 1024 else f"{cap_mb:.0f} MB")
    if request.path.startswith("/append_pair"):
        return jsonify(error=(f"That folder pair is larger than the {cap} upload "
                              f"limit. Append the folders in smaller batches, or "
                              f"raise MAX_CONTENT_LENGTH.")), 413
    return jsonify(error="Upload too large."), 413
app.jinja_env.auto_reload = True

# Make the content_validation package importable. The actual validation runs in
# a subprocess (run_validator.py), so these top-level imports are only a warm-up
# / availability check — never let them crash app startup, or the whole service
# fails its health check and the site goes down.
sys.path.insert(0, str(BASE_DIR / "content_validation"))
try:
    from content_validation import style_validation  # noqa: F401
    from content_validation import validate_toc_content  # noqa: F401
    from content_validation import findings_html  # noqa: F401
except Exception as _imp_exc:  # pragma: no cover
    print(f"[startup] content_validation import warning: {_imp_exc}", flush=True)


def _report_language_coverage():
    """Log what this machine can read and print. Never raises, never blocks.

    A missing language pack does not stop a run; it downgrades findings on that
    script to "needs a human look". Saying so at startup is the difference
    between a known limitation and a mystery."""
    try:
        from content_validation import pdf_compare, compare_report
        have = pdf_compare._LANGS_AVAILABLE
        if not have:
            print("[startup] Tesseract not found - figure artwork cannot be read. "
                  "Run: bash scripts/setup.sh", flush=True)
        else:
            print(f"[startup] OCR languages: {len(have)} pack(s)", flush=True)
            wanted = {"rus", "ell", "heb", "ara", "chi_sim", "chi_tra", "jpn",
                      "kor", "tha", "deu", "fra", "spa", "por", "tur"}
            gap = sorted(wanted - have)
            if gap:
                print(f"[startup] no pack for: {', '.join(gap)} - findings on "
                      f"those scripts are reported as 'needs a human look'. "
                      f"Run: bash scripts/setup.sh", flush=True)
        if compare_report.FONT == "Helvetica":
            print("[startup] no Unicode font found - non-Latin text in the PDF "
                  "report will not render. Run: bash scripts/setup.sh", flush=True)
    except Exception as exc:
        print(f"[startup] language coverage check skipped: {exc}", flush=True)


_report_language_coverage()

# Free-tier memory is tight (~512 MB); each parallel job is a full PyMuPDF
# subprocess. Cap concurrency via env so we don't OOM-kill the worker.
MAX_PARALLEL = max(1, int(os.environ.get("VALIDATOR_MAX_PARALLEL", "1")))


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def secure_filename_unicode_dir(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_").replace("\0", "")
    name = re.sub(r'[\x00-\x1f<>:"|?*]', '_', name)
    name = name.strip(". ")
    if not name:
        name = "subfolder"
    return name


def secure_filename_unicode_file(name: str) -> str:
    name = name.replace("/", "_").replace("\\", "_").replace("\0", "")
    name = re.sub(r'[\x00-\x1f<>:"|?*]', '_', name)
    name = name.strip(". ")
    if not name:
        name = "uploaded_file"
    if name.lower().endswith(".pdf"):
        name = name[:-4] + ".pdf"
    else:
        name += ".pdf"
    return name


def safe_path(filename: str) -> Path:
    filename = filename.replace("\\", "/")
    parts = [part for part in filename.split("/") if part]
    if not parts:
        return Path()
    secured_parts = [secure_filename_unicode_dir(part) for part in parts[:-1]]
    secured_parts.append(secure_filename_unicode_file(parts[-1]))
    return Path(*secured_parts)


def load_queue() -> list[dict]:
    if not QUEUE_FILE.exists():
        return []
    try:
        return json.loads(QUEUE_FILE.read_text())
    except Exception:
        return []


def save_queue(queue: list[dict]) -> None:
    QUEUE_FILE.write_text(json.dumps(queue, indent=2))


def write_progress(data: dict) -> None:
    # keep progress only in memory to avoid writing files to disk
    PROGRESS_STORE.update(data)


def read_progress() -> dict:
    out = dict(PROGRESS_STORE)
    out["html_ready"] = bool(LAST_HTML.get("data")
                             and LAST_HTML.get("run_id") == out.get("run_id"))
    return out


@app.route("/progress", methods=["GET"])
def progress_endpoint():
    """Return the current in-memory progress as JSON for frontend polling."""
    return jsonify(read_progress())


def build_report_name(mode: str, label: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_label = "_".join(
        secure_filename(part) for part in label.replace(" ", "_").split("_") if part
    )
    safe_label = safe_label[:64] or mode
    return f"{mode}_validation_{safe_label}_{stamp}.pdf"


def save_upload_to_pair(file_storage, target_dir: Path) -> str:
    relative = safe_path(file_storage.filename)
    if not relative.name or not allowed_file(relative.name):
        raise ValueError("Only PDF files are allowed inside folder uploads.")
    destination = target_dir / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_storage.save(destination)
    # Verify the uploaded PDF is valid and not corrupt
    try:
        import fitz
        doc = fitz.open(str(destination))
        doc.close()
    except Exception as exc:
        if destination.is_file():
            destination.unlink()
        raise ValueError(f"Uploaded file '{relative.name}' is not a valid PDF or is corrupt.") from exc
    return str(destination)


def root_folders(files: list) -> list[str]:
    roots = set()
    for file_storage in files:
        root = Path(file_storage.filename.replace("\\", "/")).parts
        if root:
            roots.add(root[0])
    return sorted(roots)


# A model code is a run of letters and digits carrying both: PD05U, PV3200U,
# RM05, XL25X, NE001A. Version and revision stamps look the same but name a
# build, not a product, so they are not identifiers.
_MODEL_TOKEN_RE = re.compile(r"[A-Za-z]+\d+[A-Za-z0-9]*|\d+[A-Za-z]+[A-Za-z0-9]*")
_VERSION_TOKEN_RE = re.compile(r"^(?:v|ver|rev|r)\d", re.I)


def model_keys(name: str) -> set:
    """Product codes in a file name.

    The two sides name the same manual completely differently — "PD05U-en.pdf"
    against "Monitor PD05U Series user manual (1).pdf" — so the model code is
    the only part that identifies the product across both.
    """
    stem = Path(name).stem
    keys = set()
    for tok in _MODEL_TOKEN_RE.findall(stem):
        if _VERSION_TOKEN_RE.match(tok):
            continue                      # V1, V3, Rev2 — a build, not a product
        keys.add(tok.upper())
    return keys


def pair_pdfs(prod_dir: Path, stage_dir: Path) -> list[tuple[Path, Path, str]]:
    """[(prod_pdf, stage_pdf, label)] matched by the product each file names.

    Pairing by sorted position was wrong whenever the two sides use different
    naming conventions, which is the normal case: it lined "PD05U-en.pdf" up
    against "BenQ Board NE001A_RE05E_UM user manual.pdf" and compared two
    unrelated manuals. Files are matched on their model code instead, and a file
    with no counterpart is left out rather than failing the whole folder pair.
    """
    prod_pdfs = sorted(prod_dir.rglob("*.pdf"))
    stage_pdfs = sorted(stage_dir.rglob("*.pdf"))
    if not prod_pdfs or not stage_pdfs:
        raise ValueError("Each appended folder pair must contain at least one PDF file.")
    if len(prod_pdfs) == 1 and len(stage_pdfs) == 1:
        return [(prod_pdfs[0], stage_pdfs[0], "")]

    def label_for(prod_pdf, stage_pdf):
        return (f"{prod_pdf.relative_to(prod_dir)} vs "
                f"{stage_pdf.relative_to(stage_dir)}")

    stage_keys = [(sp, model_keys(sp.name)) for sp in stage_pdfs]
    pairs, taken, unmatched = [], set(), []
    for prod_pdf in prod_pdfs:
        want = model_keys(prod_pdf.name)
        best, best_score = None, 0
        for stage_pdf, have in stage_keys:
            if stage_pdf in taken or not want or not have:
                continue
            shared = want & have
            if not shared:
                continue
            # The longest shared code wins: "PD06U" beats a chance overlap on a
            # shorter token, so PD06U never pairs with PD05U.
            score = max(len(k) for k in shared) * 100 + len(shared)
            if score > best_score:
                best, best_score = stage_pdf, score
        if best is None:
            unmatched.append(prod_pdf.name)
            continue
        taken.add(best)
        pairs.append((prod_pdf, best, label_for(prod_pdf, best)))

    if pairs:
        for stage_pdf, _ in stage_keys:
            if stage_pdf not in taken:
                unmatched.append(stage_pdf.name)
        if unmatched:
            print(f"[pair] no counterpart, not validated: {', '.join(unmatched)}",
                  flush=True)
        return pairs

    # Nothing carries a model code. Fall back to position, which is right only
    # when both folders are ordered the same way and hold the same count.
    if len(prod_pdfs) == len(stage_pdfs):
        return [(prod_pdfs[i], stage_pdfs[i], label_for(prod_pdfs[i], stage_pdfs[i]))
                for i in range(len(prod_pdfs))]
    raise ValueError(
        "No PROD file could be matched to a STAGE file. Name each pair after the "
        "same product (a shared model code such as PD05U), or put the same number "
        "of PDFs in both folders so they can be paired in order."
    )


def build_pair_label(prod_files: list, stage_files: list) -> str:
    prod_roots = root_folders(prod_files)
    stage_roots = root_folders(stage_files)
    if len(prod_roots) == 1 and len(stage_roots) == 1 and prod_roots[0] == stage_roots[0]:
        return prod_roots[0]
    if len(prod_roots) == 1 and len(stage_roots) == 1:
        return f"{prod_roots[0]} vs {stage_roots[0]}"
    return f"prod_{len(prod_files)}_files_vs_stage_{len(stage_files)}_files"


def clean_queue_files() -> None:
    queue = load_queue()
    for item in queue:
        pair_dir = PAIRS_DIR / item["id"]
        if pair_dir.exists():
            shutil.rmtree(pair_dir, ignore_errors=True)
    save_queue([])


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/queue", methods=["GET"])
def queue():
    return jsonify(queue=load_queue())


@app.route("/append_pair", methods=["POST"])
def append_pair():
    prod_files = request.files.getlist("prod_files")
    stage_files = request.files.getlist("stage_files")
    if not prod_files or not stage_files:
        return jsonify(error="Please select both a PROD folder and a STAGE folder."), 400

    pair_id = uuid.uuid4().hex
    pair_dir = PAIRS_DIR / pair_id
    prod_dir = pair_dir / "prod"
    stage_dir = pair_dir / "stage"
    prod_dir.mkdir(parents=True, exist_ok=True)
    stage_dir.mkdir(parents=True, exist_ok=True)

    try:
        for file_storage in prod_files:
            save_upload_to_pair(file_storage, prod_dir)
        for file_storage in stage_files:
            save_upload_to_pair(file_storage, stage_dir)
    except ValueError as exc:
        shutil.rmtree(pair_dir, ignore_errors=True)
        return jsonify(error=str(exc)), 400

    prod_count = len(list(prod_dir.rglob("*.pdf")))
    stage_count = len(list(stage_dir.rglob("*.pdf")))
    if prod_count == 0 or stage_count == 0:
        shutil.rmtree(pair_dir, ignore_errors=True)
        return jsonify(error="Selected folders must contain PDF files."), 400
    # Collect a preview of files for UI display (store relative paths)
    prod_list = [str(p.relative_to(prod_dir)) for p in sorted(prod_dir.rglob("*.pdf"))]
    stage_list = [str(p.relative_to(stage_dir)) for p in sorted(stage_dir.rglob("*.pdf"))]

    # Show which file will be compared with which, and which have no counterpart,
    # at append time — the pairing is decided here, so it should be visible here
    # rather than discovered from the reports afterwards.
    try:
        matched = pair_pdfs(prod_dir, stage_dir)
    except ValueError:
        matched = []
    file_pairs = [{"prod": p.name, "stage": s.name} for p, s, _ in matched]
    paired_prod = {p.name for p, _, _ in matched}
    paired_stage = {s.name for _, s, _ in matched}
    unpaired = ([f"PROD: {n}" for n in
                 sorted({Path(f).name for f in prod_list} - paired_prod)]
                + [f"STAGE: {n}" for n in
                   sorted({Path(f).name for f in stage_list} - paired_stage)])

    label = build_pair_label(prod_files, stage_files)
    queue = load_queue()
    queue.append({
        "id": pair_id,
        "label": label,
        "prod_count": prod_count,
        "stage_count": stage_count,
        "prod_files": prod_list[:12],
        "stage_files": stage_list[:12],
        "file_pairs": file_pairs,
        "pair_count": len(file_pairs),
        "unpaired": unpaired,
        "created": datetime.now().isoformat(),
    })
    save_queue(queue)
    return jsonify(queue=queue)


@app.route("/clear_queue", methods=["POST"])
def clear_queue():
    clean_queue_files()
    return jsonify(queue=[])


class _Cancelled(BaseException):
    # BaseException (not Exception) so the validators' `except Exception` inside
    # _emit() can't swallow it — cancellation must propagate out and abort.
    pass


def _safe_unlink(p):
    try:
        os.unlink(p)
    except Exception:
        pass


def _run_jobs(tasks, run_id):
    """Background-thread orchestrator. Dispatches to in-process (memory-light,
    free-tier safe) or parallel subprocesses (faster on bigger instances).

    MAX_PARALLEL == 1  → in-process, sequential (one PyMuPDF copy, lowest memory).
    MAX_PARALLEL  > 1  → up to N validations at once, each in its OWN subprocess.
                         Process isolation is required because PyMuPDF is not
                         thread-safe — running validations in threads can segfault
                         the worker. Each extra worker also costs ~one PDF's worth
                         of RAM, so keep N within the instance's memory budget.
    """
    produced = []
    try:
        if MAX_PARALLEL <= 1 or any(t[0] == "compare" for t in tasks):
            produced = _run_sequential_inproc(tasks)
        else:
            produced = _run_parallel_subprocess(tasks)
    finally:
        with PROG_LOCK:
            if PROGRESS_STORE.get("run_id") == run_id:
                _finalize_result([] if CANCELLED else produced, run_id)
                PROGRESS_STORE["finished"] = True
                PROGRESS_STORE["cancelled"] = bool(CANCELLED)
                if not CANCELLED:
                    PROGRESS_STORE["pct"] = 100
                PROGRESS_STORE["current"] = "cancelled" if CANCELLED else "done"


@contextlib.contextmanager
def _capture_toc_findings(store: dict):
    """Record the finding lists validate_toc_content hands its report builder.

    That validator returns nothing and writes a PDF; the findings exist only as
    the keyword arguments of one call. Wrapping that call reads them without
    touching a 4,800-line module that the other modes depend on.
    """
    original = validate_toc_content.generate_report

    def recorder(*args, **kwargs):
        store.update(kwargs)
        names = ("prod_path", "stage_path", "toc_results", "content_results",
                 "image_results", "icon_doc_summary", "report_path")
        for name, value in zip(names, args):
            store.setdefault(name, value)
        return original(*args, **kwargs)

    validate_toc_content.generate_report = recorder
    try:
        yield store
    finally:
        validate_toc_content.generate_report = original


def _collect_rows(label: str, adapt) -> None:
    """Pool one mode's findings for the run's HTML page.

    The page is a convenience laid over a validation that has already produced
    its PDF. An adapter that trips over an unexpected shape must cost the page
    those rows, never the validation.
    """
    try:
        HTML_ROWS.setdefault(label, []).extend(adapt())
    except Exception as exc:
        print(f"[html] could not read findings for '{label}': {exc}", flush=True)


def _render_html(rows, label, prod_path, stage_path, mode_label) -> bytes:
    """The findings of any mode, as a self-contained page."""
    from content_validation import findings_html
    tmp = tempfile.mkdtemp(prefix="html_report_")
    try:
        page = os.path.join(tmp, "report.html")
        findings_html.render(rows, page, {
            "name": label,
            "title": f"{label} Validation Report",
            "mode_label": mode_label,
            "mode": mode_label,
            "prod": str(prod_path),
            "stage": str(stage_path),
            "run": datetime.now().strftime("%Y-%m-%d"),
        })
        return open(page, "rb").read()
    except Exception as exc:
        print(f"[html] could not render the HTML report: {exc}", flush=True)
        return b""
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _run_sequential_inproc(tasks):
    """Run tasks one at a time in this process (no subprocess, lowest memory)."""
    n = len(tasks)
    produced = []
    for i, (mode_task, prod_path, stage_path, label) in enumerate(tasks):
        if CANCELLED:
            break
        base, span = i / n, 1.0 / n

        def cb(frac, msg="", _b=base, _s=span, _i=i, _m=mode_task, _l=label):
            if CANCELLED:
                raise _Cancelled()
            with PROG_LOCK:
                PROGRESS_STORE["pct"] = int(round((_b + _s * frac) * 100))
                PROGRESS_STORE["completed"] = _i + (1 if frac >= 1.0 else 0)
                PROGRESS_STORE["current"] = (f"{_m}: {_l} — {msg}" if msg else f"{_m}: {_l}")

        tf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tf.close()
        outpath = tf.name
        artifact_dir = tempfile.mkdtemp(prefix="dual_markdown_") if mode_task == "content_visual" else None
        compare_dir = tempfile.mkdtemp(prefix="pdf_compare_") if mode_task == "compare" else None
        PAIR_PATHS[label] = (os.path.basename(str(prod_path)),
                             os.path.basename(str(stage_path)))
        MODES_RUN.add(_MODE_LABELS.get(mode_task, mode_task))
        try:
            captured = {}
            if mode_task == "compare":
                # One comparison, two renderings. The PDF is returned through the
                # normal path; the HTML travels with it and is served inline.
                from content_validation import pdf_compare, compare_report
                diffs, prod_doc, stage_doc, pmap = pdf_compare.compare(
                    str(prod_path), str(stage_path),
                    lambda pct, msg: cb(pct / 100.0, msg))
                meta = {"name": label,
                        "title": f"{label} Content Difference Report",
                        "matched": sum(1 for v in pmap.values() if v[0]),
                        "run": datetime.now().strftime("%Y-%m-%d")}
                built = compare_report.build(diffs, prod_doc, stage_doc,
                                             compare_dir, "report", meta)
                shutil.copyfile(built["pdf"], outpath)
                if WANT_HTML:
                    _collect_rows(label, lambda: findings_html.from_compare(
                        diffs, compare_report.evidence(diffs, prod_doc,
                                                       stage_doc)))
                cb(1.0, "done")
            elif mode_task == "content_visual":
                # Separate pipeline (content_validation/dual_*.py); the existing
                # content and style validators are untouched by it.
                from content_validation import dual_runner
                dual_runner.set_progress_callback(cb)
                dual_findings, _met = dual_runner.main(
                    str(prod_path), str(stage_path), outpath, artifact_dir)
                if WANT_HTML:
                    _collect_rows(label,
                                  lambda: findings_html.from_dual(dual_findings))
            elif mode_task == "style":
                style_validation.set_progress_callback(cb)
                style_findings = style_validation.main(
                    str(prod_path), str(stage_path), outpath)
                if WANT_HTML:
                    _collect_rows(label, lambda: findings_html.from_style(
                        style_findings or []))
            # "new" and "content" both run the consolidated content validator.
            else:
                validate_toc_content.set_progress_callback(cb)
                with _capture_toc_findings(captured):
                    validate_toc_content.validate(str(prod_path), str(stage_path),
                                                  outpath)
                if WANT_HTML:
                    _collect_rows(label,
                                  lambda: findings_html.from_toc_kwargs(captured))
        except _Cancelled:
            _safe_unlink(outpath)
            for scratch in (artifact_dir, compare_dir):
                if scratch:
                    shutil.rmtree(scratch, ignore_errors=True)
            break
        except Exception:
            tb = traceback.format_exc()
            last = tb.strip().splitlines()[-1] if tb.strip() else "unknown error"
            print(f"[validate] {mode_task} failed for '{label}':\n{tb}", flush=True)
            with PROG_LOCK:
                PROGRESS_STORE["errors"].append(f"{mode_task} validation failed for '{label}': {last}")
            _safe_unlink(outpath)
            for scratch in (artifact_dir, compare_dir):
                if scratch:
                    shutil.rmtree(scratch, ignore_errors=True)
            continue
        finally:
            style_validation.set_progress_callback(None)
            validate_toc_content.set_progress_callback(None)
            gc.collect()

        try:
            data = open(outpath, "rb").read()
        except Exception:
            data = b""
        _safe_unlink(outpath)
        if len(data) > 1024:
            markdown = {}
            if compare_dir:
                shutil.rmtree(compare_dir, ignore_errors=True)
            if artifact_dir:
                for name in ("PROD.md", "STAGE.md"):
                    path = os.path.join(artifact_dir, name)
                    if os.path.isfile(path):
                        markdown[name] = open(path, "rb").read()
                shutil.rmtree(artifact_dir, ignore_errors=True)
            produced.append((label, mode_task, data, markdown))
        with PROG_LOCK:
            PROGRESS_STORE["completed"] = i + 1
            PROGRESS_STORE["pct"] = int(round((i + 1) / n * 100)) if n else 100
    return produced


_PROG_RE = re.compile(r"@@PROGRESS:([0-9.]+):([^@]*)@@")


def _run_parallel_subprocess(tasks):
    """Run up to MAX_PARALLEL validations concurrently, each in its own process.

    Streams each subprocess's stdout for @@PROGRESS@@ markers so the bar reflects
    the combined progress of all running workers.
    """
    n = len(tasks)
    frac = [0.0] * n
    slots = [None] * n     # (name, data) or None
    flock = threading.Lock()

    def refresh(current=None):
        with PROG_LOCK:
            PROGRESS_STORE["completed"] = sum(1 for f in frac if f >= 1.0)
            PROGRESS_STORE["pct"] = int(round(sum(frac) / n * 100)) if n else 100
            if current is not None:
                PROGRESS_STORE["current"] = current

    def run_one(i, mode_task, prod_path, stage_path, label):
        tf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
        tf.close()
        artifact_dir = tempfile.mkdtemp(prefix="dual_markdown_") if mode_task == "content_visual" else None
        errf = tempfile.NamedTemporaryFile(suffix=".log", delete=False, mode="w+")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(BASE_DIR / "run_validator.py"),
                 mode_task, str(prod_path), str(stage_path), tf.name]
                + ([artifact_dir] if artifact_dir else []),
                stdout=subprocess.PIPE, stderr=errf, text=True, bufsize=1)
        except Exception as exc:
            errf.close(); _safe_unlink(errf.name); _safe_unlink(tf.name)
            if artifact_dir:
                shutil.rmtree(artifact_dir, ignore_errors=True)
            with flock:
                PROGRESS_STORE["errors"].append(f"{mode_task} could not start for '{label}': {exc}")
            frac[i] = 1.0; refresh()
            return
        RUNNING_PROCS.append(proc)
        refresh(f"{mode_task}: {label}")
        try:
            for line in proc.stdout:
                if CANCELLED:
                    proc.kill(); break
                m = _PROG_RE.search(line)
                if m:
                    frac[i] = float(m.group(1))
                    refresh(f"{mode_task}: {label} — {m.group(2).strip()}")
        except Exception:
            pass
        proc.wait()
        errf.seek(0); err = errf.read()[-1500:].strip(); errf.close(); _safe_unlink(errf.name)
        try:
            RUNNING_PROCS.remove(proc)
        except Exception:
            pass
        frac[i] = 1.0
        rc = proc.returncode
        try:
            data = open(tf.name, "rb").read()
        except Exception:
            data = b""
        _safe_unlink(tf.name)
        if rc == 0 and len(data) > 1024 and not CANCELLED:
            markdown = {}
            if artifact_dir:
                for name in ("PROD.md", "STAGE.md"):
                    path = os.path.join(artifact_dir, name)
                    if os.path.isfile(path):
                        markdown[name] = open(path, "rb").read()
                shutil.rmtree(artifact_dir, ignore_errors=True)
            slots[i] = (label, mode_task, data, markdown)
        elif not CANCELLED:
            msg = f"{mode_task} validation failed for '{label}' (exit code {rc})."
            if err:
                msg += f" {err.splitlines()[-1]}"
            print(f"[validate] {msg}", flush=True)
            with flock:
                PROGRESS_STORE["errors"].append(msg)
            if artifact_dir:
                shutil.rmtree(artifact_dir, ignore_errors=True)
        refresh()

    with ThreadPoolExecutor(max_workers=MAX_PARALLEL) as ex:
        futs = [ex.submit(run_one, i, *t) for i, t in enumerate(tasks)]
        for _ in as_completed(futs):
            pass
    return [s for s in slots if s]


def _clean_folder(name: str) -> str:
    """A zip-safe folder name derived from the PROD folder label."""
    name = (name or "report").replace("\\", "/").split("/")[-1].strip()
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "", name).strip(". ")
    return name or "report"


def _finalize_result(produced, run_id=None):
    """Build the download as a SINGLE PDF report — never a zip.

    A run can produce several reports (one per PROD folder, and one per mode
    when mode="both"). They are concatenated, in run order, into one PDF so the
    user always fetches exactly one file. Each report keeps its own pages; a
    bookmark per report is added so the merged document stays navigable.
    """
    # Content + Visual returns its annotated PDF together with the two Markdown
    # views produced from the same extraction pass.
    # The HTML rendering is produced once for the whole run - every mode that
    # ran, every pair that was queued - so there is always exactly one page to
    # open, whatever was selected. The PDF path below is untouched.
    if WANT_HTML and HTML_ROWS:
        rows, labels = [], []
        for label, found in HTML_ROWS.items():
            labels.append(label)
            for row in found:
                if len(HTML_ROWS) > 1:
                    row.detail = f"[{label}] {row.detail}".strip()
                rows.append(row)
        prod = ", ".join(sorted({p for p, _s in PAIR_PATHS.values()}))
        stage = ", ".join(sorted({s for _p, s in PAIR_PATHS.values()}))
        title = labels[0] if len(labels) == 1 else f"{len(labels)} pairs"
        page = _render_html(rows, title, prod, stage,
                            ", ".join(sorted(MODES_RUN)) or "Validation")
        prefix = _clean_folder(labels[0]) if len(labels) == 1 else "validation"
        LAST_HTML.update({"data": page or None, "run_id": run_id,
                          "name": f"{prefix}_report.html" if page else None})

    if any(len(item) > 3 and item[1] == "content_visual" for item in produced):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for folder, mode_task, data, markdown in produced:
                prefix = _clean_folder(folder)
                bundle.writestr(f"{prefix}/{prefix}_{mode_task}_validation_report.pdf", data)
                for filename, contents in markdown.items():
                    bundle.writestr(f"{prefix}/{filename}", contents)
        LAST_RESULT.update({"data": archive.getvalue(), "mime": "application/zip",
                    "name": f"content_visual_validation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                    "run_id": run_id})
        return

    # de-dup identical reports (e.g. the same folder queued twice). Comparing
    # raw bytes is not enough: every reportlab build stamps a fresh document ID
    # and creation date, so two runs over the same pair of PDFs differ byte for
    # byte while rendering identically. Hash the extracted page text instead,
    # falling back to the bytes when the PDF cannot be read.
    def _report_fingerprint(data):
        try:
            import fitz
            doc = fitz.open(stream=data, filetype="pdf")
            text = "\n".join(page.get_text() for page in doc)
            doc.close()
            if text.strip():
                return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        except Exception:
            pass
        return hashlib.sha256(data).hexdigest()

    seen, items = set(), []
    for item in produced:
        folder, mode_task, data = item[:3]
        h = (mode_task, _report_fingerprint(data))
        if h in seen:
            continue
        seen.add(h)
        items.append((folder, mode_task, data))

    if not items:
        LAST_RESULT.update({"data": None, "name": None, "mime": None, "run_id": run_id})
        return

    if len(items) == 1:
        folder, mode_task, data = items[0]
        LAST_RESULT.update({"data": data, "mime": "application/pdf",
                    "name": f"{_clean_folder(folder)}_{mode_task}_validation_report.pdf",
                    "run_id": run_id})
        return

    import fitz

    def _merge(parts, folder):
        """One PDF for one pair: its modes in run order, bookmarked."""
        merged, toc = fitz.open(), []
        for mode_task, data in parts:
            try:
                part = fitz.open(stream=data, filetype="pdf")
            except Exception as exc:
                print(f"[report] skipping unreadable {mode_task} report for "
                      f"'{folder}': {exc}", flush=True)
                continue
            start_page = merged.page_count + 1        # 1-based for the bookmark
            merged.insert_pdf(part)
            part.close()
            toc.append([1, f"{_clean_folder(folder)} — {mode_task}", start_page])
        if merged.page_count == 0:
            merged.close()
            return None
        if toc:
            merged.set_toc(toc)
        out = merged.tobytes(deflate=True, garbage=3)
        merged.close()
        return out

    # Group by pair. A run over a folder pair validates each product separately,
    # and a single stitched-together PDF cannot be handed to whoever owns one of
    # them — so each pair gets its own report, and several pairs are delivered
    # as a zip of those reports rather than as one document.
    by_pair = collections.OrderedDict()
    for folder, mode_task, data in items:
        by_pair.setdefault(folder, []).append((mode_task, data))

    reports = []
    for folder, parts in by_pair.items():
        data = _merge(parts, folder)
        if data:
            reports.append((folder, data))

    if not reports:
        LAST_RESULT.update({"data": None, "name": None, "mime": None, "run_id": run_id})
        return

    if len(reports) == 1:
        folder, data = reports[0]
        LAST_RESULT.update({"data": data, "mime": "application/pdf",
                            "name": f"{_clean_folder(folder)}_validation_report.pdf",
                            "run_id": run_id})
        return

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for folder, data in reports:
            prefix = _clean_folder(folder)
            bundle.writestr(f"{prefix}_validation_report.pdf", data)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    LAST_RESULT.update({"data": archive.getvalue(), "mime": "application/zip",
                        "name": f"validation_reports_{stamp}.zip",
                        "run_id": run_id})


@app.route("/validate", methods=["POST"])
def validate():
    mode = request.form.get("mode")
    if mode not in {"new", "content_visual", "compare", "content", "style", "both"}:
        return jsonify(error="Please select a validation mode."), 400

    queue = load_queue()
    if not queue:
        return jsonify(error="No appended folder pairs to validate."), 400

    # Validate what the user ticked. The page tracks a selection and shows
    # "N selected", but the run used the whole queue regardless — so a run over
    # one ticked pair silently validated every queued pair as well.
    wanted = [i for i in request.form.getlist("pair_ids") if i]
    if wanted:
        chosen = [item for item in queue if item.get("id") in set(wanted)]
        if not chosen:
            return jsonify(error="The selected folder pairs are no longer in the "
                                 "queue. Refresh the page and try again."), 400
        queue = chosen

    # Build (prod, stage, label) bundles from the queued folder pairs. The label
    # becomes the zip folder name, so each product PDF must be named after its own
    # folder — not a shared root + index — when one upload holds many products.
    bundles_list = []
    try:
        for item in queue:
            pair_dir = PAIRS_DIR / item["id"]
            prod_dir, stage_dir = pair_dir / "prod", pair_dir / "stage"
            bundles = pair_pdfs(prod_dir, stage_dir)
            for index, (prod_path, stage_path, detail) in enumerate(bundles, start=1):
                if len(bundles) > 1:
                    # One report per pair, named after the product. The PDFs of a
                    # folder upload all share one parent directory, so naming a
                    # report after that folder gave every pair in the upload the
                    # same name and made the reports indistinguishable.
                    codes = sorted(model_keys(Path(prod_path).name))
                    label = "_".join(codes) if codes else Path(prod_path).stem
                else:
                    label = item["label"]
                bundles_list.append((prod_path, stage_path, label))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    # Expand into per-mode tasks.
    tasks = []
    for (prod_path, stage_path, label) in bundles_list:
        if mode == "new":
            # New validation is both reports: the issue table it has always
            # produced, and the PDF-to-PDF comparison that boxes each difference
            # on both pages. They answer different questions and the merged PDF
            # carries them in that order.
            tasks.append(("new", prod_path, stage_path, label))
            tasks.append(("compare", prod_path, stage_path, label))
        if mode == "compare":
            tasks.append(("compare", prod_path, stage_path, label))
        if mode == "content_visual":
            tasks.append(("content_visual", prod_path, stage_path, label))
        if mode == "compare":
            tasks.append(("compare", prod_path, stage_path, label))
        if mode in ("content", "both"):
            tasks.append(("content", prod_path, stage_path, label))
        if mode in ("style", "both"):
            tasks.append(("style", prod_path, stage_path, label))

    # The HTML rendering is a property of the run, not of a mode: whatever was
    # selected, the findings can also be written as a page.
    global WANT_HTML
    WANT_HTML = request.form.get("html_report") in ("1", "on", "true", "yes")
    HTML_ROWS.clear()
    PAIR_PATHS.clear()
    MODES_RUN.clear()

    global JOB_THREAD, CANCELLED, JOB_STARTED_AT
    with JOB_LOCK:
        running = JOB_THREAD is not None and JOB_THREAD.is_alive()
        # A job orphaned past this many seconds (e.g. a worker that hung) is
        # treated as dead so the user is never permanently locked out.
        stale = running and (time.time() - JOB_STARTED_AT) > 1800
        if running and not stale:
            return jsonify(error="A validation run is already in progress. "
                                 "Use Cancel to stop it, or wait for it to finish.",
                           in_progress=True,
                           run_id=PROGRESS_STORE.get("run_id")), 409
        # (a stale/orphaned thread is simply abandoned; the new run starts fresh)
        CANCELLED = False
        JOB_STARTED_AT = time.time()
        run_id = uuid.uuid4().hex
        LAST_RESULT.update({"data": None, "name": None, "mime": None, "run_id": None})
        LAST_HTML.update({"data": None, "name": None, "run_id": None})
        with PROG_LOCK:
            PROGRESS_STORE.update({"total": len(tasks), "completed": 0,
                                   "current": "starting…", "reports": [],
                                   "finished": False, "errors": [], "pct": 0,
                                   "run_id": run_id})
        JOB_THREAD = threading.Thread(target=_run_jobs, args=(tasks, run_id), daemon=True)
        JOB_THREAD.start()

    # Return immediately; the browser polls /progress, then GETs /result.
    return jsonify(started=True, total=len(tasks), run_id=run_id)


@app.route("/result", methods=["GET"])
def result():
    requested_run_id = request.args.get("run_id")
    if requested_run_id and requested_run_id != LAST_RESULT.get("run_id"):
        return jsonify(error="The requested validation report is not ready yet."), 404
    data = LAST_RESULT.get("data")
    if not data:
        errs = (read_progress().get("errors") or [])
        if errs:
            return jsonify(error="Validation produced no report.", errors=errs), 422
        return jsonify(error="No report is ready yet."), 404
    bio = io.BytesIO(data)
    bio.seek(0)
    return send_file(bio, mimetype=LAST_RESULT["mime"], as_attachment=True,
                     download_name=LAST_RESULT["name"])


@app.route("/result.html", methods=["GET"])
def result_html():
    """The comparison report as a page, opened rather than downloaded."""
    requested_run_id = request.args.get("run_id")
    if requested_run_id and requested_run_id != LAST_HTML.get("run_id"):
        return "The requested report is not ready yet.", 404
    data = LAST_HTML.get("data")
    if not data:
        return "No HTML report is ready. Run a PDF \u21c4 PDF comparison first.", 404
    return Response(data, mimetype="text/html; charset=utf-8")


@app.route('/cancel', methods=['POST'])
def cancel():
    """Stop the running job: flag it (in-process aborts at the next progress
    tick) and kill any running subprocesses (parallel mode)."""
    global CANCELLED
    CANCELLED = True
    for p in list(RUNNING_PROCS):
        try:
            p.kill()
        except Exception:
            pass
    with PROG_LOCK:
        PROGRESS_STORE['current'] = 'cancelling…'
    return jsonify(status='cancelling')


@app.route("/reports/<path:filename>")
def download_report(filename: str):
    return send_from_directory(app.config["REPORTS_DIR"], filename, as_attachment=True)


# ─────────────────────────────────────────────────────────────────────────────
# BenQ PDF download page — fetches the latest generated PDFs from AEM
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/download", methods=["GET"])
def download_page():
    return render_template("download.html")


def _run_download():
    """Background worker: pull the latest BenQ PDFs and record progress."""
    sys.path.insert(0, str(BASE_DIR))
    try:
        import importlib
        import download_benq_pdfs as dl
        importlib.reload(dl)  # pick up any credential/product edits without restart

        def cb(frac, msg=""):
            with DL_LOCK:
                DL_STORE["pct"] = int(round(frac * 100))
                DL_STORE["current"] = msg

        res = dl.download_all(progress_cb=cb)
        with DL_LOCK:
            DL_STORE.update({"ok": res["ok"], "total": res["total"],
                             "rows": res["rows"], "pct": 100,
                             "current": f"Downloaded {res['ok']}/{res['total']}"})
    except Exception as exc:
        tb = traceback.format_exc()
        print(f"[download] failed:\n{tb}", flush=True)
        last = tb.strip().splitlines()[-1] if tb.strip() else str(exc)
        with DL_LOCK:
            DL_STORE["error"] = last
            DL_STORE["current"] = "failed"
    finally:
        with DL_LOCK:
            DL_STORE["running"] = False
            DL_STORE["finished"] = True


@app.route("/download/start", methods=["POST"])
def download_start():
    global DL_THREAD
    with DL_LOCK:
        if DL_THREAD is not None and DL_THREAD.is_alive():
            return jsonify(error="A download is already in progress.", in_progress=True), 409
        DL_STORE.update({"running": True, "pct": 0, "current": "starting…",
                         "finished": False, "ok": 0, "total": 0, "rows": [],
                         "error": None})
        DL_THREAD = threading.Thread(target=_run_download, daemon=True)
        DL_THREAD.start()
    return jsonify(started=True)


@app.route("/download/progress", methods=["GET"])
def download_progress():
    with DL_LOCK:
        return jsonify(dict(DL_STORE))


@app.route("/download/zip", methods=["GET"])
def download_zip():
    """Zip up everything currently in benq_pdfs/ for a one-click download."""
    if not BENQ_PDFS_DIR.exists() or not any(BENQ_PDFS_DIR.rglob("*.pdf")):
        return jsonify(error="No downloaded PDFs available yet."), 404
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        for pdf in sorted(BENQ_PDFS_DIR.rglob("*.pdf")):
            zf.write(pdf, pdf.relative_to(BENQ_PDFS_DIR))
    bio.seek(0)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(bio, mimetype="application/zip", as_attachment=True,
                     download_name=f"benq_pdfs_{stamp}.zip")


# ─────────────────────────────────────────────────────────────────────────────
# Per-product download — match each downloaded PROD PDF (benq_pdfs/<folder>/)
# to the matching source zip in the Final Cleanup tree (FM/ or INDD/) by folder
# name, and offer both for download from the page.
# ─────────────────────────────────────────────────────────────────────────────
def _norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _cleanup_index() -> dict:
    """norm(zip stem) -> (source, Path) for every zip under FM/ and INDD/."""
    idx = {}
    for sub in ("FM", "INDD"):
        d = CLEANUP_DIR / sub
        if not d.is_dir():
            continue
        for z in sorted(d.glob("*.zip")):
            idx.setdefault(_norm_name(z.stem), (sub, z))
    return idx


# Hand-verified matches where names differ enough that fuzzy matching can't be
# trusted (model variant / version drift). Key = PROD folder, value = zip stem.
EXPLICIT_CLEANUP = {
    # PROD "BenQ Board RE04A" ↔ cleanup "BenQ Board RE04" user manual (V2.2).
    "RE04A_UM_V1.2_EN": "RE04_UM_V2.2_EN",
}


def _match_cleanup(idx: dict, product: str):
    """Find the cleanup zip whose name matches a benq_pdfs folder name."""
    if product in EXPLICIT_CLEANUP:
        hit = idx.get(_norm_name(EXPLICIT_CLEANUP[product]))
        if hit:
            return hit
    n = _norm_name(product)
    if n in idx:
        return idx[n]
    # prefix in either direction (tolerates _EN suffixes, V1.0 vs V1.00, etc.)
    cands = [(k, v) for k, v in idx.items() if k.startswith(n) or n.startswith(k)]
    if cands:
        cands.sort(key=lambda kv: -len(os.path.commonprefix([kv[0], n])))
        return cands[0][1]
    return None


def _matched_products() -> list:
    """Every PROD product (benq_pdfs/<folder>/ with a PDF). Each entry also
    carries its matching Final Cleanup ZIP when one exists (else zip_name=None)."""
    idx = _cleanup_index()
    out = []
    if not BENQ_PDFS_DIR.is_dir():
        return out
    for folder in sorted(BENQ_PDFS_DIR.iterdir()):
        if not folder.is_dir():
            continue
        pdfs = sorted(folder.glob("*.pdf"))
        if not pdfs:
            continue
        # Some PROD PDFs land on disk with an empty stem (".pdf"); give those a
        # friendly download name based on the product folder.
        dl_name = f"{folder.name}.pdf" if pdfs[0].name.lower() == ".pdf" else pdfs[0].name
        entry = {
            "product": folder.name,
            "pdf_name": pdfs[0].name,
            "pdf_dl": dl_name,
            "pdf_kb": pdfs[0].stat().st_size // 1024,
            "zip_name": None,
            "zip_kb": None,
            "source": None,
        }
        m = _match_cleanup(idx, folder.name)
        if m:
            source, zip_path = m
            entry.update(zip_name=zip_path.name,
                         zip_kb=zip_path.stat().st_size // 1024, source=source)
        out.append(entry)
    return out


@app.route("/download/products", methods=["GET"])
def download_products():
    return jsonify(products=_matched_products())


def _register_pair(pair_id: str, label: str, prod_dir: Path, stage_dir: Path) -> list:
    """Record a prepared prod/stage pair in the validation queue (shared helper)."""
    prod_list = [str(p.relative_to(prod_dir)) for p in sorted(prod_dir.rglob("*.pdf"))]
    stage_list = [str(p.relative_to(stage_dir)) for p in sorted(stage_dir.rglob("*.pdf"))]
    queue = load_queue()
    queue.append({
        "id": pair_id, "label": label,
        "prod_count": len(prod_list), "stage_count": len(stage_list),
        "prod_files": prod_list[:12], "stage_files": stage_list[:12],
        "created": datetime.now().isoformat(),
    })
    save_queue(queue)
    return queue


def _extract_stage_pdf(zip_path: Path, dest_dir: Path) -> Path:
    """Extract the largest embedded PDF from a cleanup zip into dest_dir."""
    with zipfile.ZipFile(zip_path) as z:
        pdfs = [n for n in z.namelist()
                if n.lower().endswith(".pdf") and "__MACOSX" not in n]
        if not pdfs:
            raise ValueError("No PDF found inside the Final Cleanup file.")
        pick = max(pdfs, key=lambda n: z.getinfo(n).file_size)  # the full manual
        out = dest_dir / os.path.basename(pick)
        out.write_bytes(z.read(pick))
        return out


@app.route("/download/add-to-validation/<path:product>", methods=["POST"])
def add_to_validation(product: str):
    """Queue a PROD (benq_pdfs) vs STAGE (cleanup-zip PDF) pair for validation —
    no download, no manual upload."""
    match = next((m for m in _matched_products() if m["product"] == product), None)
    if not match:
        return jsonify(error="Unknown product."), 404
    if not match["zip_name"]:
        return jsonify(error="No STAGE source (Final Cleanup file) for this product."), 400
    prod_src = BENQ_PDFS_DIR / product / match["pdf_name"]
    zip_path = CLEANUP_DIR / match["source"] / match["zip_name"]
    if not prod_src.is_file() or not zip_path.is_file():
        return jsonify(error="Source files are missing on the server."), 404

    pair_id = uuid.uuid4().hex
    pair_dir = PAIRS_DIR / pair_id
    prod_dir = pair_dir / "prod"
    stage_dir = pair_dir / "stage"
    prod_dir.mkdir(parents=True, exist_ok=True)
    stage_dir.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(prod_src, prod_dir / match["pdf_dl"])
        _extract_stage_pdf(zip_path, stage_dir)
    except Exception as exc:
        shutil.rmtree(pair_dir, ignore_errors=True)
        return jsonify(error=str(exc)), 400

    queue = _register_pair(pair_id, product, prod_dir, stage_dir)
    return jsonify(queue=queue, added=product)


# ─────────────────────────────────────────────────────────────────────────────
# Guide page — AEM-style TOC browser that validates a selected PROD PDF.
# Pick a product, click a TOC entry, see that section's content / images / tables
# rendered, with its STAGE validation status.
# ─────────────────────────────────────────────────────────────────────────────
_GUIDE_CACHE: dict = {}
_GUIDE_LOCK = threading.Lock()


def _guide_data(product: str):
    """Build (and cache) the TOC + per-side data for a product's PROD/STAGE pair."""
    with _GUIDE_LOCK:
        if product in _GUIDE_CACHE:
            return _GUIDE_CACHE[product]
    match = next((m for m in _matched_products() if m["product"] == product), None)
    if not match:
        return None
    prod_path = BENQ_PDFS_DIR / product / match["pdf_name"]
    if not prod_path.is_file():
        return None

    import fitz
    v = validate_toc_content
    toc = v.get_toc(str(prod_path))
    prod_sections = v.extract_sections(str(prod_path), is_prod=True)

    # STAGE side (optional — present only when a Final Cleanup file matched).
    stage_path = None
    stage_index = None
    if match["zip_name"]:
        sdir = (PAIRS_DIR / "_guide_stage" / re.sub(r"[^\w.\-]", "_", product))
        sdir.mkdir(parents=True, exist_ok=True)
        try:
            stage_path = _extract_stage_pdf(CLEANUP_DIR / match["source"] / match["zip_name"], sdir)
            sdoc = fitz.open(str(stage_path))
            stage_nav = {1} | v._detect_nav_pages(sdoc)
            sdoc.close()
            stage_index = v._build_stage_index(str(stage_path), stage_nav)
        except Exception as exc:
            print(f"[guide] stage build failed for {product}: {exc}", flush=True)
            stage_path, stage_index = None, None

    data = {
        "prod_path": str(prod_path),
        "stage_path": str(stage_path) if stage_path else None,
        "toc": toc,
        "prod_sections": prod_sections,
        "stage_index": stage_index,
        "pdf_name": match["pdf_name"],
        "has_stage": stage_index is not None,
    }
    with _GUIDE_LOCK:
        _GUIDE_CACHE[product] = data
    return data


@app.route("/guide", methods=["GET"])
def guide_page():
    return render_template("guide.html")


@app.route("/guide/toc/<path:product>", methods=["GET"])
def guide_toc(product: str):
    data = _guide_data(product)
    if not data:
        return jsonify(error="Unknown product or PROD PDF missing."), 404
    toc = [{"idx": i, "level": lvl, "title": title, "page": pg}
           for i, (lvl, title, pg) in enumerate(data["toc"])]
    return jsonify(product=product, pdf_name=data["pdf_name"],
                   has_stage=data["has_stage"], toc=toc)


@app.route("/guide/section/<path:product>/<int:idx>", methods=["GET"])
def guide_section(product: str, idx: int):
    import base64
    import fitz
    data = _guide_data(product)
    if not data:
        return jsonify(error="Unknown product."), 404
    toc = data["toc"]
    if idx < 0 or idx >= len(toc):
        return jsonify(error="Bad section index."), 404
    level, title, page = toc[idx]
    next_page = toc[idx + 1][2] if idx + 1 < len(toc) else None

    v = validate_toc_content
    section_text = data["prod_sections"].get(title, "")

    # Validation status against STAGE.
    status, coverage, missing = "no-stage", None, []
    if data["stage_index"] is not None:
        stage_ns, stage_cset, stage_full_lower = data["stage_index"]
        prod_words = v._keep(v._tokenize(section_text))
        if not prod_words:
            # Parent/heading-only entry — its body lives under sub-sections.
            status = "empty"
        else:
            coverage, missing = v._section_missing(prod_words, stage_ns, stage_cset, stage_full_lower)
            status = "pass" if not missing else "fail"

    # Per-issue cropped screenshots — only rendered when there is an issue to
    # show, so a passing section costs nothing.
    issue_shots = []
    if status == "fail" and missing:
        try:
            for sh in issue_shots_mod.build_section_shots(
                    data["prod_path"], data.get("stage_path"), missing,
                    page, title):
                issue_shots.append({
                    "fragment": sh["fragment"],
                    "comment": sh["comment"],
                    "prod_page": sh["prod_page"],
                    "stage_page": sh["stage_page"],
                    "prod_img": _png_uri(sh["prod_png"]),
                    "stage_img": _png_uri(sh["stage_png"]),
                })
        except Exception as exc:
            print(f"[guide] issue screenshots failed for {title}: {exc}", flush=True)

    # Pull tables/image counts from the section's PROD pages.
    doc = fitz.open(data["prod_path"])
    last = (next_page - 1) if next_page and next_page > page else page
    last = min(last, page + 5)            # cap very long sections
    tables, n_images = [], 0
    for pno in range(page, min(last, doc.page_count) + 1):
        pg = doc[pno - 1]
        n_images += len(pg.get_images())
        try:
            for tb in pg.find_tables().tables:
                rows = [[("" if c is None else str(c)) for c in row] for row in tb.extract()]
                if rows:
                    tables.append(rows)
        except Exception:
            pass
    doc.close()

    return jsonify(
        title=title, level=level, page=page,
        status=status, coverage=coverage,
        missing=missing[:30],
        text=section_text[:8000],
        image_count=n_images, tables=tables, issue_shots=issue_shots,
    )


def _png_uri(png: bytes | None) -> str | None:
    """base64 data URI for a PNG blob, or None when there is no image."""
    import base64
    return ("data:image/png;base64," + base64.b64encode(png).decode()) if png else None


# ── Q&A Index cache — holds the last validate-all result per product ────────
_QA_CACHE: dict = {}
_QA_LOCK = threading.Lock()


@app.route("/guide/validate-all/<path:product>", methods=["GET"])
def guide_validate_all(product: str):
    """Validate ALL TOC sections at once against the STAGE PDF.

    Returns a comprehensive report with per-section status (pass/fail/skip),
    coverage percentage, missing fragments, and overall summary.
    """
    data = _guide_data(product)
    if not data:
        return jsonify(error="Unknown product or PROD PDF missing."), 404

    v = validate_toc_content
    toc = data["toc"]
    sections = data["prod_sections"]
    stage_index = data["stage_index"]

    rows = []
    for i, (lvl, title, pg) in enumerate(toc):
        section_text = sections.get(title, "")
        row = {
            "idx": i, "title": title, "level": lvl, "page": pg,
        }
        if stage_index is None:
            row.update(status="no-stage", coverage_pct=None,
                       missing_count=0, missing_sample=[],
                       note="No STAGE PDF to compare against")
        else:
            stage_ns, stage_cset, stage_full_lower = stage_index
            prod_words = v._keep(v._tokenize(section_text))
            if not prod_words:
                row.update(status="skip", coverage_pct=None,
                           missing_count=0, missing_sample=[],
                           note="No body text under this heading in the PDF")
            else:
                coverage, missing = v._section_missing(
                    prod_words, stage_ns, stage_cset, stage_full_lower)
                row.update(
                    status="pass" if not missing else "fail",
                    coverage_pct=round(coverage, 2),
                    missing_count=len(missing),
                    missing_sample=missing[:30],
                )
        rows.append(row)

    graded = [r for r in rows if r["status"] in ("pass", "fail")]
    passed = sum(1 for r in graded if r["status"] == "pass")
    skipped = sum(1 for r in rows if r["status"] == "skip")
    no_stage = sum(1 for r in rows if r["status"] == "no-stage")
    toc_status = "complete" if all(
        r["status"] in ("pass", "skip") for r in rows if r["status"] != "no-stage"
    ) else "incomplete"

    summary = {
        "total": len(graded),
        "passed": passed,
        "failed": len(graded) - passed,
        "skipped": skipped,
        "no_stage": no_stage,
        "toc_status": toc_status,
    }

    result = {
        "product": product,
        "pdf_name": data["pdf_name"],
        "has_stage": data["has_stage"],
        "toc_status": toc_status,
        "summary": summary,
        "sections": rows,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # Cache for the report endpoint
    with _QA_LOCK:
        _QA_CACHE[product] = result

    return jsonify(result)


@app.route("/guide/validate-vs-site/<path:product>", methods=["POST"])
def guide_validate_vs_site(product: str):
    """Validate the product's PDF TOC sections against a crawled AEM site URL.

    Reuses the sites-validation shingle matcher but returns the result in the
    same format as validate-all for consistent rendering in the Guide page.
    """
    from content_validation import validate_toc_content as vtc

    data = _guide_data(product)
    if not data:
        return jsonify(error="Unknown product or PROD PDF missing."), 404

    req = request.get_json() or {}
    url = (req.get("url") or "").strip()
    if not url:
        return jsonify(error="Author URL is required."), 400

    if not _has_aem_session():
        return jsonify(error="Not signed in to AEM. Please sign in from the "
                             "Sites Validation page first."), 401

    # 1. Crawl the site pages
    try:
        pages = _aem_crawl_pages(url)
    except Exception as e:
        return jsonify(error=f"Failed to crawl the author URL: {e}"), 400
    if not pages:
        return jsonify(error="No pages found under the author URL."), 422

    def _fetch_page(pg):
        try:
            html = _fetch_html(pg["url"], timeout=20)
            return {**pg, "html": html, "text": _page_main_text(html)}
        except Exception as e:
            return {**pg, "html": "", "text": "", "error": str(e)}

    workers = min(12, max(1, len(pages)))
    fetched = [None] * len(pages)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_page, p): i for i, p in enumerate(pages)}
        for fut in as_completed(futs):
            fetched[futs[fut]] = fut.result()

    site_parts = [p["text"] for p in fetched if p.get("text")]
    if not site_parts:
        return jsonify(error="Could not read any text from the crawled pages."), 422

    # 2. Build shingle index from crawled text
    ns, cset, full_lower = _build_site_stage_index(site_parts)
    site_by_key = {}
    for p in fetched:
        site_by_key[vtc._norm_key(p["title"])] = (p.get("url"), p.get("text", ""))

    # 3. Validate each TOC section
    toc = data["toc"]
    sections = data["prod_sections"]
    rows = []
    matches = _match_toc_to_site_pages(toc, sections, fetched)
    for i, m in enumerate(matches):
        title = m["nav_item"]
        sec_text = sections.get(title, "")
        prod_words = sec_text.split()
        sec_url = m["url"]
        sec_lower = m["sec_lower"]
        on_site = m["on_site"]
        lvl, _, pg = toc[i]

        row = {
            "idx": i, "title": title, "level": lvl, "page": pg,
            "url": sec_url, "on_site": on_site,
        }
        
        title_words = vtc._keep(vtc._tokenize(title))
        is_title_only = (len(prod_words) <= len(title_words))

        if not prod_words:
            row.update(status="skip", coverage_pct=None,
                       missing_count=0, missing_sample=[],
                       note="No body text under this heading in the PDF")
        elif is_title_only and on_site:
            row.update(
                status="pass",
                coverage_pct=100.0,
                missing_count=0,
                missing_sample=[],
            )
        else:
            cov, missing = vtc._section_missing(
                prod_words, ns, cset, full_lower, sec_lower)
            status = "pass" if not missing else "fail"
            row.update(
                status=status,
                coverage_pct=round(cov, 2),
                missing_count=len(missing),
                missing_sample=missing[:30],
            )
            if not on_site:
                row["note"] = "No matching site page found by title"
            elif status != "pass":
                if m["match_type"] == "parent":
                    row["note"] = f"Matched to parent page: {m['match_page_title']}"
                elif m["match_type"] == "substring":
                    row["note"] = f"Heading found inside page: {m['match_page_title']}"
        rows.append(row)

    graded = [r for r in rows if r["status"] in ("pass", "fail")]
    passed = sum(1 for r in graded if r["status"] == "pass")
    skipped = sum(1 for r in rows if r["status"] == "skip")
    toc_status = "complete" if all(
        r["status"] in ("pass", "skip") for r in rows
    ) else "incomplete"

    summary = {
        "total": len(graded),
        "passed": passed,
        "failed": len(graded) - passed,
        "skipped": skipped,
        "toc_status": toc_status,
        "pages_crawled": len(pages),
    }

    result = {
        "product": product,
        "pdf_name": data["pdf_name"],
        "url": url,
        "toc_status": toc_status,
        "summary": summary,
        "sections": rows,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    with _QA_LOCK:
        _QA_CACHE[product + "__site"] = result

    return jsonify(result)


def _shot_flowables(gdata, row, esc, cell_style, miss_style, limit: int = 2):
    """Side-by-side PROD/STAGE issue crops for one report section.

    Kept to `limit` issues per section so a badly-regressed section cannot push
    the report to hundreds of pages.  Returns [] when screenshots are
    unavailable (site mode, missing PDFs, or nothing locatable on a page).
    """
    from reportlab.lib import colors
    from reportlab.lib.units import inch
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Image, Paragraph, Spacer, Table, TableStyle, KeepTogether

    if not gdata or not gdata.get("prod_path"):
        return []
    try:
        shots = issue_shots_mod.build_section_shots(
            gdata["prod_path"], gdata.get("stage_path"),
            row.get("missing_sample") or [], row.get("page") or 1,
            row.get("title") or "", limit=limit)
    except Exception as exc:
        print(f"[report] issue screenshots failed for {row.get('title')}: {exc}", flush=True)
        return []
    if not shots:
        return []

    cap = ParagraphStyle("shotcap", parent=cell_style, fontSize=7,
                         leading=9, textColor=colors.HexColor("#64748b"))
    note = ParagraphStyle("shotnote", parent=cell_style, fontSize=7.5, leading=10,
                          textColor=colors.HexColor("#7c2d12"))
    col_w = 4.6 * inch                      # landscape letter, two columns

    def _img(png):
        if not png:
            return Paragraph("<i>No matching location in STAGE — the whole "
                             "passage is absent.</i>", cap)
        img = Image(io.BytesIO(png))
        img.drawHeight = img.drawHeight * (col_w / img.drawWidth)
        img.drawWidth = col_w
        return img

    out = [Spacer(1, 4)]
    for sh in shots:
        tbl = Table([
            [Paragraph(f"<b>PROD p.{sh['prod_page']}</b> — missing content (red)", cap),
             Paragraph(f"<b>STAGE{(' p.' + str(sh['stage_page'])) if sh['stage_page'] else ''}</b>"
                       " — where it should be (amber)", cap)],
            [_img(sh["prod_png"]), _img(sh["stage_png"])],
        ], colWidths=[col_w + 6, col_w + 6])
        tbl.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#cbd5e1")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f8fafc")),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        out.append(KeepTogether([Paragraph("Issue: " + esc(sh["comment"]), note),
                                 Spacer(1, 3), tbl, Spacer(1, 8)]))
    return out


def is_site_state(state: dict) -> bool:
    """A site-crawl result has no STAGE PDF, so it gets no screenshots."""
    return "url" in state


@app.route("/guide/qa-report/<path:product>", methods=["GET"])
def guide_qa_report(product: str):
    """Generate and return a downloadable PDF report for the Q&A Index results."""
    # Try the site variant first (most recent), then the STAGE variant
    with _QA_LOCK:
        state = _QA_CACHE.get(product + "__site") or _QA_CACHE.get(product)
    if not state:
        return jsonify(error="Run 'Validate All' first — no report cached."), 404

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.lib.units import inch
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, Image, KeepTogether)
    from reportlab.lib.utils import ImageReader

    # Issue screenshots need the source PDFs; the site variant has no STAGE PDF.
    gdata = None if is_site_state(state) else _guide_data(state.get("product") or product)

    bio = io.BytesIO()
    doc = SimpleDocTemplate(bio, pagesize=landscape(letter),
                            leftMargin=0.4 * inch, rightMargin=0.4 * inch,
                            topMargin=0.4 * inch, bottomMargin=0.4 * inch)
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontSize=16, spaceAfter=4)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontSize=9, textColor=colors.grey)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontSize=8, leading=11)
    miss = ParagraphStyle("miss", parent=ss["Normal"], fontSize=8, leading=11,
                          textColor=colors.HexColor("#b91c1c"))
    hdr = ParagraphStyle("hdr", parent=ss["Normal"], fontSize=9,
                         textColor=colors.whitesmoke, fontName="Helvetica-Bold")

    def esc(s):
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    s = state["summary"]
    toc_st = state.get("toc_status", "—")
    is_site = "url" in state

    story = [
        Paragraph("Q&A Index — Content Validation Report", h1),
        Paragraph(f"Product: {esc(state.get('product'))}  ·  PDF: {esc(state.get('pdf_name'))}", sub),
    ]
    if is_site:
        story.append(Paragraph(f"Site URL: {esc(state.get('url'))}", sub))
    story += [
        Paragraph(f"Generated: {esc(state.get('generated'))}  ·  "
                  f"TOC Status: <b>{esc(toc_st.upper())}</b>", sub),
        Spacer(1, 10),
    ]

    # Summary metrics table
    met_headers = ["TOC Sections", "Passed", "Failed", "Skipped", "TOC Status"]
    if is_site:
        met_headers.append("Pages Crawled")
    met_hdr = [Paragraph(f"<b>{h}</b>", hdr) for h in met_headers]
    met_vals = [
        Paragraph(str(s["total"]), cell),
        Paragraph(str(s["passed"]), cell),
        Paragraph(str(s["failed"]), miss if s["failed"] else cell),
        Paragraph(str(s.get("skipped", 0)), cell),
        Paragraph(toc_st.upper(), cell),
    ]
    if is_site:
        met_vals.append(Paragraph(str(s.get("pages_crawled", "—")), cell))
    met = Table([met_hdr, met_vals], repeatRows=1)
    met.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0ea5a4")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [met, Spacer(1, 14)]

    # Per-section detail (flowing paragraphs to avoid table cell overflow)
    item_hdr = ParagraphStyle("ihdr", parent=ss["Normal"], fontSize=9.5,
                              leading=13, fontName="Helvetica-Bold", spaceBefore=8)
    frag_s = ParagraphStyle("frag", parent=ss["Normal"], fontSize=8, leading=11,
                            leftIndent=10, textColor=colors.HexColor("#b91c1c"))
    sev = {"PASS": "#166534", "FAIL": "#991b1b", "SKIP": "#854d0e",
           "NO-STAGE": "#854d0e"}

    story.append(Paragraph("<b>Per-section detail</b>", sub))
    story.append(Spacer(1, 4))

    for i, r in enumerate(state["sections"], 1):
        status = (r.get("status") or "skip").upper()
        cov = f"{r.get('coverage_pct')}%" if r.get("coverage_pct") is not None else "—"
        story.append(Paragraph(
            f'{i}. {esc(r.get("title"))} '
            f'<font color="{sev.get(status, "#334155")}">[{status}]</font> '
            f'<font color="#334155">· coverage {cov} · page {r.get("page", "?")}</font>',
            item_hdr))
        if r.get("note"):
            story.append(Paragraph(esc(r["note"]),
                                   ParagraphStyle("meta", parent=ss["Normal"],
                                                  fontSize=7.5, leading=10,
                                                  textColor=colors.HexColor("#64748b"))))
        elif r.get("missing_sample"):
            ms = r["missing_sample"]
            story.append(Paragraph(
                f"<b>{r.get('missing_count', len(ms))} missing fragment(s):</b>", miss))
            for frag in ms:
                story.append(Paragraph("• " + esc(frag), frag_s))
            story += _shot_flowables(gdata, r, esc, cell, miss)
        elif status == "PASS":
            story.append(Paragraph("✓ All content present.", cell))

    doc.build(story)
    bio.seek(0)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    mode = "site" if is_site else "stage"
    return send_file(bio, mimetype="application/pdf", as_attachment=True,
                     download_name=f"qa_index_{product}_{mode}_{stamp}.pdf")


@app.route("/download/all-prod-pdfs", methods=["GET"])
def download_all_prod_pdfs():
    """Zip every listed product's PROD PDF (one file per product, friendly names)."""
    products = _matched_products()
    if not products:
        return jsonify(error="No PROD PDFs available yet."), 404
    bio = io.BytesIO()
    with zipfile.ZipFile(bio, "w", zipfile.ZIP_DEFLATED) as zf:
        for m in products:
            src = BENQ_PDFS_DIR / m["product"] / m["pdf_name"]
            if src.is_file():
                zf.write(src, m["pdf_dl"])
    bio.seek(0)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(bio, mimetype="application/zip", as_attachment=True,
                     download_name=f"prod_pdfs_{stamp}.zip")


@app.route("/download/prod-pdf/<path:product>", methods=["GET"])
def download_prod_pdf(product: str):
    for m in _matched_products():
        if m["product"] == product:
            f = BENQ_PDFS_DIR / product / m["pdf_name"]
            if f.is_file():
                return send_file(f, mimetype="application/pdf",
                                 as_attachment=True, download_name=m["pdf_dl"])
    return jsonify(error="Product PDF not found."), 404


@app.route("/download/cleanup-zip/<path:product>", methods=["GET"])
def download_cleanup_zip(product: str):
    for m in _matched_products():
        if m["product"] == product and m["zip_name"]:
            f = CLEANUP_DIR / m["source"] / m["zip_name"]
            if f.is_file():
                return send_file(f, mimetype="application/zip",
                                 as_attachment=True, download_name=m["zip_name"])
    return jsonify(error="Cleanup zip not found."), 404


# ─────────────────────────────────────────────────────────────────────────────
# Sites Validation page
# ─────────────────────────────────────────────────────────────────────────────
# Holds the most recent sites-validation run (with the FULL per-item missing-token
# lists) so the downloadable PDF report can contain every issue, not just the
# 30-item sample shown in the UI.
LAST_SITES_RESULT: dict = {"data": None}


@app.route("/sites-validation", methods=["GET"])
def sites_validation_page():
    return render_template("sites-validation.html")

import urllib.request
import urllib.parse
import urllib.error
import http.cookiejar
from html.parser import HTMLParser

# ── AEM session store ────────────────────────────────────────────────────────
# Cookies from a successful AEM login are persisted here so the validation
# fetches run as an authenticated user across requests / restarts.
SESSION_DIR = BASE_DIR / ".sessions"
SESSION_DIR.mkdir(exist_ok=True)
AEM_COOKIE_FILE = SESSION_DIR / "aem_cookies.txt"
AEM_META_FILE = SESSION_DIR / "aem_session.json"
AEM_SESSION_TTL = 24 * 60 * 60  # seconds — keep the session for 24 hours
AEM_SESSION = {"base_url": None}  # remembers the AEM host for relative logins


def _save_session_meta(base_url, auth=None):
    AEM_META_FILE.write_text(json.dumps(
        {"base_url": base_url, "auth": auth, "created_at": time.time()}))


def _load_session_meta():
    try:
        return json.loads(AEM_META_FILE.read_text())
    except Exception:
        return None


def _aem_opener():
    """Build a urllib opener backed by the persisted AEM cookie jar, plus the
    stored HTTP Basic credentials so authoring requests are authenticated."""
    jar = http.cookiejar.MozillaCookieJar(str(AEM_COOKIE_FILE))
    if AEM_COOKIE_FILE.is_file():
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception:
            pass
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    headers = [("User-Agent", "Mozilla/5.0")]
    meta = _load_session_meta() or {}
    if meta.get("auth"):
        headers.append(("Authorization", "Basic " + meta["auth"]))
    opener.addheaders = headers
    return opener, jar


def _has_aem_session():
    # The session is valid while a Basic-auth token is stored and within the
    # 24h TTL. (AEM author here uses HTTP Basic auth, not form login.)
    meta = _load_session_meta()
    if not meta or not meta.get("auth"):
        return False
    return (time.time() - meta.get("created_at", 0)) <= AEM_SESSION_TTL


class _TextParser(HTMLParser):
    """Collect visible text, ignoring script/style/head noise."""
    _SKIP = {"script", "style", "noscript", "head", "title"}

    def __init__(self):
        super().__init__()
        self.text = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0 and data.strip():
            self.text.append(data.strip())

    def get_text(self):
        return " ".join(self.text)


class _NavParser(HTMLParser):
    """Extract left-nav items: anchors living inside a nav/aside/sidebar/toc
    container. Each item is {text, href}."""
    _NAV_TAGS = {"nav", "aside"}
    _NAV_HINT = ("nav", "sidebar", "side-bar", "toc", "menu", "tree")

    def __init__(self):
        super().__init__()
        self.items = []
        self._nav_depth = 0          # >0 while inside a nav container
        self._in_a = False
        self._cur_href = None
        self._cur_text = []

    @staticmethod
    def _is_nav_container(tag, attrs):
        if tag in _NavParser._NAV_TAGS:
            return True
        ad = dict(attrs)
        blob = " ".join(filter(None, (ad.get("class"), ad.get("id"),
                                       ad.get("role")))).lower()
        return any(h in blob for h in _NavParser._NAV_HINT)

    def handle_starttag(self, tag, attrs):
        if self._is_nav_container(tag, attrs):
            self._nav_depth += 1
        if self._nav_depth > 0 and tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._in_a = True
                self._cur_href = href
                self._cur_text = []

    def handle_startendtag(self, tag, attrs):
        # self-closing nav containers carry no children; ignore
        pass

    def handle_endtag(self, tag):
        if self._in_a and tag == "a":
            text = " ".join(self._cur_text).strip()
            if text:
                self.items.append({"text": text, "href": self._cur_href})
            self._in_a = False
            self._cur_href = None
            self._cur_text = []
        if self._is_nav_container_close(tag) and self._nav_depth:
            self._nav_depth -= 1

    def _is_nav_container_close(self, tag):
        # We can't see attrs on close; treat the structural nav/aside tags as
        # the things that decrement depth. Hint-based <div> containers are not
        # decremented precisely, but anchors are still captured while depth>0.
        return tag in self._NAV_TAGS

    def handle_data(self, data):
        if self._in_a and data.strip():
            self._cur_text.append(data.strip())


class _LinkParser(HTMLParser):
    """Extract ALL anchors on the page regardless of container. Each item is {text, href}."""
    def __init__(self):
        super().__init__()
        self.items = []
        self._in_a = False
        self._cur_href = None
        self._cur_text = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._in_a = True
                self._cur_href = href
                self._cur_text = []

    def handle_endtag(self, tag):
        if self._in_a and tag == "a":
            text = " ".join(self._cur_text).strip()
            self.items.append({"text": text, "href": self._cur_href})
            self._in_a = False
            self._cur_href = None
            self._cur_text = []

    def handle_data(self, data):
        if self._in_a and data.strip():
            self._cur_text.append(data.strip())


def _fetch_html(url, timeout=15):
    opener, _ = _aem_opener()
    with opener.open(url, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="ignore")


@app.route("/sites-validation/session", methods=["GET"])
def sites_validation_session():
    """Report whether a saved AEM session exists (for the header UI)."""
    authed = _has_aem_session()
    meta = _load_session_meta() or {}
    remaining = None
    if authed and meta.get("created_at"):
        remaining = max(0, int(AEM_SESSION_TTL - (time.time() - meta["created_at"])))
    return jsonify(authenticated=authed,
                   base_url=meta.get("base_url") or AEM_SESSION.get("base_url"),
                   expires_in=remaining)


@app.route("/sites-validation/login", methods=["POST"])
def sites_validation_login():
    """Authenticate against AEM and persist the session cookies.

    Posts the credentials to AEM's form-login endpoint (/j_security_check) and
    stores the returned login-token cookie in the on-disk cookie jar."""
    req = request.get_json() or {}
    aem_url = (req.get("aem_url") or "").strip()
    username = (req.get("username") or "").strip()
    password = req.get("password") or ""
    if not aem_url or not username or not password:
        return jsonify(error="AEM URL, username and password are required."), 400

    parsed = urllib.parse.urlparse(aem_url)
    if not parsed.scheme or not parsed.netloc:
        return jsonify(error="Enter a full AEM URL, e.g. https://author.benq.com"), 400
    base = f"{parsed.scheme}://{parsed.netloc}"

    # AEM author here authenticates with HTTP Basic auth. Validate the
    # credentials against the current-user endpoint and store the token.
    import base64
    token = base64.b64encode(f"{username}:{password}".encode()).decode()

    jar = http.cookiejar.MozillaCookieJar(str(AEM_COOKIE_FILE))
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.addheaders = [("User-Agent", "Mozilla/5.0"),
                         ("Authorization", "Basic " + token)]

    # Endpoints that require a valid login; first that returns 200 confirms auth.
    check_paths = [
        "/libs/granite/security/currentuser.json",
        "/libs/cq/security/userinfo.json",
        "/",
    ]
    ok = False
    last_err = None
    for path in check_paths:
        try:
            with opener.open(base + path, timeout=15) as resp:
                if resp.status == 200:
                    ok = True
                    break
        except urllib.error.HTTPError as he:
            last_err = he
            if he.code in (401, 403):
                # credentials rejected — stop, report auth failure
                return jsonify(error="Authentication failed — check the "
                                     "username and password."), 401
        except Exception as e:
            last_err = e

    if not ok:
        return jsonify(error=f"Login request failed: {last_err}"), 502

    # Persist cookies (if any) and the Basic token for the 24h session.
    try:
        jar.save(ignore_discard=True, ignore_expires=True)
    except Exception:
        pass
    _save_session_meta(base, auth=token)
    AEM_SESSION["base_url"] = base
    return jsonify(authenticated=True, base_url=base,
                   expires_in=AEM_SESSION_TTL,
                   message=f"Signed in to {base} as {username}. "
                           f"Session saved for 24 hours.")


@app.route("/sites-validation/logout", methods=["POST"])
def sites_validation_logout():
    for f in (AEM_COOKIE_FILE, AEM_META_FILE):
        try:
            if f.is_file():
                f.unlink()
        except Exception:
            pass
    AEM_SESSION["base_url"] = None
    return jsonify(authenticated=False)


def _page_text(html):
    p = _TextParser()
    p.feed(html)
    return p.get_text()


# Strip navigation chrome (header / footer / nav / sidebar) so only the article
# body text is compared — header links, breadcrumbs, "Next/Previous" etc. are
# not part of the manual content and would otherwise create false mismatches.
_CHROME_RE = re.compile(
    r"<(header|footer|nav|aside|script|style|noscript)(\s[^>]*)?>.*?</\1\s*>",
    re.S | re.I,
)


def _page_main_text(html: str) -> str:
    """Visible body text of a page with nav/header/footer chrome removed."""
    return _page_text(_CHROME_RE.sub("", html))


def _has_heading_in_html(html: str, title: str) -> bool:
    """Check if the HTML page contains a heading matching the given title."""
    from content_validation import validate_toc_content as vtc
    target_norm = vtc._norm_key(title)
    
    headings = []
    # Locate all h1-h6 headings
    for m in re.finditer(r'<h([1-6])\b([^>]*)>(.*?)</h\1>', html, re.S | re.I):
        inner_html = m.group(3)
        heading_text = re.sub(r'<[^>]*>', '', inner_html).strip()
        headings.append({
            "title": heading_text,
            "norm_title": vtc._norm_key(heading_text)
        })
        
    for h in headings:
        if h["norm_title"] == target_norm:
            return True
            
    target_phrase = vtc._s_norm(title).lower()
    if len(target_phrase) > 4:
        for h in headings:
            if target_phrase in vtc._s_norm(h["title"]).lower():
                return True
                
    return False


def _extract_html_section_text(html: str, title: str) -> tuple[str, str | None]:
    """Extract page text under a specific heading inside the HTML page,

    up to the next heading of the same or higher hierarchical level.
    Returns a tuple: (sliced_text_content, heading_id).
    """
    from content_validation import validate_toc_content as vtc
    
    headings = []
    # Locate all h1-h6 headings
    for m in re.finditer(r'<h([1-6])\b([^>]*)>(.*?)</h\1>', html, re.S | re.I):
        tag_num = int(m.group(1))
        attrs = m.group(2)
        inner_html = m.group(3)
        heading_text = re.sub(r'<[^>]*>', '', inner_html).strip()
        
        # Extract ID from attrs
        heading_id = None
        id_m = re.search(r'\bid\s*=\s*(?:\\?["\']|\\?&quot;|\\?&apos;|\\)*([a-zA-Z0-9_-]+)', attrs)
        if id_m:
            heading_id = id_m.group(1)
            
        headings.append({
            "level": tag_num,
            "title": heading_text,
            "norm_title": vtc._norm_key(heading_text),
            "start": m.start(),
            "end": m.end(),
            "id": heading_id
        })
        
    if not headings:
        return _page_main_text(html), None
        
    target_norm = vtc._norm_key(title)
    match_idx = -1
    for idx, h in enumerate(headings):
        if h["norm_title"] == target_norm:
            match_idx = idx
            break
            
    if match_idx == -1:
        target_phrase = vtc._s_norm(title).lower()
        if len(target_phrase) > 4:
            for idx, h in enumerate(headings):
                if target_phrase in vtc._s_norm(h["title"]).lower():
                    match_idx = idx
                    break
                    
    if match_idx == -1:
        return _page_main_text(html), None
        
    matched_h = headings[match_idx]
    start_pos = matched_h["start"]
    heading_id = matched_h["id"]
    
    end_pos = len(html)
    for next_h in headings[match_idx + 1:]:
        if next_h["level"] <= matched_h["level"]:
            end_pos = next_h["start"]
            break
            
    section_html = html[start_pos:end_pos]
    return _page_main_text(section_html), heading_id


def _match_toc_to_site_pages(toc, sections, fetched):
    """Map PDF TOC headings to crawled site pages using title matches,

    parent-child hierarchy fallbacks, or substring searches in page body text.
    Returns list of dicts: {nav_item, url, on_site, sec_lower, match_type, match_page_title}.
    """
    from content_validation import validate_toc_content as vtc
    import urllib.parse
    
    normalized_pages = []
    for p in fetched:
        p_text = p.get("text", "")
        p_url = p.get("url")
        p_title = p.get("title", "")
        p_html = p.get("html", "")
        normalized_pages.append({
            "title": p_title,
            "url": p_url,
            "text": p_text,
            "html": p_html,
            "norm_text": vtc._s_norm(p_text).lower(),
            "norm_title": vtc._norm_key(p_title)
        })

    ancestors = []
    matched_results = []

    for lvl, title, _pg in toc:
        while ancestors and ancestors[-1][0] >= lvl:
            ancestors.pop()

        title_key = vtc._norm_key(title)
        match_page = next((p for p in normalized_pages if p["norm_title"] == title_key), None)
        match_type = "direct"

        # Fallback 1: Inherit from parent heading (prefer hierarchy)
        if not match_page and ancestors:
            parent_lvl, parent_url, parent_text, parent_html, parent_title = ancestors[-1]
            if parent_url:
                match_page = {
                    "url": parent_url,
                    "text": parent_text,
                    "html": parent_html,
                    "title": parent_title
                }
                match_type = "parent"

        # Fallback 2: Heading search in page HTML (if heading is a section within a page)
        if not match_page:
            for p in normalized_pages:
                if p["html"] and _has_heading_in_html(p["html"], title):
                    match_page = p
                    match_type = "substring"
                    break

        if match_page:
            sec_url = match_page["url"]
            sec_html = match_page.get("html", "")
            heading_id = None
            
            # Isolate the section content if HTML is available
            if sec_html:
                sec_text_content, heading_id = _extract_html_section_text(sec_html, title)
            else:
                sec_text_content = match_page["text"]
                
            sec_lower = vtc._s_norm(re.sub(r"\s+", " ", sec_text_content)).lower()
            on_site = True
            
            # If heading_id is found, append it as anchor to URL (replacing any existing fragment)
            if heading_id and sec_url:
                base_url = sec_url.split("#")[0]
                sec_url = f"{base_url}#{heading_id}"
                    
            ancestors.append((lvl, sec_url, sec_text_content, sec_html, title))
        else:
            sec_url = None
            sec_lower = ""
            on_site = False
            ancestors.append((lvl, None, None, "", title))

        matched_results.append({
            "nav_item": title,
            "url": sec_url,
            "on_site": on_site,
            "sec_lower": sec_lower,
            "match_type": match_type if match_page else "none",
            "match_page_title": match_page["title"] if match_page else None
        })

    return matched_results


def _build_site_stage_index(text_parts: list):
    """Build (nospace, shingle_set, full_lower) from all crawled site pages.

    Mirrors validate_toc_content._build_stage_index but takes already-extracted
    site text instead of a STAGE PDF, so the proven shingle matcher can run the
    site as the "stage" against the PDF's TOC sections."""
    from content_validation import validate_toc_content as vtc
    all_words = []
    for body in text_parts:
        all_words += vtc._keep(vtc._tokenize(body))
    nospace = "".join(vtc._canon(w) for w in all_words)
    
    # Dynamically determine if CJK characters are dominant
    is_cjk = False
    if vtc._CJK_RE.search(nospace):
        is_cjk = True
    L = 8 if is_cjk else vtc.CHAR_SHINGLE

    cset = {nospace[i:i + L] for i in range(len(nospace) - L + 1)}
    full_lower = vtc._s_norm(re.sub(r"\s+", " ", " ".join(text_parts))).lower()
    return nospace, cset, full_lower


def _resolve_ref_pdf(pdf_sel, upload):
    """Return (pdf_path, display_name, tmp_to_cleanup) for the reference PDF.

    TOC-based validation needs exactly one document, so an uploaded PDF (saved
    to a temp file) takes precedence; otherwise a single selected product PDF."""
    if upload is not None and getattr(upload, "filename", ""):
        tmp = Path(tempfile.mkdtemp(prefix="sites_ref_")) / secure_filename(upload.filename)
        upload.save(str(tmp))
        return str(tmp), upload.filename, tmp.parent
    if pdf_sel and pdf_sel != "all":
        match = next((m for m in _matched_products() if m["product"] == pdf_sel), None)
        if match:
            p = BENQ_PDFS_DIR / match["product"] / match["pdf_name"]
            if p.is_file():
                return str(p), match["pdf_name"], None
    return None, None, None


@app.route("/sites-validation/run", methods=["POST"])
def sites_validation_run():
    """TOC-based content validation of a published AEM site against its PDF.

    The PDF is the reference: for every TOC section we check that all of the
    section's body text is present on the crawled site (content only — no
    style / CSS). Uses the shingle matcher (validate_toc_content._section_missing)
    with its reorder / numbering false-positive guards for accuracy."""
    from content_validation import validate_toc_content as vtc

    # Accept multipart (with an optional uploaded PDF) or JSON.
    if request.files or request.form:
        pdf_sel = request.form.get("pdf")
        url = (request.form.get("url") or "").strip()
        upload = request.files.get("pdf_file")
    else:
        req = request.get_json() or {}
        pdf_sel = req.get("pdf")
        url = (req.get("url") or "").strip()
        upload = None
    if not url:
        return jsonify(error="Author URL is required."), 400

    if not _has_aem_session():
        return jsonify(error="Not signed in to AEM. Use the Sign in to AEM "
                             "panel in the header first."), 401

    # Resolve the single reference PDF (TOC validation needs one document).
    ref_path, ref_name, tmp_dir = _resolve_ref_pdf(pdf_sel, upload)
    if not ref_path:
        return jsonify(error="Select a specific PROD PDF (not “All PDFs”) or "
                             "upload one — TOC validation needs a single PDF."), 400

    try:
        # 1. Discover and fetch every page under the author URL.
        try:
            pages = _aem_crawl_pages(url)
        except Exception as e:
            return jsonify(error=f"Failed to crawl the author URL: {e}"), 400
        if not pages:
            return jsonify(error="No pages found under the author URL. The site "
                                 "may render navigation with JavaScript, or the "
                                 "URL path has no child pages."), 422

        def _fetch_page(pg):
            try:
                html = _fetch_html(pg["url"], timeout=20)
                return {**pg, "html": html, "text": _page_main_text(html)}
            except Exception as e:
                return {**pg, "html": "", "text": "", "error": str(e)}

        workers = min(12, max(1, len(pages)))
        fetched = [None] * len(pages)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_fetch_page, p): i for i, p in enumerate(pages)}
            for fut in as_completed(futs):
                fetched[futs[fut]] = fut.result()

        site_parts = [p["text"] for p in fetched if p.get("text")]
        if not site_parts:
            return jsonify(error="Could not read any text from the crawled pages."), 422

        # 2. Build the site shingle index + a per-title lookup for the
        #    section-scoped reorder guard.
        ns, cset, full_lower = _build_site_stage_index(site_parts)
        site_by_key = {}
        for p in fetched:
            site_by_key[vtc._norm_key(p["title"])] = (p.get("url"), p.get("text", ""))

        # 3. Extract the PDF's TOC sections (PROD = reference).
        toc = vtc.get_toc(ref_path)
        sections = vtc.extract_sections(ref_path, is_prod=True)
    finally:
        if tmp_dir:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    if not toc:
        return jsonify(error="The reference PDF has no usable table of contents."), 422

    # 4. Validate each TOC section's content against the site.
    matches = _match_toc_to_site_pages(toc, sections, fetched)
    rows, report_rows = [], []
    for m in matches:
        title = m["nav_item"]
        sec_text = sections.get(title, "")
        prod_words = sec_text.split()
        sec_url = m["url"]
        sec_lower = m["sec_lower"]
        on_site = m["on_site"]

        row = {"nav_item": title, "url": sec_url, "on_site": on_site}
        full_missing = []
        
        title_words = vtc._keep(vtc._tokenize(title))
        is_title_only = (len(prod_words) <= len(title_words))

        # Heading-only sections (no body text under the heading in the PDF — e.g.
        # parent/chapter nodes whose content lives in child sections) are still
        # validated, not skipped: we confirm the heading text itself is present
        # on the site. Only sections with literally nothing to check are skipped.
        heading_only = not prod_words
        check_words = prod_words if prod_words else title_words

        if not check_words:
            row.update(site_words=0, matched_words=0, coverage_pct=None,
                       status="skip", missing_count=0, missing_sample=[],
                       note="No text for this heading in the PDF")
        elif is_title_only and on_site:
            row.update(
                site_words=len(check_words),
                matched_words=len(check_words),
                coverage_pct=100.0,
                missing_count=0,
                missing_sample=[],
                status="pass",
            )
            if heading_only:
                row["note"] = "Heading-only section — heading found on site"
        else:
            cov, missing = vtc._section_missing(check_words, ns, cset,
                                                full_lower, sec_lower)
            full_missing = missing
            covered = round(len(check_words) * cov / 100.0)
            status = "pass" if not missing else "fail"
            row.update(
                site_words=len(check_words),         # PDF section word count
                matched_words=covered,
                coverage_pct=round(cov, 2),
                missing_count=len(missing),
                missing_sample=missing[:30],
                status=status,
            )
            if not on_site:
                row["note"] = ("Heading not found on the site"
                               if heading_only else
                               "No matching site page found by title")
            elif status != "pass":
                if m["match_type"] == "parent":
                    row["note"] = f"Matched to parent page: {m['match_page_title']}"
                elif m["match_type"] == "substring":
                    row["note"] = f"Heading found inside page: {m['match_page_title']}"
        rows.append(row)
        report_rows.append({**row, "missing_all": full_missing})

    graded = [r for r in rows if r["status"] in ("pass", "fail")]
    passed = sum(1 for r in graded if r["status"] == "pass")
    toc_status = ("complete" if all(r["status"] in ("pass", "skip")
                                    for r in rows) else "incomplete")
    summary = {"total": len(graded), "passed": passed,
               "failed": len(graded) - passed,
               "skipped": len(rows) - len(graded),
               "pages_crawled": len(pages),
               "toc_status": toc_status}

    state = {
        "kind": "toc", "url": url, "pdf_names": [ref_name],
        "summary": summary, "rows": report_rows,
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    LAST_SITES_RESULT["data"] = state
    LAST_SITES_RESULT["pdf_bytes"] = None
    threading.Thread(target=_prebuild_sites_report, args=(state,), daemon=True).start()

    return jsonify(
        url=url,
        pdf_names=[ref_name],
        summary=summary,
        results=rows,
        toc_status=toc_status,
        report_available=True,
    )


SITES_REPORT_LOCK = threading.Lock()


def _prebuild_sites_report(state: dict):
    """Build the report PDF off the request thread and cache its bytes."""
    try:
        data = _build_sites_report_pdf(state).getvalue()
    except Exception:
        traceback.print_exc()
        return
    with SITES_REPORT_LOCK:
        if LAST_SITES_RESULT.get("data") is state:   # still the current run
            LAST_SITES_RESULT["pdf_bytes"] = data


def _build_sites_report_pdf(state: dict) -> io.BytesIO:
    """Render a full sites-validation PDF report — every issue, not a sample."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter, landscape
    from reportlab.lib.units import inch
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle)

    bio = io.BytesIO()
    doc = SimpleDocTemplate(bio, pagesize=landscape(letter),
                            leftMargin=0.4 * inch, rightMargin=0.4 * inch,
                            topMargin=0.4 * inch, bottomMargin=0.4 * inch)
    ss = getSampleStyleSheet()
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontSize=16, spaceAfter=4)
    sub = ParagraphStyle("sub", parent=ss["Normal"], fontSize=9, textColor=colors.grey)
    cell = ParagraphStyle("cell", parent=ss["Normal"], fontSize=8, leading=11)
    miss = ParagraphStyle("miss", parent=ss["Normal"], fontSize=8, leading=11,
                          textColor=colors.HexColor("#b91c1c"))
    hdr = ParagraphStyle("hdr", parent=ss["Normal"], fontSize=9,
                         textColor=colors.whitesmoke, fontName="Helvetica-Bold")

    def esc(s):
        return (str(s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    s = state["summary"]
    rows_all = state["rows"]
    # ── Aggregate metrics across the whole TOC ──
    tot_words = sum((r.get("site_words") or 0) for r in rows_all)
    tot_covered = sum((r.get("matched_words") or 0) for r in rows_all)
    tot_missing = sum(len(r.get("missing_all") or []) for r in rows_all)
    overall_cov = round(100.0 * tot_covered / tot_words, 2) if tot_words else 0.0
    covs = [r["coverage_pct"] for r in rows_all if r.get("coverage_pct") is not None]
    avg_cov = round(sum(covs) / len(covs), 2) if covs else 0.0

    story = [
        Paragraph("Sites Content Validation Report (TOC-based)", h1),
        Paragraph(f"Author URL: {esc(state.get('url'))}", sub),
        Paragraph(f"Reference PDF: {esc(', '.join(state.get('pdf_names') or []) or '—')}", sub),
        Paragraph(f"Pages crawled: {s.get('pages_crawled', '—')} &nbsp;·&nbsp; "
                  f"Generated: {esc(state.get('generated'))}", sub),
        Spacer(1, 10),
    ]

    # Metrics summary table (TOC-level).
    met_hdr = [Paragraph(f"<b>{h}</b>", hdr) for h in
               ["TOC sections", "Passed", "Failed", "Skipped",
                "Overall coverage", "Avg coverage", "Missing fragments", "PDF words"]]
    met_row = [Paragraph(str(s["total"]), cell), Paragraph(str(s["passed"]), cell),
               Paragraph(str(s["failed"]), miss if s["failed"] else cell),
               Paragraph(str(s.get("skipped", 0)), cell),
               Paragraph(f"{overall_cov}%", cell), Paragraph(f"{avg_cov}%", cell),
               Paragraph(str(tot_missing), miss if tot_missing else cell),
               Paragraph(str(tot_words), cell)]
    met = Table([met_hdr, met_row], repeatRows=1)
    met.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0ea5a4")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story += [Paragraph("<b>Metrics (per TOC section — content text only, no style)</b>", sub),
              Spacer(1, 4), met, Spacer(1, 14),
              Paragraph("<b>Per-section detail</b>", sub), Spacer(1, 4)]

    # Per-section detail as FLOWING paragraphs (not a table): a missing-fragment
    # list can be longer than one page, and a table cell cannot split across
    # pages (raises LayoutError) while stacked Paragraphs flow freely.
    item_hdr = ParagraphStyle("ihdr", parent=ss["Normal"], fontSize=9.5,
                              leading=13, fontName="Helvetica-Bold", spaceBefore=8)
    meta_s = ParagraphStyle("meta", parent=ss["Normal"], fontSize=7.5,
                            leading=10, textColor=colors.HexColor("#64748b"))
    frag_s = ParagraphStyle("frag", parent=ss["Normal"], fontSize=8, leading=11,
                            leftIndent=10, textColor=colors.HexColor("#b91c1c"))
    sev = {"PASS": "#166534", "FAIL": "#991b1b", "SKIP": "#854d0e", "ERROR": "#854d0e"}
    for i, r in enumerate(state["rows"], 1):
        status = (r.get("status") or "error").upper()
        cov = f"{r.get('coverage_pct')}%" if r.get("coverage_pct") is not None else "—"
        site_flag = "" if r.get("on_site") else " · not matched to a site page"
        story.append(Paragraph(
            f'{i}. {esc(r.get("nav_item"))} '
            f'<font color="{sev.get(status, "#334155")}">[{status}]</font> '
            f'<font color="#334155">· coverage {cov} · {r.get("site_words", 0)} PDF words'
            f'{site_flag}</font>',
            item_hdr))
        if r.get("url"):
            story.append(Paragraph(esc(r["url"]), meta_s))
        if r.get("note") and status != "PASS":
            story.append(Paragraph(esc(r["note"]), meta_s))
        if r.get("missing_all"):
            ms = r["missing_all"]
            story.append(Paragraph(
                f"<b>{len(ms)} content fragment(s) in the PDF missing from the site:</b>", miss))
            for frag in ms:
                story.append(Paragraph("• " + esc(frag), frag_s))
        elif status == "FAIL":
            story.append(Paragraph("Content differs (see coverage).", miss))
        else:
            story.append(Paragraph("✓ All PDF content for this section found on the site.", cell))
    doc.build(story)
    bio.seek(0)
    return bio


@app.route("/sites-validation/report", methods=["GET"])
def sites_validation_report():
    state = LAST_SITES_RESULT.get("data")
    if not state:
        return jsonify(error="Run a validation first — no report available yet."), 404
    # Serve the pre-built bytes (rendered in the background after the run) so the
    # download is instant; fall back to building on demand if not ready yet.
    data = LAST_SITES_RESULT.get("pdf_bytes")
    if data is None:
        try:
            data = _build_sites_report_pdf(state).getvalue()
        except Exception as e:
            traceback.print_exc()
            return jsonify(error=f"Could not build report: {e}"), 500
        with SITES_REPORT_LOCK:
            if LAST_SITES_RESULT.get("data") is state:
                LAST_SITES_RESULT["pdf_bytes"] = data
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return send_file(io.BytesIO(data), mimetype="application/pdf", as_attachment=True,
                     download_name=f"sites_validation_report_{stamp}.pdf")


# ── Sites Validation — Style Check routes ────────────────────────────────────
_SITES_STYLE_RESULT: dict = {"pdf_bytes": None, "prod_name": None, "stage_name": None}
_SITES_STYLE_LOCK = threading.Lock()
_SITES_STYLE_PROG: dict = {"pct": 0, "msg": "", "done": False, "error": None}
_SITES_STYLE_THREAD = None


@app.route("/sites-validation/style/run", methods=["POST"])
def sites_style_run():
    """Validate the live AEM site against a PROD PDF — images, layout & typography.

    Inputs: a single PROD PDF (selection or upload) + the AEM author URL. The
    pages under that URL are rendered in headless Chromium and checked for
    oversized / cut-off images, table breaking, and typography-spec drift; image
    sizes are cross-checked against the matching PDF section."""
    global _SITES_STYLE_THREAD
    from content_validation import sites_image_validation as siv

    pdf_sel = request.form.get("pdf")
    url     = (request.form.get("url") or "").strip()
    upload  = request.files.get("pdf_file")
    if not url:
        return jsonify(error="AEM author URL is required."), 400
    if not _has_aem_session():
        return jsonify(error="Not signed in to AEM. Use the Sign in panel first."), 401

    ref_path, ref_name, tmp_dir = _resolve_ref_pdf(pdf_sel, upload)
    if not ref_path:
        return jsonify(error="Select a specific PROD PDF (not “All PDFs”) or upload one."), 400
    # Copy to a stable temp file so the worker owns its lifetime, then clean source.
    prod_tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    prod_tmp.write(open(ref_path, "rb").read()); prod_tmp.close()
    if tmp_dir:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    auth_token = (_load_session_meta() or {}).get("auth")

    with _SITES_STYLE_LOCK:
        if _SITES_STYLE_THREAD is not None and _SITES_STYLE_THREAD.is_alive():
            return jsonify(error="A check is already running. Please wait."), 409
        _SITES_STYLE_PROG.update({"pct": 0, "msg": "starting…", "done": False, "error": None})
        _SITES_STYLE_RESULT.update({"pdf_bytes": None, "findings": None, "doc_stats": None,
                                    "prod_name": ref_name, "stage_name": url})

        def _run(prod_path, site_url, token, p_name):
            out_tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            out_tmp.close()
            try:
                def cb(frac, msg=""):
                    _SITES_STYLE_PROG["pct"] = int(round(frac * 100))
                    _SITES_STYLE_PROG["msg"] = msg

                cb(0.04, "crawling site pages")
                pages = _aem_crawl_pages(site_url)
                if not pages:
                    raise RuntimeError("No pages found under the author URL.")

                findings, stats = siv.validate_site_vs_pdf(prod_path, pages, token, cb)
                stats["site_url"] = site_url
                style_validation.build_report(
                    prod_path, prod_path, findings, out_tmp.name,
                    category_order=siv.SITES_IMAGE_CATEGORY_ORDER)
                data = open(out_tmp.name, "rb").read()
                with _SITES_STYLE_LOCK:
                    _SITES_STYLE_RESULT["pdf_bytes"] = data
                    _SITES_STYLE_RESULT["findings"]  = findings
                    _SITES_STYLE_RESULT["doc_stats"] = stats
                _SITES_STYLE_PROG.update({"pct": 100, "msg": "done", "done": True})
            except Exception as e:
                traceback.print_exc()
                _SITES_STYLE_PROG.update({"done": True, "error": str(e)})
            finally:
                _safe_unlink(prod_path)
                _safe_unlink(out_tmp.name)

        _SITES_STYLE_THREAD = threading.Thread(
            target=_run, args=(prod_tmp.name, url, auth_token, ref_name), daemon=True)
        _SITES_STYLE_THREAD.start()

    return jsonify(started=True)


@app.route("/sites-validation/style/progress", methods=["GET"])
def sites_style_progress():
    return jsonify(**_SITES_STYLE_PROG)


@app.route("/sites-validation/style/summary", methods=["GET"])
def sites_style_summary():
    """Return structured JSON summary of the last style check for in-page rendering."""
    data = _SITES_STYLE_RESULT.get("pdf_bytes")
    if not data:
        return jsonify(error="No style check result yet."), 404
    # Re-run validate_style to get findings + doc_stats (cached pdf_bytes already built)
    # We store findings in _SITES_STYLE_RESULT so we don't re-run the check.
    findings  = _SITES_STYLE_RESULT.get("findings") or []
    stats     = _SITES_STYLE_RESULT.get("doc_stats") or {}
    from content_validation.sites_image_validation import SITES_IMAGE_CATEGORY_ORDER as CATEGORY_ORDER

    # Each finding is one issue; a category with N findings = N issues out of
    # max(N, images/tables checked) so the pass% is meaningful for the site.
    by_cat = {c: [f for f in findings if f["category"] == c] for c in CATEGORY_ORDER}
    pages_n = stats.get("pages_rendered", 0)
    base_checked = {
        "Image dimension":   stats.get("images_checked", 0),
        "Image padding":     stats.get("images_checked", 0),
        "Space above image": stats.get("images_checked", 0),
        "Oversized image":   stats.get("images_checked", 0),
        "Image cut off":     stats.get("images_checked", 0),
        "Image alignment":   stats.get("images_checked", 0),
        "Table breaking":    stats.get("tables_checked", 0),
        "Typography spec":   pages_n,
        "Line height":       pages_n,
        "Content alignment": pages_n,
    }

    categories = []
    total_checked_all = 0
    total_issues_all  = 0
    for c in CATEGORY_ORDER:
        items     = by_cat[c]
        n_issues  = len(items)
        n_checked = max(base_checked.get(c, 1), n_issues, 1)
        n_passed  = max(0, n_checked - n_issues)
        pct       = round(100 * n_passed / n_checked, 1) if n_checked else 100.0
        total_checked_all += n_checked
        total_issues_all  += n_issues
        top_sev = next((s for s in ("High","Medium","Low","Info")
                        if any(f["severity"]==s for f in items)), None)
        categories.append({
            "name":     c,
            "checked":  n_checked,
            "issues":   [{"topic":f["topic"],"pages":f["pages"],"severity":f["severity"],
                          "expected":f["expected"],"actual":f["actual"],
                          "issue":f["issue"],"fix":f["fix"]} for f in items],
            "pass_pct": pct,
            "severity": top_sev,
        })

    total_passed = max(0, total_checked_all - total_issues_all)
    overall_pct  = round(100 * total_passed / total_checked_all, 1) if total_checked_all else 100.0

    return jsonify(
        overall_pct   = overall_pct,
        total_checked = total_checked_all,
        total_issues  = total_issues_all,
        total_passed  = total_passed,
        prod_name     = _SITES_STYLE_RESULT.get("prod_name"),
        stage_name    = _SITES_STYLE_RESULT.get("stage_name"),
        prod_pages    = stats.get("pages_rendered"),
        stage_pages   = stats.get("pages_failed"),
        prod_headings = stats.get("tables_checked"),
        prod_images   = stats.get("images_checked"),
        render_errors = stats.get("render_errors"),
        categories    = categories,
    )


@app.route("/sites-validation/style/report", methods=["GET"])
def sites_style_report():
    data = _SITES_STYLE_RESULT.get("pdf_bytes")
    if not data:
        return jsonify(error="Run a style check first — no report available yet."), 404
    prod_stem  = Path(_SITES_STYLE_RESULT.get("prod_name",  "prod")).stem
    stage_stem = Path(_SITES_STYLE_RESULT.get("stage_name", "stage")).stem
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"style_validation_{prod_stem}_vs_{stage_stem}_{stamp}.pdf"
    return send_file(io.BytesIO(data), mimetype="application/pdf",
                     as_attachment=True, download_name=fname)


def _aem_crawl_pages(base_url: str, limit: int = 1000) -> list[dict]:
    """Return [{title, path, url}] for all CQ:Pages under the same content
    path as base_url, via AEM's QueryBuilder API.  Falls back to nav-link
    crawling if QueryBuilder is unavailable."""
    meta = _load_session_meta() or {}
    import base64 as _b64
    auth_header = ("Authorization", "Basic " + meta["auth"]) if meta.get("auth") else None

    parsed = urllib.parse.urlparse(base_url)
    host = f"{parsed.scheme}://{parsed.netloc}"
    # derive the JCR content path from the URL path (strip .html suffix)
    content_path = re.sub(r"\.html$", "", parsed.path)
    # go up one level to get the parent section (all sibling pages)
    parent_path = "/".join(content_path.rstrip("/").split("/")[:-1]) or content_path

    def _get_json(url):
        hdrs = {"User-Agent": "Mozilla/5.0"}
        if auth_header:
            hdrs[auth_header[0]] = auth_header[1]
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                return json.loads(r.read())
        except Exception:
            return None

    qb_url = (host + "/bin/querybuilder.json?" +
              urllib.parse.urlencode({
                  "type": "cq:Page",
                  "path": parent_path,
                  "p.limit": str(limit),
                  "orderby": "path",
              }))
    data = _get_json(qb_url)
    if data and data.get("hits"):
        pages = []
        for h in data["hits"]:
            p = h.get("path", "")
            title = h.get("title") or p.split("/")[-1]
            pages.append({"title": title, "path": p,
                          "url": host + p + ".html"})
        return pages

    # Fallback BFS recursive crawler: parse all links under parent_path prefix up to depth 3
    def _is_subpath(parent: str, child: str) -> bool:
        p_parts = parent.rstrip("/").split("/")
        c_parts = child.rstrip("/").split("/")
        if len(c_parts) < len(p_parts):
            return False
        return c_parts[:len(p_parts)] == p_parts

    seen_urls = {base_url}
    pages = [{"title": "Main Landing Page", "path": parsed.path, "url": base_url}]
    queue = [(base_url, 0)]

    while queue:
        curr_url, curr_depth = queue.pop(0)
        if curr_depth >= 3:
            continue
        try:
            html = _fetch_html(curr_url)
        except Exception:
            continue

        parser = _LinkParser()
        parser.feed(html)

        for item in parser.items:
            href = item["href"].strip()
            if not href or href.startswith(("#", "javascript:", "mailto:")):
                continue
            full_url = urllib.parse.urljoin(curr_url, href)
            parsed_full = urllib.parse.urlparse(full_url)
            clean_path = parsed_full.path

            # Normalize to clean URL
            clean_url = urllib.parse.urlunparse((
                parsed_full.scheme,
                parsed_full.netloc,
                clean_path,
                "", "", ""
            ))

            if clean_url in seen_urls:
                continue
            # Filter: same host
            if parsed_full.netloc and parsed_full.netloc != parsed.netloc:
                continue
            # Filter: under parent_path section (ignoring .html extension)
            clean_path_no_html = re.sub(r"\.html$", "", clean_path)
            if not _is_subpath(parent_path, clean_path_no_html):
                continue

            seen_urls.add(clean_url)
            title = item["text"] or clean_path.split("/")[-1] or "Sub-page"
            pages.append({"title": title, "path": clean_path, "url": clean_url})
            queue.append((clean_url, curr_depth + 1))

    return pages



if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the PDF validation frontend.")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind the Flask app")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)), help="Port to bind the Flask app")
    args = parser.parse_args()

    # debug=True keeps the werkzeug auto-reloader on: editing any .py file
    # restarts the worker automatically, and templates re-render per request.
    # So changes show up after a browser refresh — no manual restart required.
    app.run(host=args.host, port=args.port, debug=True, use_reloader=True)

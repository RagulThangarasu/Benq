import gc
import hashlib
import json
import os
import re
import shutil
import threading
import traceback
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory, send_file
import tempfile
import io
from werkzeug.utils import secure_filename
import sys
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "tmp_uploads"
PAIRS_DIR = UPLOAD_FOLDER / "pairs"
QUEUE_FILE = PAIRS_DIR / "queue.json"
REPORTS_DIR = BASE_DIR / "reports"
ALLOWED_EXTENSIONS = {"pdf"}
PROGRESS_FILE = REPORTS_DIR / "progress.json"
# In-memory progress store to avoid writing progress to disk
PROGRESS_STORE: dict = {"total": 0, "completed": 0, "current": None, "reports": [],
                        "finished": True, "errors": [], "pct": 0}
PROG_LOCK = threading.Lock()
# background job control
JOB_LOCK = threading.Lock()
JOB_THREAD = None
# finished report held in memory for /result download
LAST_RESULT: dict = {"data": None, "name": None, "mime": None}
# track running subprocesses for cancellation
RUNNING_PROCS: list = []
# cancellation flag
CANCELLED = False

UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
PAIRS_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["REPORTS_DIR"] = str(REPORTS_DIR)
app.config["MAX_CONTENT_LENGTH"] = 100 * 1024 * 1024  # 100 MB

# Make the content_validation package importable. The actual validation runs in
# a subprocess (run_validator.py), so these top-level imports are only a warm-up
# / availability check — never let them crash app startup, or the whole service
# fails its health check and the site goes down.
sys.path.insert(0, str(BASE_DIR / "content_validation"))
try:
    from content_validation import style_validation  # noqa: F401
    from content_validation import validate_toc_content  # noqa: F401
except Exception as _imp_exc:  # pragma: no cover
    print(f"[startup] content_validation import warning: {_imp_exc}", flush=True)

# Free-tier memory is tight (~512 MB); each parallel job is a full PyMuPDF
# subprocess. Cap concurrency via env so we don't OOM-kill the worker.
MAX_PARALLEL = max(1, int(os.environ.get("VALIDATOR_MAX_PARALLEL", "1")))


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def safe_path(filename: str) -> Path:
    filename = filename.replace("\\", "/")
    parts = [secure_filename(part) for part in filename.split("/") if part]
    return Path(*parts)


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
    return dict(PROGRESS_STORE)


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
    return str(destination)


def root_folders(files: list) -> list[str]:
    roots = set()
    for file_storage in files:
        root = Path(file_storage.filename.replace("\\", "/")).parts
        if root:
            roots.add(root[0])
    return sorted(roots)


def pair_pdfs(prod_dir: Path, stage_dir: Path) -> list[tuple[Path, Path, str]]:
    prod_pdfs = sorted(prod_dir.rglob("*.pdf"))
    stage_pdfs = sorted(stage_dir.rglob("*.pdf"))
    if not prod_pdfs or not stage_pdfs:
        raise ValueError("Each appended folder pair must contain at least one PDF file.")
    if len(prod_pdfs) == 1 and len(stage_pdfs) == 1:
        return [(prod_pdfs[0], stage_pdfs[0], "")]
    if len(prod_pdfs) == len(stage_pdfs):
        return [
            (
                prod_pdfs[i],
                stage_pdfs[i],
                f"{prod_pdfs[i].relative_to(prod_dir)} vs {stage_pdfs[i].relative_to(stage_dir)}",
            )
            for i in range(len(prod_pdfs))
        ]
    raise ValueError(
        "Prod and Stage folder must each contain the same number of PDF files, or exactly one PDF each."
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

    label = build_pair_label(prod_files, stage_files)
    queue = load_queue()
    queue.append({
        "id": pair_id,
        "label": label,
        "prod_count": prod_count,
        "stage_count": stage_count,
        "prod_files": prod_list[:12],
        "stage_files": stage_list[:12],
        "created": datetime.now().isoformat(),
    })
    save_queue(queue)
    return jsonify(queue=queue)


@app.route("/clear_queue", methods=["POST"])
def clear_queue():
    clean_queue_files()
    return jsonify(queue=[])


class _Cancelled(Exception):
    pass


def _safe_unlink(p):
    try:
        os.unlink(p)
    except Exception:
        pass


def _run_jobs(tasks):
    """Run validation tasks IN-PROCESS, sequentially, in a background thread.

    Why in-process (not a subprocess): a subprocess loads a *second* full
    Python + PyMuPDF interpreter on top of the gunicorn worker, and on the
    512 MB free tier that double footprint gets OOM-killed → 502s on every
    request (even /progress). Running in this worker keeps a single PyMuPDF
    copy; sequential + gc.collect() bounds peak memory to one validation.
    Running in a background thread keeps /validate from blocking the worker, so
    /progress and /result stay responsive throughout.
    """
    n = len(tasks)
    produced = []  # (download_name, bytes)
    try:
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
                    PROGRESS_STORE["current"] = (f"{_m}: {_l} — {msg}" if msg
                                                 else f"{_m}: {_l}")

            tf = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
            tf.close()
            outpath = tf.name
            try:
                if mode_task == "style":
                    style_validation.set_progress_callback(cb)
                    style_validation.main(str(prod_path), str(stage_path), outpath)
                else:
                    validate_toc_content.set_progress_callback(cb)
                    validate_toc_content.validate(str(prod_path), str(stage_path), outpath)
            except _Cancelled:
                _safe_unlink(outpath)
                break
            except Exception:
                tb = traceback.format_exc()
                last = tb.strip().splitlines()[-1] if tb.strip() else "unknown error"
                print(f"[validate] {mode_task} failed for '{label}':\n{tb}", flush=True)
                with PROG_LOCK:
                    PROGRESS_STORE["errors"].append(
                        f"{mode_task} validation failed for '{label}': {last}")
                _safe_unlink(outpath)
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
                produced.append((build_report_name(mode_task, label), data))
            with PROG_LOCK:
                PROGRESS_STORE["completed"] = i + 1
                PROGRESS_STORE["pct"] = int(round((i + 1) / n * 100)) if n else 100
    finally:
        _finalize_result(produced)
        with PROG_LOCK:
            PROGRESS_STORE["finished"] = True
            if not CANCELLED:
                PROGRESS_STORE["pct"] = 100
            PROGRESS_STORE["current"] = "cancelled" if CANCELLED else "done"


def _finalize_result(produced):
    """De-dup identical reports and stash a single PDF (or a zip) for /result."""
    seen, items, name_counts = set(), [], {}
    for name, data in produced:
        h = hashlib.sha256(data).hexdigest()
        if h in seen:
            continue
        seen.add(h)
        if name in name_counts:
            name_counts[name] += 1
            root, ext = os.path.splitext(name)
            name = f"{root}_{name_counts[name]}{ext}"
        else:
            name_counts[name] = 1
        items.append((name, data))

    if not items:
        LAST_RESULT.update({"data": None, "name": None, "mime": None})
    elif len(items) == 1:
        name, data = items[0]
        LAST_RESULT.update({"data": data, "name": name, "mime": "application/pdf"})
    else:
        zip_bio = io.BytesIO()
        with zipfile.ZipFile(zip_bio, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in items:
                zf.writestr(name, data)
        LAST_RESULT.update({
            "data": zip_bio.getvalue(),
            "name": f"validation_reports_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
            "mime": "application/zip"})


@app.route("/validate", methods=["POST"])
def validate():
    mode = request.form.get("mode")
    if mode not in {"content", "style", "both"}:
        return jsonify(error="Please select a validation mode."), 400

    queue = load_queue()
    if not queue:
        return jsonify(error="No appended folder pairs to validate."), 400

    # Build (prod, stage, label) bundles from the queued folder pairs.
    bundles_list = []
    try:
        for item in queue:
            pair_dir = PAIRS_DIR / item["id"]
            bundles = pair_pdfs(pair_dir / "prod", pair_dir / "stage")
            for index, (prod_path, stage_path, detail) in enumerate(bundles, start=1):
                label = item["label"]
                if len(bundles) > 1:
                    label = f"{label}_{index}"
                bundles_list.append((prod_path, stage_path, label))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    # Expand into per-mode tasks.
    tasks = []
    for (prod_path, stage_path, label) in bundles_list:
        if mode in ("content", "both"):
            tasks.append(("content", prod_path, stage_path, label))
        if mode in ("style", "both"):
            tasks.append(("style", prod_path, stage_path, label))

    global JOB_THREAD, CANCELLED
    with JOB_LOCK:
        if JOB_THREAD is not None and JOB_THREAD.is_alive():
            return jsonify(error="A validation run is already in progress."), 409
        CANCELLED = False
        LAST_RESULT.update({"data": None, "name": None, "mime": None})
        with PROG_LOCK:
            PROGRESS_STORE.update({"total": len(tasks), "completed": 0,
                                   "current": "starting…", "reports": [],
                                   "finished": False, "errors": [], "pct": 0})
        JOB_THREAD = threading.Thread(target=_run_jobs, args=(tasks,), daemon=True)
        JOB_THREAD.start()

    # Return immediately; the browser polls /progress, then GETs /result.
    return jsonify(started=True, total=len(tasks))


@app.route("/result", methods=["GET"])
def result():
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


@app.route('/cancel', methods=['POST'])
def cancel():
    """Request cancellation; the in-process job aborts at its next progress tick."""
    global CANCELLED
    CANCELLED = True
    with PROG_LOCK:
        PROGRESS_STORE['current'] = 'cancelling…'
    return jsonify(status='cancelling')


@app.route("/reports/<path:filename>")
def download_report(filename: str):
    return send_from_directory(app.config["REPORTS_DIR"], filename, as_attachment=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the PDF validation frontend.")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind the Flask app")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)), help="Port to bind the Flask app")
    args = parser.parse_args()

    app.run(host=args.host, port=args.port, debug=True)

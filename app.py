import json
import os
import shutil
import uuid
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
PROGRESS_STORE: dict = {"total": 0, "completed": 0, "current": None, "reports": [], "finished": False}
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

try:
    from content_validation import style_validation
    from content_validation import validate_toc_content
except ImportError:
    import sys

    sys.path.insert(0, str(BASE_DIR / "content_validation"))
    from content_validation import style_validation
    from content_validation import validate_toc_content


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


@app.route("/validate", methods=["POST"])
def validate():
    mode = request.form.get("mode")
    if mode not in {"content", "style", "both"}:
        return jsonify(error="Please select a validation mode."), 400

    queue = load_queue()
    if not queue:
        return jsonify(error="No appended folder pairs to validate."), 400

    # Prepare bundles and total work count
    bundles_list = []  # list of tuples (prod_path, stage_path, label)
    try:
        for item in queue:
            pair_dir = PAIRS_DIR / item["id"]
            prod_dir = pair_dir / "prod"
            stage_dir = pair_dir / "stage"
            bundles = pair_pdfs(prod_dir, stage_dir)
            for index, (prod_path, stage_path, detail) in enumerate(bundles, start=1):
                label = item["label"]
                if len(bundles) > 1:
                    label = f"{label}_{index}"
                bundles_list.append((prod_path, stage_path, label))
    except ValueError as exc:
        return jsonify(error=str(exc)), 400

    total = len(bundles_list) * (2 if mode == "both" else 1)
    progress = {"total": total, "completed": 0, "current": None, "reports": [], "finished": False}
    write_progress(progress)

    temp_files = []  # list of (path, download_name)

    # Create a list of tasks (mode, prod, stage, label)
    tasks = []
    for (prod_path, stage_path, label) in bundles_list:
        if mode in ("content", "both"):
            tasks.append(("content", prod_path, stage_path, label))
        if mode in ("style", "both"):
            tasks.append(("style", prod_path, stage_path, label))

    # concurrency controlled by form field 'parallel' (int)
    try:
        parallel = int(request.form.get('parallel', request.form.get('parallelCount', 3)))
        if parallel < 1:
            parallel = 1
    except Exception:
        parallel = 3

    # run tasks using subprocesses to enable true parallelism and allow cancellation
    futures = []
    global CANCELLED
    CANCELLED = False
    with ThreadPoolExecutor(max_workers=parallel) as exc:
        for mode_task, prod_path, stage_path, label in tasks:
            if CANCELLED:
                break
            progress['current'] = f"{mode_task}: {label}"
            write_progress(progress)
            tf = tempfile.NamedTemporaryFile(suffix='.pdf', delete=False)
            tf.close()
            cmd = [sys.executable, str(BASE_DIR / 'run_validator.py'), mode_task, str(prod_path), str(stage_path), tf.name]
            # start process so we can kill it if requested
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            RUNNING_PROCS.append(proc)
            future = exc.submit(proc.communicate)
            futures.append((future, proc, tf.name, mode_task, label))

        # collect results as they complete
        for fut, proc, outpath, mode_task, label in futures:
            if CANCELLED:
                try:
                    proc.kill()
                except Exception:
                    pass
                continue
            try:
                stdout, stderr = fut.result()
            except Exception as excp:
                progress['finished'] = True
                write_progress(progress)
                # cleanup
                for pth, _, _ in temp_files:
                    try:
                        os.unlink(pth)
                    except Exception:
                        pass
                return jsonify(error=f"Validation failed for {label}: {excp}"), 500
            finally:
                try:
                    RUNNING_PROCS.remove(proc)
                except Exception:
                    pass

            # read output
            try:
                data = open(outpath, 'rb').read()
            except Exception:
                data = b''
            if len(data) > 1024 and not CANCELLED:
                temp_files.append((outpath, build_report_name(mode_task, label), data))
            else:
                try:
                    os.unlink(outpath)
                except Exception:
                    pass
            progress['completed'] += 1
            write_progress(progress)

    # finished generating reports
    progress['finished'] = True
    write_progress(progress)

    # If no reports generated, inform the client
    if not temp_files:
        return jsonify(error="No validation reports were generated (empty or no content)."), 422

    # Deduplicate by content hash and prepare download
    import hashlib
    unique = {}
    name_counts = {}
    for pth, dname, data in temp_files:
        h = hashlib.sha256(data).hexdigest()
        if h in unique:
            # duplicate content, skip
            try:
                os.unlink(pth)
            except Exception:
                pass
            continue
        # ensure unique filename if same name appears
        base = dname
        if base in name_counts:
            name_counts[base] += 1
            base = f"{os.path.splitext(dname)[0]}_{name_counts[dname]}{os.path.splitext(dname)[1]}"
        else:
            name_counts[base] = 1
        unique[h] = (pth, base, data)

    items = list(unique.values())
    if len(items) == 1:
        pth, dname, data = items[0]
        try:
            os.unlink(pth)
        except Exception:
            pass
        bio = io.BytesIO(data)
        bio.seek(0)
        return send_file(bio, mimetype='application/pdf', as_attachment=True, download_name=dname)

    # multiple unique files -> zip in-memory
    zip_bio = io.BytesIO()
    import zipfile
    with zipfile.ZipFile(zip_bio, 'w', zipfile.ZIP_DEFLATED) as zf:
        for pth, dname, data in items:
            zf.writestr(dname, data)
    # cleanup temp files
    for pth, _, _ in temp_files:
        try:
            os.unlink(pth)
        except Exception:
            pass
    zip_bio.seek(0)
    zip_name = f"validation_reports_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    return send_file(zip_bio, mimetype='application/zip', as_attachment=True, download_name=zip_name)


@app.route('/cancel', methods=['POST'])
def cancel():
    """Cancel currently running validation jobs."""
    global CANCELLED
    CANCELLED = True
    # kill running processes
    for p in list(RUNNING_PROCS):
        try:
            p.kill()
        except Exception:
            pass
    # clear the list
    RUNNING_PROCS.clear()
    PROGRESS_STORE['finished'] = True
    PROGRESS_STORE['current'] = 'cancelled'
    return jsonify(status='cancelled')


@app.route("/reports/<path:filename>")
def download_report(filename: str):
    return send_from_directory(app.config["REPORTS_DIR"], filename, as_attachment=True)


@app.route('/progress', methods=['GET'])
def progress():
    return jsonify(read_progress())


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the PDF validation frontend.")
    parser.add_argument("--host", default="0.0.0.0", help="Host to bind the Flask app")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", 5000)), help="Port to bind the Flask app")
    args = parser.parse_args()

    app.run(host=args.host, port=args.port, debug=True)

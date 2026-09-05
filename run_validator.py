#!/usr/bin/env python3
import sys
from pathlib import Path

mode = sys.argv[1]
prod = sys.argv[2]
stage = sys.argv[3]
out = sys.argv[4]
markdown_dir = sys.argv[5] if len(sys.argv) > 5 else None

# Avoid importing heavy modules unless needed
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / 'content_validation'))


def _progress(frac, msg=""):
    """Emit a machine-readable progress marker the web app streams + parses."""
    print(f"@@PROGRESS:{frac:.3f}:{msg}@@", flush=True)


if mode == 'content_visual':
    sys.path.insert(0, str(BASE))
    from content_validation.dual_runner import main, set_progress_callback
    set_progress_callback(_progress)
    main(prod, stage, out, markdown_dir)
elif mode in ('new', 'content'):
    from validate_toc_content import validate, set_progress_callback
    set_progress_callback(_progress)
    validate(prod, stage, out)
elif mode == 'style':
    from style_validation import main, set_progress_callback
    set_progress_callback(_progress)
    main(prod, stage, out)
else:
    raise SystemExit(f'Unknown mode: {mode}')

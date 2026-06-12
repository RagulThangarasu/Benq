#!/usr/bin/env python3
import sys
from pathlib import Path

mode = sys.argv[1]
prod = sys.argv[2]
stage = sys.argv[3]
out = sys.argv[4]

# Avoid importing heavy modules unless needed
BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / 'content_validation'))

if mode == 'content':
    from validate_toc_content import validate
    validate(prod, stage, out)
elif mode == 'style':
    from style_validation import main
    main(prod, stage, out)
else:
    raise SystemExit(f'Unknown mode: {mode}')

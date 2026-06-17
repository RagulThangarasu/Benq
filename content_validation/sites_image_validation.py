"""Compatibility wrapper: sites validation moved into style_validation.py.

This module re-exports the sites image/style validation entry points so
existing imports keep working.
"""

from content_validation.style_validation import (  # noqa: F401
    SITES_IMAGE_CATEGORY_ORDER,
    render_pages,
    validate_site_vs_pdf,
)

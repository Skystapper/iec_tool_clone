"""
Backwards-compatible error classifiers.

The canonical implementations now live in ``app.core.retry_handler`` so the
retry layer and the route layer share one source of truth. This module simply
re-exports them so existing imports (e.g. ``from app.core.error_handling
import is_rate_limit_error``) keep working.
"""
from app.core.retry_handler import (  # noqa: F401
    is_file_too_large_error,
    is_rate_limit_error,
    is_token_limit_error,
)

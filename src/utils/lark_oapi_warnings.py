# -*- coding: utf-8 -*-
"""Warning handling for lark-oapi import-time compatibility noise."""

from __future__ import annotations

import warnings
from collections.abc import Iterator
from contextlib import contextmanager


@contextmanager
def suppress_lark_oapi_import_warnings() -> Iterator[None]:
    """Suppress known upstream import-time deprecations from lark-oapi."""

    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="pkg_resources is deprecated as an API.*",
            category=UserWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message="Deprecated call to `pkg_resources\\.declare_namespace.*",
            category=DeprecationWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message="datetime\\.datetime\\.utcfromtimestamp\\(\\) is deprecated.*",
            category=DeprecationWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message="There is no current event loop",
            category=DeprecationWarning,
        )
        yield

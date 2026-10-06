"""Textual-side wrapper around the headless PDF converter.

The conversion itself lives in :mod:`pony.pdf_export`, which knows nothing
about the terminal.  This module adds only what a running app needs: the
blocking call is made on a thread worker and every outcome is reported
through the app rather than raised at the caller.

The converter names are re-exported so that existing imports — and the
tests that patch them — keep working from one place.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ..pdf_export import (
    Converter,
    NoPdfConverterError,
    find_converter,
    html_to_pdf,
)

if TYPE_CHECKING:
    from textual.app import App

__all__ = [
    "Converter",
    "NoPdfConverterError",
    "export_pdf_in_thread",
    "find_converter",
    "html_to_pdf",
]


def export_pdf_in_thread(app: App[object], html: str, out: Path, dest: Path) -> None:
    """Convert *html* to a PDF at *out*, reporting the result via *app*.

    Runs the blocking conversion; meant to be called from a Textual thread
    worker, so all user feedback goes through ``app.call_from_thread``.
    """
    try:
        html_to_pdf(html, out)
    except NoPdfConverterError as exc:
        app.call_from_thread(app.notify, str(exc), severity="error")
    except Exception as exc:  # noqa: BLE001
        app.call_from_thread(
            app.notify, f"Could not create PDF: {exc}", severity="error"
        )
    else:
        app.call_from_thread(app.notify, f"Saved {out.name} to {dest}")

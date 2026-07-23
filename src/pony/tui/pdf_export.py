"""Convert self-contained message HTML to PDF via an external converter.

Pony bundles no PDF library.  Instead this module shells out to whichever
HTML-to-PDF tool is installed, preferring higher-fidelity engines.  The HTML is
produced by :func:`pony.tui.message_renderer.build_browser_html` — the same
self-contained document used for the ``w`` browser view.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from textual.app import App


class NoPdfConverterError(RuntimeError):
    """Raised when no supported HTML-to-PDF converter is on ``PATH``."""


# Command builder: (executable, input_html, output_or_outdir) -> argv.
_BuildCmd = Callable[[str, Path, Path], list[str]]


@dataclass(frozen=True)
class _ConverterSpec:
    """A candidate converter: executable names and how to invoke it."""

    names: tuple[str, ...]
    build: _BuildCmd
    # LibreOffice ignores the output filename and writes ``<stem>.pdf`` into an
    # output directory, so it receives a directory and the result is moved.
    outdir_mode: bool = False


@dataclass(frozen=True)
class Converter:
    """A resolved converter: an existing executable plus its invocation."""

    executable: str
    build: _BuildCmd
    outdir_mode: bool


# Ordered by output fidelity for HTML email (best first).  The first whose
# executable resolves on ``PATH`` wins.
_CONVERTERS: tuple[_ConverterSpec, ...] = (
    _ConverterSpec(
        names=(
            "chromium",
            "chromium-browser",
            "google-chrome",
            "google-chrome-stable",
            "chrome",
        ),
        build=lambda exe, src, out: [
            exe,
            "--headless=new",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={out}",
            src.as_uri(),
        ],
    ),
    _ConverterSpec(
        names=("wkhtmltopdf",),
        build=lambda exe, src, out: [
            exe,
            "--enable-local-file-access",
            str(src),
            str(out),
        ],
    ),
    _ConverterSpec(
        names=("weasyprint",),
        build=lambda exe, src, out: [exe, str(src), str(out)],
    ),
    _ConverterSpec(
        names=("soffice", "libreoffice"),
        build=lambda exe, src, outdir: [
            exe,
            "--headless",
            "--convert-to",
            "pdf",
            "--outdir",
            str(outdir),
            str(src),
        ],
        outdir_mode=True,
    ),
)

# Tools named in the guidance message when nothing is installed.
_SUPPORTED = "chromium, google-chrome, wkhtmltopdf, weasyprint, or libreoffice"


def find_converter() -> Converter | None:
    """Return the highest-priority installed converter, or ``None``."""
    for spec in _CONVERTERS:
        for name in spec.names:
            exe = shutil.which(name)
            if exe is not None:
                return Converter(exe, spec.build, spec.outdir_mode)
    return None


def html_to_pdf(html: str, out_path: Path) -> None:
    """Render *html* to a PDF at *out_path* using an external converter.

    Raises :class:`NoPdfConverterError` when no converter is installed and
    ``subprocess.CalledProcessError`` when the converter itself fails.
    """
    converter = find_converter()
    if converter is None:
        raise NoPdfConverterError(
            f"No HTML-to-PDF converter found. Install one of: {_SUPPORTED}."
        )
    with tempfile.TemporaryDirectory(prefix="pony-pdf-") as tmp:
        tmpdir = Path(tmp)
        src = tmpdir / "message.html"
        src.write_text(html, encoding="utf-8")
        if converter.outdir_mode:
            argv = converter.build(converter.executable, src, tmpdir)
            subprocess.run(argv, check=True, capture_output=True)  # noqa: S603
            produced = tmpdir / f"{src.stem}.pdf"
            shutil.move(str(produced), str(out_path))
        else:
            argv = converter.build(converter.executable, src, out_path)
            subprocess.run(argv, check=True, capture_output=True)  # noqa: S603


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

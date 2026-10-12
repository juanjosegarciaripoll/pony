"""Convert self-contained message HTML to PDF via an external converter.

Pony bundles no PDF library.  Instead this module shells out to whichever
HTML-to-PDF tool is installed, preferring higher-fidelity engines.  The HTML is
produced by :func:`pony.message_renderer.build_browser_html` — the same
self-contained document used for the ``w`` browser view.

Nothing here knows about the terminal: ``pony view --pdf`` converts files
from the command line with no UI at all, and the Textual side lives in
:mod:`pony.tui.pdf_export`, which wraps :func:`html_to_pdf` in a worker.

**A converter that is a browser is given a URL, not a file.**  Chromium
printing a ``file://`` document applies no headers to it, so the
message's own scripts ran and its remote images were fetched while the
PDF was being made — measured: a ``<script>`` authored text into the
output and a tracking pixel reached its server.  Printing the page
:func:`pony.browser_preview.document_server` serves instead puts it
under :data:`pony.html_policy.MESSAGE_HTML_CSP`, where both are refused
and the PDF is otherwise identical.  Switching scripting off at the
command line is not an alternative: ``--disable-javascript`` is ignored
by current Chromium, and ``--blink-settings=scriptEnabled=false``, which
is not, stops ``--print-to-pdf`` producing any file at all.

The converters that are not browsers keep a file, and want one: none of
them executes script.  ``wkhtmltopdf`` is the exception that needs
saying no to — it does run script, so it is told not to, and it is not
given ``--enable-local-file-access``, a flag whose whole purpose is
letting the document read the disk.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .browser_preview import document_server


class NoPdfConverterError(RuntimeError):
    """Raised when no supported HTML-to-PDF converter is on ``PATH``."""


# Command builder: (executable, source, output_or_outdir) -> argv.  The
# source is whatever that converter is handed: a URL for one that speaks
# HTTP, otherwise the path of a temporary file.
_BuildCmd = Callable[[str, str, Path], list[str]]


@dataclass(frozen=True)
class _ConverterSpec:
    """A candidate converter: executable names and how to invoke it."""

    names: tuple[str, ...]
    build: _BuildCmd
    # LibreOffice ignores the output filename and writes ``<stem>.pdf`` into an
    # output directory, so it receives a directory and the result is moved.
    outdir_mode: bool = False
    # True for a converter that is a real browser: it is pointed at the
    # served page so the policy applies, and no file is written.
    serves: bool = False


@dataclass(frozen=True)
class Converter:
    """A resolved converter: an existing executable plus its invocation."""

    executable: str
    build: _BuildCmd
    outdir_mode: bool
    serves: bool


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
            src,
        ],
        serves=True,
    ),
    _ConverterSpec(
        names=("wkhtmltopdf",),
        build=lambda exe, src, out: [
            exe,
            # It runs script unless told otherwise, and it is not given
            # the flag that would let the document read the disk.
            "--disable-javascript",
            src,
            str(out),
        ],
    ),
    _ConverterSpec(
        names=("weasyprint",),
        build=lambda exe, src, out: [exe, src, str(out)],
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
            src,
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
                return Converter(exe, spec.build, spec.outdir_mode, spec.serves)
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
    if converter.serves:
        # Nothing is written: the converter fetches the document from the
        # loopback server, which answers under the message-HTML policy.
        with document_server(html) as url:
            argv = converter.build(converter.executable, url, out_path)
            subprocess.run(argv, check=True, capture_output=True)  # noqa: S603
        return
    with tempfile.TemporaryDirectory(prefix="pony-pdf-") as tmp:
        tmpdir = Path(tmp)
        src = tmpdir / "message.html"
        src.write_text(html, encoding="utf-8")
        if converter.outdir_mode:
            argv = converter.build(converter.executable, str(src), tmpdir)
            subprocess.run(argv, check=True, capture_output=True)  # noqa: S603
            produced = tmpdir / f"{src.stem}.pdf"
            shutil.move(str(produced), str(out_path))
        else:
            argv = converter.build(converter.executable, str(src), out_path)
            subprocess.run(argv, check=True, capture_output=True)  # noqa: S603

"""Tests for the external HTML-to-PDF converter.

The conversion itself lives in ``pony.pdf_export`` and runs headless, for
`pony view --pdf` as much as for the TUI; ``pony.tui.pdf_export`` adds only
the worker wrapper that reports through a running app.
"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from pony.pdf_export import (
    NoPdfConverterError,
    find_converter,
    html_to_pdf,
)
from pony.tui import pdf_export


def _which_for(available: set[str]) -> Callable[[str], str | None]:
    """Return a ``shutil.which`` stub that resolves only *available* names."""

    def _which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in available else None

    return _which


_WHICH = "pony.pdf_export.shutil.which"
_RUN = "pony.pdf_export.subprocess.run"


class FindConverterTest(unittest.TestCase):
    def test_prefers_chromium_over_libreoffice(self) -> None:
        with mock.patch(_WHICH, _which_for({"chromium", "soffice"})):
            conv = find_converter()
        assert conv is not None
        self.assertEqual(conv.executable, "/usr/bin/chromium")
        self.assertFalse(conv.outdir_mode)
        self.assertTrue(conv.serves)

    def test_falls_back_to_libreoffice(self) -> None:
        with mock.patch(_WHICH, _which_for({"soffice"})):
            conv = find_converter()
        assert conv is not None
        self.assertEqual(conv.executable, "/usr/bin/soffice")
        self.assertTrue(conv.outdir_mode)
        self.assertFalse(conv.serves)

    def test_none_when_nothing_installed(self) -> None:
        with mock.patch(_WHICH, _which_for(set())):
            self.assertIsNone(find_converter())


class HtmlToPdfTest(unittest.TestCase):
    def test_raises_when_no_converter(self) -> None:
        with (
            mock.patch(_WHICH, _which_for(set())),
            self.assertRaises(NoPdfConverterError),
        ):
            html_to_pdf("<html></html>", Path("/tmp/out.pdf"))

    def test_chromium_is_pointed_at_the_served_page_not_a_file(self) -> None:
        """A `file://` document carries no headers, so it gets a URL.

        Printing the served page is what keeps the message's scripts and
        its remote images out of the PDF: the policy refuses both.  The
        fake converter fetches the URL while the run is in progress, so
        the test also proves the document is really being served.
        """
        recorded: list[list[str]] = []
        fetched: list[str] = []

        def _fake_run(argv: list[str], **_kw: object) -> object:
            recorded.append(argv)
            request = urllib.request.Request(argv[-1])  # noqa: S310 (loopback)
            with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
                fetched.append(response.read().decode("utf-8"))
            # Emulate Chromium writing the requested PDF.
            out = next(a for a in argv if a.startswith("--print-to-pdf=")).split(
                "=", 1
            )[1]
            Path(out).write_bytes(b"%PDF-1.4\n")
            return subprocess.CompletedProcess(argv, 0)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "msg.pdf"
            with (
                mock.patch(_WHICH, lambda name: f"/usr/bin/{name}"),
                mock.patch(_RUN, _fake_run),
            ):
                html_to_pdf("<html>hi</html>", out)
            self.assertTrue(out.exists())
        argv = recorded[0]
        self.assertEqual(argv[0], "/usr/bin/chromium")
        self.assertTrue(argv[-1].startswith("http://127.0.0.1:"))
        self.assertEqual(fetched, ["<html>hi</html>"])

    def test_the_served_page_stops_being_served_afterwards(self) -> None:
        """Printing must not leave a message answering on a port."""
        urls: list[str] = []

        def _fake_run(argv: list[str], **_kw: object) -> object:
            urls.append(argv[-1])
            out = next(a for a in argv if a.startswith("--print-to-pdf=")).split(
                "=", 1
            )[1]
            Path(out).write_bytes(b"%PDF-1.4\n")
            return subprocess.CompletedProcess(argv, 0)

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(_WHICH, lambda name: f"/usr/bin/{name}"),
            mock.patch(_RUN, _fake_run),
        ):
            html_to_pdf("<html>hi</html>", Path(tmp) / "msg.pdf")

        # A closed listening socket refuses the connection; which
        # exception that surfaces as is not the same on every platform,
        # and CI runs three.
        with self.assertRaises((urllib.error.URLError, ConnectionError)):
            request = urllib.request.Request(urls[0])  # noqa: S310
            urllib.request.urlopen(request, timeout=5)  # noqa: S310

    def test_wkhtmltopdf_is_denied_script_and_the_disk(self) -> None:
        """It runs script by default, and the dropped flag read the disk."""
        recorded: list[list[str]] = []

        def _fake_run(argv: list[str], **_kw: object) -> object:
            recorded.append(argv)
            Path(argv[-1]).write_bytes(b"%PDF-1.4\n")
            return subprocess.CompletedProcess(argv, 0)

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch(_WHICH, _which_for({"wkhtmltopdf"})),
            mock.patch(_RUN, _fake_run),
        ):
            html_to_pdf("<html>hi</html>", Path(tmp) / "msg.pdf")
        argv = recorded[0]
        self.assertIn("--disable-javascript", argv)
        self.assertNotIn("--enable-local-file-access", argv)

    def test_libreoffice_moves_output_into_place(self) -> None:
        recorded: list[list[str]] = []

        def _fake_run(argv: list[str], **_kw: object) -> object:
            recorded.append(argv)
            # soffice writes <stem>.pdf into the --outdir directory.
            outdir = Path(argv[argv.index("--outdir") + 1])
            src = Path(argv[-1])
            (outdir / f"{src.stem}.pdf").write_bytes(b"%PDF-1.4\n")
            return subprocess.CompletedProcess(argv, 0)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "renamed.pdf"
            with (
                mock.patch(_WHICH, _which_for({"soffice"})),
                mock.patch(_RUN, _fake_run),
            ):
                html_to_pdf("<html>hi</html>", out)
            self.assertTrue(out.exists())
        self.assertIn("--convert-to", recorded[0])


class _FakeApp:
    """Minimal stand-in for a Textual ``App`` for thread-worker feedback."""

    def __init__(self) -> None:
        self.notes: list[tuple[str, str]] = []

    def call_from_thread(
        self, fn: Callable[..., object], *args: object, **kw: object
    ) -> object:
        return fn(*args, **kw)

    def notify(self, message: str, *, severity: str = "information") -> None:
        self.notes.append((message, severity))


class ExportPdfInThreadTest(unittest.TestCase):
    def test_success_notifies_saved(self) -> None:
        app = _FakeApp()
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "msg.pdf"
            with mock.patch.object(pdf_export, "html_to_pdf", lambda _h, _o: None):
                pdf_export.export_pdf_in_thread(
                    app,  # type: ignore[arg-type]
                    "<html></html>",
                    out,
                    Path(tmp),
                )
        self.assertEqual(len(app.notes), 1)
        message, severity = app.notes[0]
        self.assertIn("msg.pdf", message)
        self.assertEqual(severity, "information")

    def test_missing_converter_notifies_guidance(self) -> None:
        app = _FakeApp()

        def _raise(_h: str, _o: Path) -> None:
            raise NoPdfConverterError("no converter: install chromium")

        with mock.patch.object(pdf_export, "html_to_pdf", _raise):
            pdf_export.export_pdf_in_thread(
                app,  # type: ignore[arg-type]
                "<html></html>",
                Path("/tmp/x.pdf"),
                Path("/tmp"),
            )
        message, severity = app.notes[0]
        self.assertIn("install chromium", message)
        self.assertEqual(severity, "error")

    def test_converter_failure_notifies_error(self) -> None:
        app = _FakeApp()

        def _raise(_h: str, _o: Path) -> None:
            raise subprocess.CalledProcessError(1, ["soffice"])

        with mock.patch.object(pdf_export, "html_to_pdf", _raise):
            pdf_export.export_pdf_in_thread(
                app,  # type: ignore[arg-type]
                "<html></html>",
                Path("/tmp/x.pdf"),
                Path("/tmp"),
            )
        message, severity = app.notes[0]
        self.assertIn("Could not create PDF", message)
        self.assertEqual(severity, "error")


if __name__ == "__main__":
    unittest.main()

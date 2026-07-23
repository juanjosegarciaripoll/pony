"""Tests for the external HTML-to-PDF converter shim (``pony.tui.pdf_export``)."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest import mock

from pony.tui import pdf_export
from pony.tui.pdf_export import NoPdfConverterError, find_converter, html_to_pdf


def _which_for(available: set[str]) -> Callable[[str], str | None]:
    """Return a ``shutil.which`` stub that resolves only *available* names."""

    def _which(name: str) -> str | None:
        return f"/usr/bin/{name}" if name in available else None

    return _which


_WHICH = "pony.tui.pdf_export.shutil.which"
_RUN = "pony.tui.pdf_export.subprocess.run"


class FindConverterTest(unittest.TestCase):
    def test_prefers_chromium_over_libreoffice(self) -> None:
        with mock.patch(_WHICH, _which_for({"chromium", "soffice"})):
            conv = find_converter()
        assert conv is not None
        self.assertEqual(conv.executable, "/usr/bin/chromium")
        self.assertFalse(conv.outdir_mode)

    def test_falls_back_to_libreoffice(self) -> None:
        with mock.patch(_WHICH, _which_for({"soffice"})):
            conv = find_converter()
        assert conv is not None
        self.assertEqual(conv.executable, "/usr/bin/soffice")
        self.assertTrue(conv.outdir_mode)

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

    def test_chromium_argv_and_output(self) -> None:
        recorded: list[list[str]] = []

        def _fake_run(argv: list[str], **_kw: object) -> object:
            recorded.append(argv)
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
        self.assertTrue(argv[-1].startswith("file://"))

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

"""`pony view --pdf` — converting .eml files without a UI."""

from __future__ import annotations

import io
import subprocess
import unittest
from collections.abc import Sequence
from email.message import EmailMessage
from pathlib import Path
from unittest import mock

from conftest import TMP_ROOT

from pony.cli import main, run_eml_to_pdf
from pony.pdf_export import NoPdfConverterError

_CONVERT = "pony.pdf_export.html_to_pdf"


def _eml(path: Path, subject: str = "A subject") -> Path:
    message = EmailMessage()
    message["From"] = "her@example.com"
    message["To"] = "me@example.com"
    message["Subject"] = subject
    message["Date"] = "Mon, 05 Oct 2026 09:00:00 +0000"
    message["Message-ID"] = f"<{subject.replace(' ', '-')}@example.com>"
    message.set_content("Body text.")
    path.write_bytes(message.as_bytes())
    return path


def _fake_converter(written: list[Path]) -> object:
    """Stand in for the external converter, recording what it was asked for."""

    def _convert(html: str, out_path: Path) -> None:
        assert "<html" in html.lower()
        out_path.write_bytes(b"%PDF-1.7\n")
        written.append(out_path)

    return _convert


class RunEmlToPdfTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = TMP_ROOT / "eml-pdf" / self.id().rsplit(".", 1)[-1]
        self.root.mkdir(parents=True, exist_ok=True)
        self.written: list[Path] = []

    def _run(
        self, files: Sequence[Path], out_dir: Path | None = None
    ) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch(_CONVERT, _fake_converter(self.written)),
            mock.patch("sys.stdout", out),
            mock.patch("sys.stderr", err),
        ):
            code = run_eml_to_pdf(files=files, out_dir=out_dir)
        return code, out.getvalue(), err.getvalue()

    def test_a_file_becomes_a_pdf_beside_it(self) -> None:
        source = _eml(self.root / "report.eml")
        code, out, _err = self._run([source])
        self.assertEqual(0, code)
        self.assertEqual([self.root / "report.pdf"], self.written)
        self.assertIn("report.pdf", out)

    def test_several_files_in_one_run(self) -> None:
        sources = [_eml(self.root / f"m{n}.eml", f"Subject {n}") for n in range(3)]
        code, _out, _err = self._run(sources)
        self.assertEqual(0, code)
        self.assertEqual(
            [self.root / "m0.pdf", self.root / "m1.pdf", self.root / "m2.pdf"],
            self.written,
        )

    def test_an_existing_pdf_is_not_overwritten(self) -> None:
        source = _eml(self.root / "report.eml")
        (self.root / "report.pdf").write_bytes(b"older")
        code, _out, _err = self._run([source])
        self.assertEqual(0, code)
        self.assertEqual([self.root / "report-1.pdf"], self.written)
        self.assertEqual(b"older", (self.root / "report.pdf").read_bytes())

    def test_out_dir_is_used_and_created(self) -> None:
        source = _eml(self.root / "report.eml")
        target = self.root / "pdfs" / "nested"
        code, _out, _err = self._run([source], out_dir=target)
        self.assertEqual(0, code)
        self.assertEqual([target / "report.pdf"], self.written)

    def test_a_missing_file_is_reported_and_the_rest_still_run(self) -> None:
        good = _eml(self.root / "good.eml")
        code, _out, err = self._run([self.root / "absent.eml", good])
        self.assertEqual(1, code)
        self.assertIn("not a readable file", err)
        # The failure did not stop the run.
        self.assertEqual([self.root / "good.pdf"], self.written)

    def test_a_directory_is_not_a_message(self) -> None:
        code, _out, err = self._run([self.root])
        self.assertEqual(1, code)
        self.assertIn("not a readable file", err)

    def test_a_missing_converter_stops_the_run(self) -> None:
        """No later file could succeed either, so do not try them."""
        sources = [_eml(self.root / f"m{n}.eml", f"Subject {n}") for n in range(3)]
        out, err = io.StringIO(), io.StringIO()

        def _no_converter(_html: str, _out: Path) -> None:
            raise NoPdfConverterError("No HTML-to-PDF converter found. Install one.")

        with (
            mock.patch(_CONVERT, _no_converter),
            mock.patch("sys.stdout", out),
            mock.patch("sys.stderr", err),
        ):
            code = run_eml_to_pdf(files=sources, out_dir=None)
        self.assertEqual(1, code)
        self.assertIn("No HTML-to-PDF converter", err.getvalue())
        self.assertEqual("", out.getvalue())

    def test_a_converter_failure_names_the_file_and_continues(self) -> None:
        first = _eml(self.root / "bad.eml", "Bad")
        second = _eml(self.root / "fine.eml", "Fine")
        written: list[Path] = []

        def _convert(html: str, out_path: Path) -> None:
            if "Bad" in html:
                raise subprocess.CalledProcessError(
                    1, ["converter"], stderr=b"converter exploded"
                )
            out_path.write_bytes(b"%PDF-1.7\n")
            written.append(out_path)

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch(_CONVERT, _convert),
            mock.patch("sys.stdout", out),
            mock.patch("sys.stderr", err),
        ):
            code = run_eml_to_pdf(files=[first, second], out_dir=None)
        self.assertEqual(1, code)
        self.assertIn("bad.eml", err.getvalue())
        self.assertIn("converter exploded", err.getvalue())
        self.assertEqual([self.root / "fine.pdf"], written)

    def test_an_unwritable_out_dir_is_reported(self) -> None:
        source = _eml(self.root / "report.eml")
        # A file where the directory should be: mkdir cannot succeed.
        blocker = self.root / "blocker"
        blocker.write_bytes(b"not a directory")
        code, _out, err = self._run([source], out_dir=blocker / "sub")
        self.assertEqual(1, code)
        self.assertIn("could not create", err)


class ViewCommandTest(unittest.TestCase):
    """The flag reaches `run_eml_to_pdf` through `main`."""

    def setUp(self) -> None:
        self.root = TMP_ROOT / "eml-pdf-cli" / self.id().rsplit(".", 1)[-1]
        self.root.mkdir(parents=True, exist_ok=True)

    def test_main_routes_pdf_conversion(self) -> None:
        source = _eml(self.root / "report.eml")
        written: list[Path] = []
        with (
            mock.patch(_CONVERT, _fake_converter(written)),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            code = main(["view", "--pdf", str(source)])
        self.assertEqual(0, code)
        self.assertEqual([self.root / "report.pdf"], written)

    def test_main_passes_the_out_dir_through(self) -> None:
        source = _eml(self.root / "report.eml")
        target = self.root / "out"
        written: list[Path] = []
        with (
            mock.patch(_CONVERT, _fake_converter(written)),
            mock.patch("sys.stdout", io.StringIO()),
        ):
            code = main(["view", "--pdf", "--out-dir", str(target), str(source)])
        self.assertEqual(0, code)
        self.assertEqual([target / "report.pdf"], written)

    def test_the_viewer_still_opens_one_file(self) -> None:
        source = _eml(self.root / "report.eml")
        with mock.patch("pony.cli.run_eml_viewer", return_value=0) as viewer:
            code = main(["view", str(source)])
        self.assertEqual(0, code)
        self.assertEqual(source, viewer.call_args.kwargs["path"])

    def test_the_viewer_refuses_several_files(self) -> None:
        """Opening many at once has no meaning; --pdf is the bulk path."""
        first = _eml(self.root / "a.eml", "A")
        second = _eml(self.root / "b.eml", "B")
        err = io.StringIO()
        with mock.patch("sys.stderr", err), self.assertRaises(SystemExit):
            main(["view", str(first), str(second)])
        self.assertIn("one file at a time", err.getvalue())


if __name__ == "__main__":
    unittest.main()

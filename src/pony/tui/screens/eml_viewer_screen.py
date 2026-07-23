"""Full-screen viewer for a single RFC 5322 message (.eml file or attachment)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen

from ..message_renderer import extract_attachment
from ..terminal import launch_file, suspend_for_external_program
from ..widgets.message_view import MessageViewPanel


class EmlViewerScreen(Screen[None]):
    """Read-only full-screen viewer for a single RFC 5322 message.

    Used by ``pony view <file>`` and when opening email-format attachments
    from within the main TUI.  Nested attached emails open a second instance
    of this screen on the same app stack.
    """

    BINDINGS = [
        Binding("Q", "quit_viewer", "Close", priority=True, show=False),
        Binding("w", "open_browser", "Browser"),
        Binding("ctrl+p", "print_pdf", "Print PDF"),
    ]

    def __init__(
        self,
        raw_bytes: bytes,
        downloads_dir: Path | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._raw_bytes = raw_bytes
        self._downloads_dir = downloads_dir or Path.home() / "Downloads"

    def compose(self) -> ComposeResult:
        yield MessageViewPanel()

    def on_mount(self) -> None:
        self.query_one(MessageViewPanel).load_bytes(self._raw_bytes)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def action_quit_viewer(self) -> None:
        self.dismiss()

    def on_message_view_panel_close_requested(
        self, event: MessageViewPanel.CloseRequested
    ) -> None:
        event.stop()
        self.dismiss()

    # ------------------------------------------------------------------
    # In-message links and addresses
    # ------------------------------------------------------------------

    def action_activate_link(self, idx: str) -> None:
        pair = self.query_one(MessageViewPanel).body_link(int(idx))
        if pair is None or pair[0] != "web":
            return
        from .link_action_screen import LinkActionScreen

        self.app.push_screen(LinkActionScreen(pair[1]))  # pyright: ignore[reportUnknownMemberType]

    def action_compose_link(self, idx: str) -> None:  # noqa: ARG002
        self.app.notify(  # pyright: ignore[reportUnknownMemberType]
            "Compose is not available in the standalone viewer.",
            severity="warning",
        )

    def action_compose_address(self, idx: str) -> None:  # noqa: ARG002
        self.app.notify(  # pyright: ignore[reportUnknownMemberType]
            "Compose is not available in the standalone viewer.",
            severity="warning",
        )

    def action_harvest_contact(self, idx: str) -> None:  # noqa: ARG002
        pass

    # ------------------------------------------------------------------
    # Browser view
    # ------------------------------------------------------------------

    def action_open_browser(self) -> None:
        self.query_one(MessageViewPanel).open_in_browser()

    def action_print_pdf(self) -> None:
        """Render this message to a PDF in a folder the user picks."""
        from ..message_renderer import build_browser_html, render_message
        from ..pdf_export import export_pdf_in_thread
        from .save_folder_picker_screen import SaveFolderPickerScreen
        from .save_message_screen import _proposed_body_filename

        raw = self._raw_bytes
        rendered = render_message(raw)
        html = build_browser_html(raw)
        filename = Path(_proposed_body_filename(rendered)).with_suffix(".pdf").name

        def _on_folder(dest: Path | None) -> None:
            if dest is None:
                return
            out = (dest / filename).resolve()
            if not out.is_relative_to(dest.resolve()):
                self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                    "Invalid destination.", severity="error"
                )
                return
            self.run_worker(
                lambda: export_pdf_in_thread(self.app, html, out, dest),
                name="print-pdf",
                thread=True,
            )

        self.app.push_screen(SaveFolderPickerScreen(), _on_folder)  # pyright: ignore[reportUnknownMemberType]

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def action_open_attachment(self, index: str) -> None:
        idx = int(index)
        if idx == 0:
            count = self.query_one(MessageViewPanel).attachment_count
            self._open_indices(list(range(1, count + 1)))
        else:
            self._open_indices([idx])

    def action_save_attachment(self, index: str) -> None:
        idx = int(index)
        if idx == 0:
            count = self.query_one(MessageViewPanel).attachment_count
            self._save_indices(list(range(1, count + 1)))
        else:
            self._save_indices([idx])

    def _open_indices(self, indices: list[int]) -> None:
        for idx in indices:
            payload = extract_attachment(self._raw_bytes, idx)
            if payload is None:
                self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                    f"Attachment {idx} not found.", severity="warning"
                )
                continue
            if payload.content_type == "message/rfc822":
                self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                    EmlViewerScreen(payload.data, self._downloads_dir)
                )
            else:
                suffix = Path(payload.filename).suffix
                try:
                    with tempfile.NamedTemporaryFile(
                        delete=False,
                        suffix=suffix,
                        prefix="pony-attachment-",
                    ) as f:
                        f.write(payload.data)
                        path = Path(f.name)
                    # The default viewer may run in this terminal and alter
                    # input modes.  Textual's suspend context restores them.
                    with suspend_for_external_program(self.app):
                        launch_file(path)
                except OSError as exc:
                    self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                        f"Could not open attachment {idx}: {exc}", severity="error"
                    )

    def _save_indices(self, indices: list[int]) -> None:
        dest = self._downloads_dir
        dest.mkdir(parents=True, exist_ok=True)
        panel = self.query_one(MessageViewPanel)
        saved: list[str] = []
        for idx in indices:
            try:
                name = panel.save_attachment(idx, dest)
            except OSError as exc:
                self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                    f"Could not save attachment {idx}: {exc}", severity="error"
                )
                continue
            if name:
                saved.append(name)
        if saved:
            self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                f"Saved to {dest}: {', '.join(saved)}"
            )

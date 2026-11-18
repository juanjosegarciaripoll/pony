"""Full-screen viewer for a single RFC 5322 message (.eml file or attachment)."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen

from ....mail.domain import ViewerRule
from ....mail.message_export import (
    PartKey,
    part_key,
    parts_to_open,
    save_parts,
)
from ....mail.message_export import downloads_dir as resolve_downloads_dir
from ....mail.message_renderer import render_message
from ...reporting import report
from ...terminal import (
    launch_file,
    suspend_for_external_program,
)
from ..widgets.message_view import MessageViewPanel


class EmlViewerScreen(Screen[None]):
    """Read-only full-screen viewer for a single RFC 5322 message.

    Used by ``pony view <file>`` and when opening email-format attachments
    from within the main TUI.  Nested attached emails open a second instance
    of this screen on the same app stack.
    """

    # No `Q` here.  It is the application's key, and the application
    # asks this screen what leaving means (`handle_quit_key`): a binding
    # written here could never fire, because Textual's priority pass
    # runs the App first -- which is how "Close [Q]" came to quit Pony.
    BINDINGS = [
        Binding("w", "open_browser", "Browser"),
        Binding("ctrl+p", "print_pdf", "Print PDF"),
    ]

    def __init__(
        self,
        raw_bytes: bytes,
        downloads_dir: Path | None = None,
        viewers: Sequence[ViewerRule] = (),
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._raw_bytes = raw_bytes
        self._downloads_dir = resolve_downloads_dir(downloads_dir)
        self._viewers = viewers

    def compose(self) -> ComposeResult:
        yield MessageViewPanel()

    def on_mount(self) -> None:
        self.query_one(MessageViewPanel).load_bytes(self._raw_bytes)

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def action_quit_viewer(self) -> None:
        self.dismiss()

    def handle_quit_key(self) -> bool:
        """++shift+q++ closes the viewer, not Pony.

        A nested attachment opens a second viewer on the same stack, so
        this closes one layer at a time; in ``pony view`` the last one
        closing is what ends the program (see
        ``EmlViewerApp.on_screen_resume``).
        """
        self.action_quit_viewer()
        return True

    def on_message_view_panel_close_requested(
        self, event: MessageViewPanel.CloseRequested
    ) -> None:
        event.stop()
        self.dismiss()

    # ------------------------------------------------------------------
    # In-message links and addresses
    # ------------------------------------------------------------------

    def action_show_sender_check(self) -> None:
        """Say what the padlock beside `From` means, at length."""
        from .sender_check_screen import SenderCheckScreen

        panel = self.query_one(MessageViewPanel)
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            SenderCheckScreen(panel.authentication)
        )

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
        from ....mail.message_renderer import (
            build_browser_html,
            proposed_filename,
            render_message,
        )
        from ..pdf_export import start_pdf_export
        from .save_folder_picker_screen import SaveFolderPickerScreen

        raw = self._raw_bytes
        rendered = render_message(raw)
        html = build_browser_html(raw)
        filename = proposed_filename(rendered, suffix=".pdf")

        def _on_folder(dest: Path | None) -> None:
            if dest is None:
                return
            start_pdf_export(self, html=html, filename=filename, dest=dest)

        # Falls back to where saving puts things, which is the one
        # directory the viewer knows the reader wants; somewhere they
        # picked earlier still wins.
        self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
            SaveFolderPickerScreen(default_dir=self._downloads_dir), _on_folder
        )

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    def action_open_attachment(self, index: str) -> None:
        """Open part *index*: 0 the message, T its text, 1..N attachments.

        Through `part_key`, which is where the numbering lives.  This
        screen read the index with `int()` and took ``0`` for "every
        attachment", so the ``t`` and ++ctrl+t++ that the panel it mounts
        binds — both of which send ``'T'`` — crashed it, and ``0`` meant
        something here that it means nowhere else.
        """
        key = part_key(index)
        if key is None:
            return
        self._open_parts([key])

    def action_save_attachment(self, index: str) -> None:
        """Save part *index*: 0 the message, T its text, 1..N attachments."""
        key = part_key(index)
        if key is None:
            return
        self._save_parts([key])

    def _open_parts(self, keys: list[PartKey]) -> None:
        """Launch each chosen part, the way the mail reader launches it.

        `parts_to_open` makes every decision — where the bytes go, which
        viewer a content type gets, that a nested message opens in this
        screen rather than in whatever the desktop opens an ``.eml``
        with — and the launching is all that is left here.  This screen
        had its own copy of the whole thing, down to its own wording for
        a part that was not there.
        """
        dest = self._downloads_dir
        parts, outcome = parts_to_open(
            render_message(self._raw_bytes), keys, dest, viewers=self._viewers
        )
        report(self.app, outcome)
        for part in parts:
            if part.path is None:
                self.app.push_screen(  # pyright: ignore[reportUnknownMemberType]
                    EmlViewerScreen(part.data, dest, viewers=self._viewers)
                )
                continue
            try:
                # The default viewer may run in this terminal and alter
                # input modes.  Textual's suspend context restores them.
                with suspend_for_external_program(self.app):
                    launch_file(part.path, part.command)
            except OSError as exc:
                self.app.notify(  # pyright: ignore[reportUnknownMemberType]
                    f"Could not open {part.path.name}: {exc}", severity="error"
                )

    def _save_parts(self, keys: list[PartKey]) -> None:
        report(
            self.app,
            save_parts(render_message(self._raw_bytes), keys, self._downloads_dir),
        )

"""Top-level Textual application for Pony Express."""

from __future__ import annotations

from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding

from ..domain import AnyAccount, AppConfig
from ..protocols import (
    ContactRepository,
    CredentialsProvider,
    IndexRepository,
    MirrorRepository,
)
from .screens.main_screen import MainScreen


class PonyApp(App[None]):
    """Pony Express — terminal mail client."""

    TITLE = "Pony Express"
    SUB_TITLE = "Mail"

    # The Textual built-in command palette (``ctrl+p``) shows a
    # system-style side panel with actions like Quit and Show Keys.
    # Disable it: our own centered help dialog on ``F1`` is the
    # intended keyboard-shortcut discovery path.
    ENABLE_COMMAND_PALETTE = False

    BINDINGS = [
        Binding("Q", "quit", "Quit", priority=True),
        Binding("f1", "show_help", "Help"),
    ]

    def action_show_help(self) -> None:
        """Push the centered help dialog (keybinding reference)."""
        from .screens.help_screen import HelpScreen

        # If the help screen is already on top, F1 from the screen
        # itself dismisses it — this path is only for app-level F1.
        self.push_screen(HelpScreen())

    def __init__(
        self,
        config: AppConfig,
        index: IndexRepository,
        mirrors: dict[str, MirrorRepository],
        credentials: CredentialsProvider,
        contacts: ContactRepository | None = None,
        config_path: Path | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._config = config
        self._index = index
        self._mirrors = mirrors
        self._credentials = credentials
        self._contacts = contacts
        self._config_path = config_path

    def compose(self) -> ComposeResult:
        # Screens are pushed via on_mount; compose yields nothing at app level.
        return iter([])

    def on_mount(self) -> None:
        self.push_screen(
            MainScreen(
                self._config,
                self._index,
                self._mirrors,
                credentials=self._credentials,
                contacts=self._contacts,
            )
        )
        if self._config.mcp is not None:
            from ..mcp_server import start_mcp_thread

            mcp = self._config.mcp
            start_mcp_thread(self._config_path, mcp)
            self.notify(
                f"MCP server listening on http://{mcp.host}:{mcp.port}/mcp",
                title="MCP",
                timeout=5,
            )


class ComposeApp(App[None]):
    """Minimal Textual app that opens the composer directly.

    Used by ``pony compose`` when the user wants to write a new message
    without entering the full mail-reader TUI.
    """

    TITLE = "Pony Express — Compose"
    INHERIT_BINDINGS = False

    BINDINGS = [
        Binding("Q", "quit", "Quit", priority=True),
    ]

    def __init__(
        self,
        config: AppConfig,
        account: AnyAccount,
        index: IndexRepository,
        mirrors: dict[str, MirrorRepository],
        contacts: ContactRepository | None = None,
        to: str = "",
        cc: str = "",
        bcc: str = "",
        subject: str = "",
        body: str = "",
        markdown_mode: bool = False,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._config = config
        self._account = account
        self._index = index
        self._mirrors = mirrors
        self._contacts = contacts
        self._to = to
        self._cc = cc
        self._bcc = bcc
        self._subject = subject
        self._body = body
        self._markdown_mode = markdown_mode

    def on_mount(self) -> None:
        from .screens.compose_screen import ComposeInitial, ComposeScreen

        def _on_done(sent: bool | None) -> None:
            if sent:
                self.notify("Message sent.", timeout=2)
                self.set_timer(2, self.exit)
            else:
                self.exit()

        # ComposeScreen only accepts accounts that can send; ``pony
        # compose`` at the CLI refuses to launch without at least one
        # sendable account, so this filter is primarily defensive.
        accounts = [a for a in self._config.accounts if a.can_send]
        self.push_screen(
            ComposeScreen(
                self._config,
                accounts,
                self._index,
                self._mirrors,
                ComposeInitial(
                    account_name=self._account.name,
                    to=self._to,
                    cc=self._cc,
                    bcc=self._bcc,
                    subject=self._subject,
                    body=self._body,
                    markdown_mode=self._markdown_mode,
                ),
                contacts=self._contacts,
            ),
            _on_done,
        )


class ContactsApp(App[None]):
    """Minimal Textual app for the standalone contacts browser.

    Used by ``pony contacts browse`` to open the contacts browser
    without the full mail-reader TUI.
    """

    TITLE = "Pony Express — Contacts"

    def __init__(
        self,
        contacts: ContactRepository,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._contacts = contacts

    def on_mount(self) -> None:
        from .screens.contact_browser_screen import ContactBrowserScreen

        self.push_screen(ContactBrowserScreen(self._contacts))

    def on_screen_resume(self) -> None:
        # Exit when the browser screen is dismissed.
        if len(self.screen_stack) <= 1:
            self.exit()

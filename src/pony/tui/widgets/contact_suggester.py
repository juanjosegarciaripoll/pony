"""Selectable email-address completion backed by the contacts store."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.events import Key
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from ...compose_utils import format_display_address, split_trailing_address
from ...protocols import ContactRepository


class RecipientInput(Vertical):
    """An address input with a selectable list of contact matches.

    The text input remains a normal free-form field.  Once at least two
    characters have been entered in the current comma-separated token, up to
    ten matching contact addresses are displayed below it.
    """

    DEFAULT_CSS = """
    RecipientInput {
        width: 1fr;
        height: auto;
    }

    RecipientInput > Input {
        width: 1fr;
    }

    RecipientInput > OptionList {
        display: none;
        width: 1fr;
        height: auto;
        max-height: 6;
        border: none;
        padding: 0 1;
        background: $panel;
    }
    """

    def __init__(
        self,
        contacts: ContactRepository,
        value: str = "",
        *,
        placeholder: str = "",
        input_id: str | None = None,
        input_classes: str = "",
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._contacts = contacts
        self._value = value
        self._placeholder = placeholder
        self._input_id = input_id
        self._input_classes = input_classes
        self._suggestions: list[str] = []
        self._selected_value: str | None = None

    def compose(self) -> ComposeResult:
        yield Input(
            self._value,
            placeholder=self._placeholder,
            id=self._input_id,
            classes=self._input_classes,
        )
        yield OptionList(classes="recipient-options")

    @property
    def input(self) -> Input:
        return self.query_one(Input)

    async def get_suggestion(self, value: str) -> str | None:
        """Return the top match for compatibility with the former suggester.

        Compose fields use the selectable list, but retaining this small API
        avoids breaking integrations which queried ``ContactSuggester``
        directly.
        """
        prefix, typed = _split_current_token(value)
        if len(typed) < 2:
            return None
        for contact in self._contacts.search_contacts(prefix=typed, limit=1):
            if contact.primary_email:
                return prefix + format_display_address(
                    contact.display_name, contact.primary_email
                )
        return None

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input is not self.input:
            return
        if event.value == self._selected_value:
            self._selected_value = None
            self._hide_options()
            return
        self._selected_value = None
        _prefix, typed = _split_current_token(event.value)
        if len(typed) < 2:
            self._hide_options()
            return

        addresses: list[str] = []
        for contact in self._contacts.search_contacts(prefix=typed, limit=10):
            for email in contact.emails:
                addresses.append(format_display_address(contact.display_name, email))
                if len(addresses) == 10:
                    break
            if len(addresses) == 10:
                break
        self._suggestions = addresses
        options = self.query_one(OptionList)
        options.clear_options()
        options.add_options(
            Option(address, id=str(index)) for index, address in enumerate(addresses)
        )
        options.display = bool(addresses)
        options.highlighted = 0 if addresses else None

    def on_key(self, event: Key) -> None:
        options = self.query_one(OptionList)
        if not self._suggestions:
            return
        if event.key == "down" and self.input.has_focus:
            options.focus()
            event.stop()
        elif event.key in {"tab", "enter"} and self.input.has_focus:
            self._select(0)
            event.prevent_default()
            event.stop()
        elif event.key == "escape":
            self._hide_options()
            self.input.focus()
            event.prevent_default()
            event.stop()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_list is self.query_one(OptionList):
            self._select(event.option_index)
            event.stop()

    def _select(self, index: int) -> None:
        if not 0 <= index < len(self._suggestions):
            return
        prefix, _typed = _split_current_token(self.input.value)
        selected_value = prefix + self._suggestions[index]
        self._selected_value = selected_value
        self.input.value = selected_value
        self.input.cursor_position = len(self.input.value)
        self._hide_options()
        self.input.focus()

    def _hide_options(self) -> None:
        self._suggestions = []
        options = self.query_one(OptionList)
        options.clear_options()
        options.display = False


def _split_current_token(value: str) -> tuple[str, str]:
    """Return the preserved field prefix and stripped current token."""
    return split_trailing_address(value)


# Kept as an import alias for third-party code; new code should use
# ``RecipientInput`` because completion is no longer a Textual Suggester.
ContactSuggester = RecipientInput

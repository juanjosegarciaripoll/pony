"""Which folder is which, by configuration or by name.

Used by the composer to locate Sent and Drafts folders without requiring
explicit configuration, and by the message list to tell a Sent folder from
an ordinary one.

Three layers: the pure name matchers, which know what servers call
these folders in other languages; :func:`resolve_folder`, which picks
one folder out of a mirror's list by what it is *for*; and
:func:`holds_sent_mail` / :func:`holds_drafts`, which consult the
account's own setting first and fall back to the matchers.  Every layer
above the first goes through the matchers, so the program cannot hold
two answers to "is this the Sent folder" — which it did, and the half
that said no was the one filing the copy of everything the user sent.

None of these predicates is about rendering, so none of them is the
reader's to answer.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

from .accounts import find_account

if TYPE_CHECKING:
    from .domain import AppConfig, FolderRef


def find_folder(candidates: list[str], hint: str) -> str | None:
    """Return the candidate whose name best matches *hint*.

    Matching priority (case-insensitive):

    1. Exact match               — ``"Sent"`` matches ``"sent"``
    2. Ends-with-separator match — ``"INBOX/Sent"`` matches ``"Sent"``
    3. Contains match            — ``"[Gmail]/Sent Mail"`` matches ``"Sent"``

    Returns ``None`` if no candidate satisfies any of the criteria.
    """
    hint_lower = hint.lower()

    # Pass 1: exact
    for name in candidates:
        if name.lower() == hint_lower:
            return name

    # Pass 2: ends with a path separator followed by hint
    for name in candidates:
        lower = name.lower()
        if lower.endswith(f"/{hint_lower}") or lower.endswith(f".{hint_lower}"):
            return name

    # Pass 3: contains hint as a substring
    for name in candidates:
        if hint_lower in name.lower():
            return name

    return None


def _fold(name: str) -> str:
    """Case- and diacritic-insensitive form of a folder name.

    ``Envoyés`` and ``Envoyes`` are the same folder as far as a user is
    concerned, and servers differ on whether they send the composed or
    the decomposed form.  Letters that do not decompose (Polish ``ł``,
    Cyrillic) are left alone and matched literally.
    """
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).strip()


# Names an IMAP server gives the folder holding messages the user has sent.
# Pony has no SPECIAL-USE data to consult, so the folder is recognised by
# name; the account's ``sent_folder`` setting overrides this when a server
# uses something unusual.
_SENT_FOLDER_NAMES = frozenset(
    _fold(name)
    for name in (
        # English
        "Sent",
        "Sent Items",
        "Sent Mail",
        "Sent Messages",
        # Spanish / Catalan / Galician
        "Enviados",
        "Enviadas",
        "Enviats",
        "Correo enviado",
        "Elementos enviados",
        "Mensajes enviados",
        # Portuguese
        "Itens enviados",
        "Mensagens enviadas",
        # French
        "Envoyés",
        "Messages envoyés",
        "Éléments envoyés",
        "Courrier envoyé",
        # German
        "Gesendet",
        "Gesendete Objekte",
        "Gesendete Elemente",
        "Gesendete Nachrichten",
        # Italian
        "Inviati",
        "Posta inviata",
        "Elementi inviati",
        # Dutch
        "Verzonden",
        "Verzonden items",
        # Nordic
        "Sendt",
        "Sendte elementer",
        "Skickat",
        "Skickade meddelanden",
        "Lähetetyt",
        "Lähetetyt viestit",
        # Slavic
        "Wysłane",
        "Elementy wysłane",
        "Odeslaná pošta",
        "Odeslané",
        "Отправленные",
        "Надіслані",
        # Other
        "Gönderilmiş",
        "Gönderilmiş Öğeler",
        "送信済み",
        "送信済みメール",
        "已发送",
        "已发送邮件",
        "寄件備份",
        "보낸편지함",
    )
)


# The same, for the folder holding messages the user has started but
# not sent.  Shorter than the Sent list because servers localise the
# word itself rather than wrapping it in "Items" / "Messages" variants.
_DRAFTS_FOLDER_NAMES = frozenset(
    _fold(name)
    for name in (
        # English
        "Drafts",
        "Draft",
        # Spanish / Catalan / Galician / Portuguese
        "Borradores",
        "Borrador",
        "Esborranys",
        "Rascunhos",
        # French
        "Brouillons",
        "Brouillon",
        # German
        "Entwürfe",
        "Entwurf",
        # Italian
        "Bozze",
        # Dutch
        "Concepten",
        "Klad",
        # Nordic
        "Kladder",
        "Utkast",
        "Luonnokset",
        # Slavic
        "Szkice",
        "Kopie robocze",
        "Koncepty",
        "Rozepsané",
        "Черновики",
        "Чернетки",
        # Other
        "Taslaklar",
        "下書き",
        "草稿",
        "임시 보관함",
    )
)


def is_sent_folder(folder_name: str) -> bool:
    """True when *folder_name* is the account's Sent folder.

    Only the last path segment is examined, so ``INBOX/Enviados`` and
    ``[Gmail]/Sent Mail`` both match while a folder merely filed under
    one of them (``Sent/2019``) does not.
    """
    return _leaf(folder_name) in _SENT_FOLDER_NAMES


def is_drafts_folder(folder_name: str) -> bool:
    """True when *folder_name* is the account's Drafts folder.

    Same rule as :func:`is_sent_folder`, against the names servers
    localise *Drafts* into.
    """
    return _leaf(folder_name) in _DRAFTS_FOLDER_NAMES


def _leaf(folder_name: str) -> str:
    """The folded last path segment of *folder_name*."""
    return _fold(folder_name.replace(".", "/").rsplit("/", 1)[-1])


# The localised tables, by the hint a caller passes.  Keyed on the
# English word because that is what the rest of the program calls these
# folders -- ``folder_hint="Sent"``, ``folder_hint="Drafts"`` -- and the
# whole point is that the *candidates* need not be in English.
_LOCALISED_MATCHERS = {
    "sent": is_sent_folder,
    "drafts": is_drafts_folder,
}


def resolve_folder(candidates: list[str], hint: str) -> str | None:
    """The candidate that *is* the *hint* folder, in whatever language.

    :func:`find_folder` matches the English word and nothing else, so on
    a Spanish or German server it found no ``Sent`` folder at all --
    while the message list, which asks :func:`is_sent_folder`, had
    already decided ``Enviados`` was one.  Two answers to one question,
    and the half that lost was the one that files the copy of everything
    the user sends.

    So the localised predicates are tried first, and :func:`find_folder`
    remains the fallback for a hint with no table (``"Archive"``) and for
    a folder whose name no table could hold (``"My Drafts"``).
    """
    matcher = _LOCALISED_MATCHERS.get(hint.casefold())
    if matcher is not None:
        for name in candidates:
            if matcher(name):
                return name
    return find_folder(candidates, hint)


def holds_sent_mail(config: AppConfig, folder: FolderRef) -> bool:
    """True when *folder* holds the user's own outgoing mail.

    The account's ``sent_folder`` setting decides it when configured;
    otherwise the name is matched against the ones servers localise the
    folder into, so ``Enviados`` is recognised as readily as ``Sent``.
    """
    account = find_account(config, folder.account_name)
    configured = account.sent_folder if account is not None else None
    if configured:
        return folder.folder_name == configured
    return is_sent_folder(folder.folder_name)


def holds_drafts(config: AppConfig, folder: FolderRef) -> bool:
    """True when *folder* is where this account's drafts are kept.

    Asked by both front ends before offering to resume a message, and
    again by :mod:`pony.delivery` before retiring the one it was resumed
    from, so a localised ``Borradores`` has to answer the same as
    ``Drafts``.
    """
    account = find_account(config, folder.account_name)
    configured = account.drafts_folder if account is not None else None
    if configured:
        return folder.folder_name == configured
    return resolve_folder([folder.folder_name], "Drafts") is not None


def shows_recipient(config: AppConfig, folder: FolderRef) -> bool:
    """True when a row in *folder* should name who it is *to*.

    Sent mail and drafts are both the user's own outgoing mail, so the
    sender is always themselves and the useful column is the recipient.
    Every list of messages asks this -- the reader's, the browser's and
    a search's -- so it is asked in one place.
    """
    return holds_sent_mail(config, folder) or holds_drafts(config, folder)

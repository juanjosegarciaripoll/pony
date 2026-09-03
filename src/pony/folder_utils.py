"""Folder name discovery helpers for Pony Express.

Used by the composer to locate Sent and Drafts folders without requiring
explicit configuration, and by the message list to tell a Sent folder from
an ordinary one.
"""

from __future__ import annotations

import unicodedata


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


def is_sent_folder(folder_name: str) -> bool:
    """True when *folder_name* is the account's Sent folder.

    Only the last path segment is examined, so ``INBOX/Enviados`` and
    ``[Gmail]/Sent Mail`` both match while a folder merely filed under
    one of them (``Sent/2019``) does not.
    """
    leaf = folder_name.replace(".", "/").rsplit("/", 1)[-1]
    return _fold(leaf) in _SENT_FOLDER_NAMES

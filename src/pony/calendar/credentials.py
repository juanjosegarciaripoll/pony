from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from pony.calendar.authorization import Authorization
from pony.calendar.domain import (
    GOOGLE_OAUTH_SCOPE,
    AccountConfig,
    CommandCredential,
    CredentialSpec,
    EnvCredential,
    GoogleCredential,
    OAuthCredential,
    PlaintextCredential,
)
from pony.calendar.oauth import (
    BearerTokenAuth,
    InvalidGrantError,
    OAuthError,
    StoredTokens,
    build_bearer_auth,
    save_tokens,
)
from pony.calendar.paths import CalendarPaths

_COMMAND_TIMEOUT_SECONDS = 30


# Returns fresh tokens for an account by running the OAuth loopback
# flow. Raises `OAuthError` if it can't (e.g. no browser available, or
# the TUI is owning the terminal).
InteractiveAuthorizer = Callable[[str, OAuthCredential, Path], StoredTokens]


class CredentialResolutionError(RuntimeError):
    pass


class DefaultCredentialsProvider:
    """Build an `Authorization` for one account from its credential spec.

    Backends:
      - PlaintextCredential: returns the stored string as a basic-auth
        password.
      - EnvCredential: reads the named environment variable.
      - CommandCredential: runs the command and uses stdout (stripped)
        as the basic-auth password.
      - OAuthCredential / GoogleCredential: load tokens from
        `paths.token_path(account)`. If the file is missing and an
        `interactive_authorizer` is configured, run it to obtain fresh
        tokens and save them. Otherwise raise.

    *paths* says where the tokens live.  It is required for an OAuth
    account and unused for the other three backends, so a caller that
    has no calendar root -- a test over a plaintext credential -- may
    leave it out and gets a clear error if an OAuth account turns up.
    """

    def __init__(
        self,
        env: Mapping[str, str] | None = None,
        *,
        interactive_authorizer: InteractiveAuthorizer | None = None,
        paths: CalendarPaths | None = None,
    ) -> None:
        self._env = env if env is not None else os.environ
        self._authorizer = interactive_authorizer
        self._paths = paths

    def build_auth(self, account: AccountConfig) -> Authorization:
        spec = account.credential
        if isinstance(spec, OAuthCredential):
            return _build_oauth_authorization(
                account.name,
                spec,
                interactive_authorizer=self._authorizer,
                paths=self._paths,
            )
        if isinstance(spec, GoogleCredential):
            return _build_oauth_authorization(
                account.name,
                OAuthCredential(
                    client_id=spec.client_id,
                    client_secret=spec.client_secret,
                    scope=GOOGLE_OAUTH_SCOPE,
                    token_path=None,
                ),
                interactive_authorizer=self._authorizer,
                paths=self._paths,
            )
        password = self._resolve_password(account.name, spec)
        return Authorization(basic=(account.username, password))

    def _resolve_password(self, account_name: str, spec: CredentialSpec) -> str:
        if isinstance(spec, PlaintextCredential):
            return spec.password
        if isinstance(spec, EnvCredential):
            value = self._env.get(spec.variable)
            if value is None:
                raise CredentialResolutionError(
                    f"{account_name}: environment variable {spec.variable!r} is not set"
                )
            return value
        if isinstance(spec, CommandCredential):
            return _resolve_command(account_name, spec)
        # spec narrows to OAuthCredential by elimination, but OAuth is
        # handled in build_auth() before this function is called.
        raise CredentialResolutionError(
            f"{account_name}: OAuth credentials must not be resolved as a "
            "password; this is an internal routing bug."
        )


def _default_token_path(account_name: str, paths: CalendarPaths | None) -> Path:
    """Where this account's tokens go when it names no file of its own.

    Refuses rather than guessing: a calendar that resolved a directory
    itself would write tokens somewhere the host never asked for, which
    is what the provider is handed a `CalendarPaths` to avoid.
    """
    if paths is None:
        raise CredentialResolutionError(
            f"{account_name}: no calendar root to resolve a token path under; "
            "the host must pass `paths` to DefaultCredentialsProvider"
        )
    return paths.token_path(account_name)


def _build_oauth_authorization(
    account_name: str,
    spec: OAuthCredential,
    *,
    interactive_authorizer: InteractiveAuthorizer | None,
    paths: CalendarPaths | None,
) -> Authorization:
    token_path = spec.token_path or _default_token_path(account_name, paths)

    def _new_bearer() -> BearerTokenAuth:
        return build_bearer_auth(
            client_id=spec.client_id,
            client_secret=spec.client_secret,
            scope=spec.scope,
            token_path=token_path,
        )

    def _reauthorize() -> None:
        """Re-run the OAuth flow after a revoked refresh token."""
        if interactive_authorizer is None:
            raise InvalidGrantError(
                f"{account_name}: refresh token has been revoked — "
                "run `pony calendar sync` from an interactive terminal to re-authorize"
            )
        token_path.unlink(missing_ok=True)
        try:
            tokens = interactive_authorizer(account_name, spec, token_path)
        except OAuthError as exc:
            raise OAuthError(f"{account_name}: re-authorization failed: {exc}") from exc
        save_tokens(token_path, tokens)

    if not token_path.exists():
        if interactive_authorizer is None:
            raise CredentialResolutionError(
                f"{account_name}: no stored OAuth tokens at {token_path}. "
                "Run `pony calendar sync` from an interactive terminal to authorize."
            )
        try:
            tokens = interactive_authorizer(account_name, spec, token_path)
        except OAuthError as exc:
            raise CredentialResolutionError(f"{account_name}: {exc}") from exc
        save_tokens(token_path, tokens)

    try:
        slot = [_new_bearer()]
    except OAuthError as exc:
        raise CredentialResolutionError(f"{account_name}: {exc}") from exc

    def get_header() -> str:
        try:
            return slot[0].get_header()
        except InvalidGrantError:
            _reauthorize()
            slot[0] = _new_bearer()
            return slot[0].get_header()

    def on_commit() -> None:
        slot[0].persist()

    return Authorization(bearer_token_fn=get_header, on_commit=on_commit)


def _resolve_command(account_name: str, spec: CommandCredential) -> str:
    try:
        completed = subprocess.run(
            list(spec.command),
            capture_output=True,
            check=True,
            text=True,
            timeout=_COMMAND_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:
        raise CredentialResolutionError(
            f"{account_name}: credential command not found: {spec.command[0]!r}"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CredentialResolutionError(
            f"{account_name}: credential command timed out after "
            f"{_COMMAND_TIMEOUT_SECONDS}s"
        ) from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() if exc.stderr else ""
        raise CredentialResolutionError(
            f"{account_name}: credential command exited {exc.returncode}"
            + (f": {stderr}" if stderr else "")
        ) from exc
    return completed.stdout.rstrip("\n")


__all__ = [
    "CredentialResolutionError",
    "DefaultCredentialsProvider",
    "InteractiveAuthorizer",
]

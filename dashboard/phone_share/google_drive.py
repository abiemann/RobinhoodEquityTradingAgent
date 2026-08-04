"""Google Drive ``appDataFolder`` storage for encrypted phone snapshots.

This module deliberately uses only the Python standard library.  It stores the
existing encrypted phone-share envelope as an opaque JSON document.  It does
not encrypt, decrypt, inspect, or persist OAuth credentials.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple


DRIVE_APPDATA_SCOPE = "https://www.googleapis.com/auth/drive.appdata"
GOOGLE_AUTHORIZATION_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_DRIVE_FILES_ENDPOINT = "https://www.googleapis.com/drive/v3/files"
GOOGLE_DRIVE_UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/drive/v3/files"

ENVELOPE_KEYS = (
    "schema_version",
    "share_id",
    "sequence",
    "captured_at",
    "expires_at",
    "iv",
    "ciphertext",
)
SHARE_ID_RE = re.compile(r"[A-Za-z0-9_-]{22,64}\Z")
BASE64URL_RE = re.compile(r"[A-Za-z0-9_-]+\Z")
TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\Z"
)
DRIVE_FILE_ID_RE = re.compile(r"[A-Za-z0-9_-]{8,256}\Z")
SAFE_TOKEN_RE = re.compile(r"[^\x00-\x20\x7f]{8,4096}\Z")

MAX_CIPHERTEXT_BYTES = 262144
MAX_ENVELOPE_BYTES = 393216
MAX_TTL_SECONDS = 8 * 60 * 60
MAX_CLOCK_SKEW_SECONDS = 2 * 60
MAX_OAUTH_RESPONSE_BYTES = 65536


class PhoneShareProviderError(RuntimeError):
    """A deliberately safe error suitable for display or logging.

    Upstream response bodies, OAuth codes, and credentials are never included
    in this exception's text or representation.
    """

    def __init__(self, code: str, safe_message: str):
        super().__init__(safe_message)
        self.code = code
        self.safe_message = safe_message

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(code={self.code!r}, "
            f"safe_message={self.safe_message!r})"
        )


@dataclass(frozen=True)
class HttpResponse:
    """A small, bounded HTTP response returned by a transport."""

    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        return None


class UrlLibTransport:
    """HTTPS transport with redirect rejection and bounded response reads."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[Mapping[str, str]] = None,
        body: Optional[bytes] = None,
        timeout: float = 10,
        max_response_bytes: int = MAX_ENVELOPE_BYTES,
    ) -> HttpResponse:
        if method not in {"GET", "POST", "PATCH", "DELETE"}:
            raise PhoneShareProviderError(
                "invalid_http_method", "The storage request method is invalid."
            )
        parsed = urllib.parse.urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise PhoneShareProviderError(
                "invalid_endpoint", "The storage endpoint is invalid."
            )
        if (
            isinstance(max_response_bytes, bool)
            or not isinstance(max_response_bytes, int)
            or not 1 <= max_response_bytes <= 1048576
        ):
            raise PhoneShareProviderError(
                "invalid_response_limit", "The response-size limit is invalid."
            )

        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers or {}),
            method=method,
        )
        opener = urllib.request.build_opener(_NoRedirectHandler())
        response = None
        try:
            try:
                response = opener.open(request, timeout=timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            except (OSError, urllib.error.URLError, ValueError):
                raise PhoneShareProviderError(
                    "network_unavailable",
                    "The encrypted phone-share storage service is unavailable.",
                ) from None

            final_url = response.geturl()
            if final_url != url:
                raise PhoneShareProviderError(
                    "redirect_rejected",
                    "The storage service attempted an unexpected redirect.",
                )
            raw_length = response.headers.get("Content-Length")
            if raw_length is not None:
                try:
                    content_length = int(raw_length)
                except (TypeError, ValueError):
                    raise PhoneShareProviderError(
                        "invalid_http_response",
                        "The storage service returned an invalid response.",
                    ) from None
                if content_length < 0 or content_length > max_response_bytes:
                    raise PhoneShareProviderError(
                        "response_too_large",
                        "The storage service response exceeded the safe limit.",
                    )
            response_body = response.read(max_response_bytes + 1)
            if len(response_body) > max_response_bytes:
                raise PhoneShareProviderError(
                    "response_too_large",
                    "The storage service response exceeded the safe limit.",
                )
            response_headers = {
                str(key).lower(): str(value)
                for key, value in response.headers.items()
            }
            return HttpResponse(
                status=int(response.status),
                headers=response_headers,
                body=response_body,
                url=final_url,
            )
        finally:
            if response is not None:
                response.close()


@dataclass(frozen=True)
class GoogleDriveConfig:
    """Strict configuration for a Google desktop OAuth client."""

    client_id: str
    redirect_uri: str
    request_timeout_seconds: float = 10
    max_response_bytes: int = 524288
    refresh_leeway_seconds: int = 60

    def __post_init__(self) -> None:
        suffix = ".apps.googleusercontent.com"
        if (
            not isinstance(self.client_id, str)
            or not self.client_id.endswith(suffix)
            or not 32 <= len(self.client_id) <= 512
            or not re.fullmatch(r"[A-Za-z0-9._-]+", self.client_id)
        ):
            raise PhoneShareProviderError(
                "invalid_google_client_id",
                "The Google OAuth desktop client ID is invalid.",
            )

        if not isinstance(self.redirect_uri, str):
            raise PhoneShareProviderError(
                "invalid_oauth_redirect", "The OAuth callback address is invalid."
            )
        try:
            parsed = urllib.parse.urlsplit(self.redirect_uri)
            port = parsed.port
        except ValueError:
            parsed = None
            port = None
        if (
            parsed is None
            or parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or port is None
            or not 1024 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path != "/oauth2/callback"
            or parsed.query
            or parsed.fragment
        ):
            raise PhoneShareProviderError(
                "invalid_oauth_redirect", "The OAuth callback address is invalid."
            )

        if (
            isinstance(self.request_timeout_seconds, bool)
            or not isinstance(self.request_timeout_seconds, (int, float))
            or not 1 <= self.request_timeout_seconds <= 30
        ):
            raise PhoneShareProviderError(
                "invalid_request_timeout", "The storage request timeout is invalid."
            )
        if (
            isinstance(self.max_response_bytes, bool)
            or not isinstance(self.max_response_bytes, int)
            or not MAX_ENVELOPE_BYTES
            <= self.max_response_bytes
            <= 1048576
        ):
            raise PhoneShareProviderError(
                "invalid_response_limit", "The response-size limit is invalid."
            )
        if (
            isinstance(self.refresh_leeway_seconds, bool)
            or not isinstance(self.refresh_leeway_seconds, int)
            or not 0 <= self.refresh_leeway_seconds <= 300
        ):
            raise PhoneShareProviderError(
                "invalid_refresh_leeway", "The OAuth refresh window is invalid."
            )


@dataclass(frozen=True, repr=False)
class OAuthAuthorizationRequest:
    authorization_url: str
    redirect_uri: str
    state: str = field(repr=False)
    code_verifier: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(authorization_url=<redacted>, "
            f"redirect_uri={self.redirect_uri!r}, state=<redacted>, "
            "code_verifier=<redacted>)"
        )


@dataclass(frozen=True, repr=False)
class OAuthCredentials:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    expires_at: float
    scopes: Tuple[str, ...]
    token_type: str = "Bearer"

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}(access_token=<redacted>, "
            f"refresh_token=<redacted>, expires_at={self.expires_at!r}, "
            f"scopes={self.scopes!r}, token_type={self.token_type!r})"
        )


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _safe_header_token(value: Any, name: str) -> str:
    if not isinstance(value, str) or not SAFE_TOKEN_RE.fullmatch(value):
        raise PhoneShareProviderError(
            "invalid_oauth_token", f"The Google OAuth {name} is invalid."
        )
    return value


def _json_object(body: bytes, code: str, message: str) -> Dict[str, Any]:
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PhoneShareProviderError(code, message) from None
    if not isinstance(value, dict):
        raise PhoneShareProviderError(code, message)
    return value


class InMemoryOAuthSession:
    """One-process OAuth credentials with PKCE and automatic refresh."""

    def __init__(
        self,
        config: GoogleDriveConfig,
        transport: Optional[Any] = None,
        *,
        clock: Callable[[], float] = time.time,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
    ):
        if not isinstance(config, GoogleDriveConfig):
            raise PhoneShareProviderError(
                "invalid_provider_config", "The Google Drive configuration is invalid."
            )
        if not callable(clock) or not callable(random_bytes):
            raise PhoneShareProviderError(
                "invalid_oauth_runtime", "The OAuth runtime configuration is invalid."
            )
        self.config = config
        self.transport = transport or UrlLibTransport()
        self._clock = clock
        self._random_bytes = random_bytes
        self._credentials: Optional[OAuthCredentials] = None
        self._credentials_changed_callback: Optional[
            Callable[[OAuthCredentials], Any]
        ] = None
        self._credentials_cleared_callback: Optional[Callable[[], Any]] = None
        self._pending: Dict[str, str] = {}

    @property
    def is_authorized(self) -> bool:
        return self._credentials is not None

    def begin_authorization(
        self, login_hint: Optional[str] = None
    ) -> OAuthAuthorizationRequest:
        if login_hint is not None and (
            not isinstance(login_hint, str)
            or not 3 <= len(login_hint) <= 320
            or any(ord(character) <= 32 or ord(character) == 127 for character in login_hint)
        ):
            raise PhoneShareProviderError(
                "invalid_login_hint", "The Google account hint is invalid."
            )
        state_bytes = self._random_bytes(32)
        verifier_bytes = self._random_bytes(64)
        if (
            not isinstance(state_bytes, bytes)
            or len(state_bytes) != 32
            or not isinstance(verifier_bytes, bytes)
            or len(verifier_bytes) != 64
        ):
            raise PhoneShareProviderError(
                "oauth_randomness_failed", "Secure OAuth setup could not be started."
            )
        state = _base64url(state_bytes)
        verifier = _base64url(verifier_bytes)
        challenge = _base64url(hashlib.sha256(verifier.encode("ascii")).digest())
        self._pending.clear()
        self._pending[state] = verifier
        parameters = {
            "access_type": "offline",
            "client_id": self.config.client_id,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "include_granted_scopes": "false",
            "prompt": "consent",
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": DRIVE_APPDATA_SCOPE,
            "state": state,
        }
        if login_hint is not None:
            parameters["login_hint"] = login_hint
        authorization_url = (
            GOOGLE_AUTHORIZATION_ENDPOINT
            + "?"
            + urllib.parse.urlencode(parameters)
        )
        return OAuthAuthorizationRequest(
            authorization_url=authorization_url,
            redirect_uri=self.config.redirect_uri,
            state=state,
            code_verifier=verifier,
        )

    def authorize(
        self,
        open_browser: Callable[[str], Any],
        receive_callback: Callable[[str], str],
        login_hint: Optional[str] = None,
    ) -> OAuthCredentials:
        if not callable(open_browser) or not callable(receive_callback):
            raise PhoneShareProviderError(
                "invalid_oauth_callback", "The OAuth callback handler is invalid."
            )
        pending = self.begin_authorization(login_hint)
        try:
            open_browser(pending.authorization_url)
            callback_url = receive_callback(pending.redirect_uri)
        except Exception:
            self._pending.pop(pending.state, None)
            raise PhoneShareProviderError(
                "oauth_interrupted", "Google authorization was interrupted."
            ) from None
        return self.complete_authorization(callback_url, pending)

    def complete_authorization(
        self, callback_url: str, pending: OAuthAuthorizationRequest
    ) -> OAuthCredentials:
        if not isinstance(pending, OAuthAuthorizationRequest):
            raise PhoneShareProviderError(
                "invalid_oauth_state", "The Google authorization state is invalid."
            )
        verifier = self._pending.pop(pending.state, None)
        if verifier is None or not hmac.compare_digest(verifier, pending.code_verifier):
            raise PhoneShareProviderError(
                "invalid_oauth_state", "The Google authorization state is invalid."
            )
        code = self._parse_callback(callback_url, pending.state)
        form = urllib.parse.urlencode(
            {
                "client_id": self.config.client_id,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": self.config.redirect_uri,
            }
        ).encode("ascii")
        document = self._token_request(form, "oauth_exchange_failed")
        credentials = self._credentials_from_document(document, require_refresh=True)
        self._credentials = credentials
        self._notify_credentials_changed(credentials)
        return credentials

    def credentials_snapshot(self) -> Optional[OAuthCredentials]:
        """Return a redacting immutable copy for a native credential store."""
        credentials = self._credentials
        return None if credentials is None else replace(credentials)

    def set_credentials_changed_callback(
        self, callback: Optional[Callable[[OAuthCredentials], Any]]
    ) -> None:
        """Observe newly issued credentials without exposing them in logs.

        Restoring credentials with :meth:`set_credentials` intentionally does
        not call the observer. OAuth completion and automatic refresh do.
        Persistence failures are non-fatal: the active process may continue
        with its in-memory credential and report that persistence is disabled.
        """
        if callback is not None and not callable(callback):
            raise PhoneShareProviderError(
                "invalid_oauth_runtime", "The OAuth runtime configuration is invalid."
            )
        self._credentials_changed_callback = callback

    def _notify_credentials_changed(self, credentials: OAuthCredentials) -> None:
        callback = self._credentials_changed_callback
        if callback is None:
            return
        try:
            callback(replace(credentials))
        except Exception:
            # Native persistence is a convenience. It must never break a
            # freshly authorized in-memory session or surface credential data.
            return

    def set_credentials_cleared_callback(
        self, callback: Optional[Callable[[], Any]]
    ) -> None:
        """Observe terminal credential invalidation without exposing secrets."""
        if callback is not None and not callable(callback):
            raise PhoneShareProviderError(
                "invalid_oauth_runtime", "The OAuth runtime configuration is invalid."
            )
        self._credentials_cleared_callback = callback

    def clear_credentials(self) -> None:
        """Forget revoked credentials and notify the native encrypted store."""
        had_credentials = self._credentials is not None
        self._credentials = None
        if not had_credentials:
            return
        callback = self._credentials_cleared_callback
        if callback is None:
            return
        try:
            callback()
        except Exception:
            # A failed cleanup must not resurrect credentials or reveal them.
            return

    def set_credentials(self, credentials: OAuthCredentials) -> None:
        if not isinstance(credentials, OAuthCredentials):
            raise PhoneShareProviderError(
                "invalid_oauth_credentials", "The Google OAuth credentials are invalid."
            )
        access_token = _safe_header_token(credentials.access_token, "access token")
        refresh_token = _safe_header_token(credentials.refresh_token, "refresh token")
        if (
            isinstance(credentials.expires_at, bool)
            or not isinstance(credentials.expires_at, (int, float))
            or credentials.expires_at <= 0
            or credentials.token_type != "Bearer"
            or tuple(credentials.scopes) != (DRIVE_APPDATA_SCOPE,)
        ):
            raise PhoneShareProviderError(
                "invalid_oauth_credentials", "The Google OAuth credentials are invalid."
            )
        self._credentials = replace(
            credentials,
            access_token=access_token,
            refresh_token=refresh_token,
            scopes=(DRIVE_APPDATA_SCOPE,),
        )

    def authorization_header(self) -> str:
        credentials = self._credentials
        if credentials is None:
            raise PhoneShareProviderError(
                "google_authorization_required",
                "Connect a Google account before using View on Phone.",
            )
        if credentials.expires_at <= self._clock() + self.config.refresh_leeway_seconds:
            try:
                credentials = self._refresh(credentials)
            except PhoneShareProviderError as exc:
                if exc.code != "oauth_refresh_revoked":
                    raise
                self.clear_credentials()
                raise PhoneShareProviderError(
                    "google_authorization_required",
                    "Connect a Google account before using View on Phone.",
                ) from None
            self._credentials = credentials
            self._notify_credentials_changed(credentials)
        return "Bearer " + credentials.access_token

    def _parse_callback(self, callback_url: str, expected_state: str) -> str:
        if not isinstance(callback_url, str) or len(callback_url) > 8192:
            raise PhoneShareProviderError(
                "invalid_oauth_callback", "The Google authorization callback is invalid."
            )
        try:
            callback = urllib.parse.urlsplit(callback_url)
            expected = urllib.parse.urlsplit(self.config.redirect_uri)
        except ValueError:
            raise PhoneShareProviderError(
                "invalid_oauth_callback", "The Google authorization callback is invalid."
            ) from None
        if (
            callback.scheme != expected.scheme
            or callback.hostname != expected.hostname
            or callback.port != expected.port
            or callback.path != expected.path
            or callback.fragment
            or callback.username is not None
            or callback.password is not None
        ):
            raise PhoneShareProviderError(
                "invalid_oauth_callback", "The Google authorization callback is invalid."
            )
        try:
            pairs = urllib.parse.parse_qsl(
                callback.query, keep_blank_values=True, strict_parsing=True
            )
        except ValueError:
            raise PhoneShareProviderError(
                'invalid_oauth_callback', 'The Google authorization callback is invalid.'
            ) from None
        values: Dict[str, list[str]] = {}
        for key, value in pairs:
            values.setdefault(key, []).append(value)
        state_values = values.get("state", [])
        if (
            len(state_values) != 1
            or not hmac.compare_digest(state_values[0], expected_state)
        ):
            raise PhoneShareProviderError(
                "invalid_oauth_state", "The Google authorization state is invalid."
            )
        if "error" in values:
            raise PhoneShareProviderError(
                "oauth_authorization_denied", "Google authorization was not completed."
            )
        code_values = values.get("code", [])
        if len(code_values) != 1:
            raise PhoneShareProviderError(
                "invalid_oauth_callback", "The Google authorization callback is invalid."
            )
        return _safe_header_token(code_values[0], "authorization code")

    def _token_request(self, form: bytes, error_code: str) -> Dict[str, Any]:
        response = self.transport.request(
            "POST",
            GOOGLE_TOKEN_ENDPOINT,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "RHMRA-Dashboard/2",
            },
            body=form,
            timeout=self.config.request_timeout_seconds,
            max_response_bytes=MAX_OAUTH_RESPONSE_BYTES,
        )
        if response.url != GOOGLE_TOKEN_ENDPOINT:
            raise PhoneShareProviderError(
                error_code, "Google OAuth could not issue a storage credential."
            )
        if response.status != 200:
            if error_code == "oauth_refresh_failed" and response.status in {400, 401}:
                try:
                    document = _json_object(
                        response.body,
                        error_code,
                        "Google OAuth returned an invalid credential response.",
                    )
                except PhoneShareProviderError:
                    document = {}
                if document.get("error") == "invalid_grant":
                    raise PhoneShareProviderError(
                        "oauth_refresh_revoked",
                        "The Google Drive connection has expired; reconnect it.",
                    )
            raise PhoneShareProviderError(
                error_code, "Google OAuth could not issue a storage credential."
            )
        return _json_object(
            response.body,
            error_code,
            "Google OAuth returned an invalid credential response.",
        )

    def _credentials_from_document(
        self,
        document: Mapping[str, Any],
        *,
        previous: Optional[OAuthCredentials] = None,
        require_refresh: bool = False,
    ) -> OAuthCredentials:
        access_token = _safe_header_token(document.get("access_token"), "access token")
        token_type = document.get("token_type")
        expires_in = document.get("expires_in")
        if (
            token_type != "Bearer"
            or isinstance(expires_in, bool)
            or not isinstance(expires_in, int)
            or not 60 <= expires_in <= 86400
        ):
            raise PhoneShareProviderError(
                "invalid_oauth_credentials", "Google OAuth returned invalid credentials."
            )
        refresh_value = document.get("refresh_token")
        if refresh_value is None and previous is not None:
            refresh_value = previous.refresh_token
        if require_refresh and refresh_value is None:
            raise PhoneShareProviderError(
                "missing_refresh_token",
                "Google did not return an offline storage credential; reconnect the account.",
            )
        refresh_token = _safe_header_token(refresh_value, "refresh token")

        raw_scope = document.get("scope")
        if raw_scope is None and previous is not None:
            scopes = previous.scopes
        elif isinstance(raw_scope, str):
            scopes = tuple(raw_scope.split())
        else:
            scopes = ()
        if scopes != (DRIVE_APPDATA_SCOPE,):
            raise PhoneShareProviderError(
                "invalid_oauth_scope",
                "Google did not grant the required private app-data permission.",
            )
        return OAuthCredentials(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=float(self._clock()) + expires_in,
            scopes=(DRIVE_APPDATA_SCOPE,),
        )

    def _refresh(self, credentials: OAuthCredentials) -> OAuthCredentials:
        form = urllib.parse.urlencode(
            {
                "client_id": self.config.client_id,
                "grant_type": "refresh_token",
                "refresh_token": credentials.refresh_token,
            }
        ).encode("ascii")
        document = self._token_request(form, "oauth_refresh_failed")
        return self._credentials_from_document(document, previous=credentials)


def _parse_timestamp(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not TIMESTAMP_RE.fullmatch(value):
        raise PhoneShareProviderError(
            "invalid_envelope", f"The encrypted envelope {field_name} is invalid."
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise PhoneShareProviderError(
            "invalid_envelope", f"The encrypted envelope {field_name} is invalid."
        ) from None
    if parsed.tzinfo != timezone.utc:
        raise PhoneShareProviderError(
            "invalid_envelope", f"The encrypted envelope {field_name} is invalid."
        )
    return parsed


def _decoded_base64url(value: Any, field_name: str) -> bytes:
    if (
        not isinstance(value, str)
        or not BASE64URL_RE.fullmatch(value)
        or len(value) % 4 == 1
    ):
        raise PhoneShareProviderError(
            "invalid_envelope", f"The encrypted envelope {field_name} is invalid."
        )
    try:
        decoded = base64.b64decode(
            value + ("=" * (-len(value) % 4)), altchars=b"-_", validate=True
        )
    except (ValueError, TypeError):
        raise PhoneShareProviderError(
            "invalid_envelope", f"The encrypted envelope {field_name} is invalid."
        ) from None
    if _base64url(decoded) != value:
        raise PhoneShareProviderError(
            "invalid_envelope", f"The encrypted envelope {field_name} is invalid."
        )
    return decoded


def validate_envelope(
    envelope: Mapping[str, Any], *, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Validate and copy the existing encrypted wire envelope unchanged."""

    if not isinstance(envelope, dict) or set(envelope) != set(ENVELOPE_KEYS):
        raise PhoneShareProviderError(
            "invalid_envelope", "The encrypted phone-share envelope is invalid."
        )
    if envelope.get("schema_version") != 1:
        raise PhoneShareProviderError(
            "invalid_envelope", "The encrypted phone-share envelope is invalid."
        )
    share_id = envelope.get("share_id")
    if not isinstance(share_id, str) or not SHARE_ID_RE.fullmatch(share_id):
        raise PhoneShareProviderError(
            "invalid_share_id", "The encrypted phone-share ID is invalid."
        )
    sequence = envelope.get("sequence")
    if (
        isinstance(sequence, bool)
        or not isinstance(sequence, int)
        or not 1 <= sequence <= 9007199254740991
    ):
        raise PhoneShareProviderError(
            "invalid_envelope", "The encrypted phone-share sequence is invalid."
        )
    captured_at = _parse_timestamp(envelope.get("captured_at"), "capture time")
    expires_at = _parse_timestamp(envelope.get("expires_at"), "expiration time")
    lifetime = (expires_at - captured_at).total_seconds()
    if lifetime <= 0 or lifetime > MAX_TTL_SECONDS:
        raise PhoneShareProviderError(
            "invalid_envelope", "The encrypted phone-share lifetime is invalid."
        )
    if now is not None:
        if not isinstance(now, datetime) or now.tzinfo != timezone.utc:
            raise PhoneShareProviderError(
                "invalid_envelope", "The encrypted phone-share clock is invalid."
            )
        skew = timedelta(seconds=MAX_CLOCK_SKEW_SECONDS)
        if (
            captured_at > now + skew
            or expires_at <= now
            or expires_at > now + timedelta(seconds=MAX_TTL_SECONDS) + skew
        ):
            raise PhoneShareProviderError(
                "invalid_envelope", "The encrypted phone-share timing is invalid."
            )
    iv = _decoded_base64url(envelope.get("iv"), "IV")
    ciphertext = _decoded_base64url(envelope.get("ciphertext"), "ciphertext")
    if len(iv) != 12 or not 16 <= len(ciphertext) <= MAX_CIPHERTEXT_BYTES:
        raise PhoneShareProviderError(
            "invalid_envelope", "The encrypted phone-share payload is invalid."
        )
    result = {key: envelope[key] for key in ENVELOPE_KEYS}
    encoded = json.dumps(
        result, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    if len(encoded) > MAX_ENVELOPE_BYTES:
        raise PhoneShareProviderError(
            "invalid_envelope", "The encrypted phone-share envelope is too large."
        )
    return result


def phone_share_filename(share_id: str) -> str:
    if not isinstance(share_id, str) or not SHARE_ID_RE.fullmatch(share_id):
        raise PhoneShareProviderError(
            "invalid_share_id", "The encrypted phone-share ID is invalid."
        )
    return f"rhmra-phone-v2-{share_id}.json"


@dataclass(frozen=True)
class DriveFile:
    file_id: str
    name: str
    modified_time: Optional[str] = None
    size: Optional[int] = None


class GoogleDriveProvider:
    """Exact-name CRUD for encrypted snapshots in Drive ``appDataFolder``."""

    def __init__(
        self,
        config: GoogleDriveConfig,
        session: InMemoryOAuthSession,
        transport: Optional[Any] = None,
        *,
        random_bytes: Callable[[int], bytes] = secrets.token_bytes,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        if not isinstance(config, GoogleDriveConfig) or not isinstance(
            session, InMemoryOAuthSession
        ):
            raise PhoneShareProviderError(
                "invalid_provider_config", "The Google Drive provider is invalid."
            )
        if (
            session.config != config
            or not callable(random_bytes)
            or not callable(clock)
        ):
            raise PhoneShareProviderError(
                "invalid_provider_config", "The Google Drive provider is invalid."
            )
        self.config = config
        self.session = session
        self.transport = transport or session.transport
        self._random_bytes = random_bytes
        self._clock = clock

    def list_file(self, share_id: str) -> Optional[DriveFile]:
        files = self._list_exact_files(share_id)
        return files[0] if files else None

    def _list_exact_files(
        self, share_id: str, *, allow_duplicates: bool = False
    ) -> Tuple[DriveFile, ...]:
        name = phone_share_filename(share_id)
        query = f"'appDataFolder' in parents and name = '{name}' and trashed = false"
        parameters = urllib.parse.urlencode(
            {
                "fields": "files(id,name,modifiedTime,size),nextPageToken",
                "pageSize": "2",
                "q": query,
                "spaces": "appDataFolder",
            }
        )
        url = GOOGLE_DRIVE_FILES_ENDPOINT + "?" + parameters
        response = self._request("GET", url)
        if response.status != 200:
            self._raise_drive_status(response.status)
        document = _json_object(
            response.body,
            "invalid_drive_response",
            "Google Drive returned an invalid file listing.",
        )
        files = document.get("files")
        if not isinstance(files, list) or len(files) > 2:
            raise PhoneShareProviderError(
                "invalid_drive_response", "Google Drive returned an invalid file listing."
            )
        if document.get("nextPageToken") or (
            len(files) > 1 and not allow_duplicates
        ):
            raise PhoneShareProviderError(
                "duplicate_drive_file",
                "More than one encrypted snapshot has the same private filename.",
            )
        return tuple(self._parse_drive_file(item, name) for item in files)

    def create_envelope(self, envelope: Mapping[str, Any]) -> DriveFile:
        value = self._validate_incoming(envelope)
        if self.list_file(value["share_id"]) is not None:
            raise PhoneShareProviderError(
                "drive_file_exists", "The encrypted snapshot already exists."
            )
        created = self._create(value)
        return self._reconcile_created_envelope(value, created)

    def update_envelope(self, envelope: Mapping[str, Any]) -> DriveFile:
        value = self._validate_incoming(envelope)
        existing = self.list_file(value["share_id"])
        if existing is None:
            raise PhoneShareProviderError(
                "drive_file_not_found", "The encrypted snapshot does not exist."
            )
        current, etag = self._read_existing_envelope(existing, value["share_id"])
        if current is None:
            raise PhoneShareProviderError(
                "drive_file_not_found", "The encrypted snapshot does not exist."
            )
        if self._compare_envelopes(current, value) == "idempotent":
            return existing
        return self._update(existing, value, if_match=etag)

    def put_envelope(self, envelope: Mapping[str, Any]) -> DriveFile:
        value = self._validate_incoming(envelope)
        # The local dashboard server serializes calls, but Drive is still an
        # external shared boundary. Read and compare before every write so a
        # delayed tab cannot replace a newer sequence with an older one. When
        # Drive supplies an ETag, If-Match closes the final read/write window.
        for attempt in range(3):
            existing = self.list_file(value["share_id"])
            if existing is None:
                created = self._create(value)
                return self._reconcile_created_envelope(value, created)
            current, etag = self._read_existing_envelope(
                existing, value["share_id"]
            )
            if current is None:
                if attempt < 2:
                    continue
                raise PhoneShareProviderError(
                    "drive_conflict",
                    "The encrypted snapshot changed while it was being updated.",
                )
            if self._compare_envelopes(current, value) == "idempotent":
                return existing
            try:
                return self._update(existing, value, if_match=etag)
            except PhoneShareProviderError as exc:
                if exc.code != "drive_conflict" or attempt >= 2:
                    raise
        raise PhoneShareProviderError(
            "drive_conflict",
            "The encrypted snapshot changed while it was being updated.",
        )

    def _reconcile_created_envelope(
        self, value: Mapping[str, Any], created: DriveFile
    ) -> DriveFile:
        """Resolve the exact-name create race without weakening monotonicity.

        Drive appDataFolder does not provide a unique-name constraint. Two
        dashboard processes can therefore both observe no file and create one.
        Re-list after every create, choose the same canonical candidate in all
        contenders, remove only exact file IDs, and then re-list before success.
        """
        for attempt in range(4):
            files = self._list_exact_files(
                value["share_id"], allow_duplicates=True
            )
            if not files:
                continue
            if len(files) == 1:
                existing = files[0]
                if existing.file_id == created.file_id:
                    return existing
                current, etag = self._read_existing_envelope(
                    existing, value["share_id"]
                )
                if current is None:
                    continue
                if self._compare_envelopes(current, value) == "idempotent":
                    return existing
                try:
                    return self._update(existing, value, if_match=etag)
                except PhoneShareProviderError as exc:
                    if exc.code != "drive_conflict" or attempt >= 3:
                        raise
                    continue

            records = []
            for existing in files:
                current, etag = self._read_existing_envelope(
                    existing, value["share_id"]
                )
                if current is None:
                    records = []
                    break
                records.append((existing, current, etag))
            if len(records) != len(files):
                continue

            canonical = self._canonical_duplicate(records)
            for existing, unused_envelope, unused_etag in records:
                if existing.file_id != canonical[0].file_id:
                    self._delete_file(existing)
            # Re-list rather than trusting delete visibility. A third writer
            # may have raced the cleanup, and only one exact name is safe.

        raise PhoneShareProviderError(
            "drive_conflict",
            "The encrypted snapshot changed while it was being created.",
        )

    @staticmethod
    def _canonical_duplicate(records):
        """Return one deterministic record, preferring a monotonic successor."""
        winner = records[0]
        for candidate in records[1:]:
            winner_file, winner_value, unused_winner_etag = winner
            candidate_file, candidate_value, unused_candidate_etag = candidate
            if dict(candidate_value) == dict(winner_value):
                if candidate_file.file_id < winner_file.file_id:
                    winner = candidate
                continue
            if (
                candidate_value["sequence"] > winner_value["sequence"]
                and candidate_value["captured_at"] >= winner_value["captured_at"]
            ):
                winner = candidate
                continue
            if (
                candidate_value["sequence"] < winner_value["sequence"]
                and candidate_value["captured_at"] <= winner_value["captured_at"]
            ):
                continue
            # Equivocating sequence/timestamp pairs have no safe semantic
            # ordering. File ID is an opaque, stable tiebreaker so concurrent
            # reconcilers still preserve exactly the same candidate.
            if candidate_file.file_id < winner_file.file_id:
                winner = candidate
        return winner

    def get_envelope(self, share_id: str) -> Optional[Dict[str, Any]]:
        existing = self.list_file(share_id)
        if existing is None:
            return None
        value, unused_etag = self._read_existing_envelope(existing, share_id)
        return value

    def _validate_incoming(
        self, envelope: Mapping[str, Any]
    ) -> Dict[str, Any]:
        now = self._clock()
        return validate_envelope(envelope, now=now)

    def _read_existing_envelope(
        self, existing: DriveFile, share_id: str
    ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        file_id = urllib.parse.quote(existing.file_id, safe="")
        url = GOOGLE_DRIVE_FILES_ENDPOINT + "/" + file_id + "?alt=media"
        response = self._request("GET", url)
        if response.status == 404:
            return None, None
        if response.status != 200:
            self._raise_drive_status(response.status)
        document = _json_object(
            response.body,
            "invalid_drive_envelope",
            "Google Drive returned an invalid encrypted snapshot.",
        )
        value = validate_envelope(document)
        if value["share_id"] != share_id:
            raise PhoneShareProviderError(
                "drive_share_mismatch",
                "The private snapshot did not match the requested share.",
            )
        etag = response.headers.get("etag")
        if etag is not None and (
            not isinstance(etag, str)
            or not 3 <= len(etag) <= 512
            or any(
                ord(character) < 33 or ord(character) == 127
                for character in etag
            )
            or not (etag.startswith('"') or etag.startswith('W/"'))
            or not etag.endswith('"')
        ):
            raise PhoneShareProviderError(
                "invalid_drive_response", "Google Drive returned an invalid ETag."
            )
        return value, etag

    @staticmethod
    def _compare_envelopes(
        current: Mapping[str, Any], incoming: Mapping[str, Any]
    ) -> str:
        current_sequence = current["sequence"]
        incoming_sequence = incoming["sequence"]
        if incoming_sequence < current_sequence:
            raise PhoneShareProviderError(
                "stale_envelope",
                "A newer encrypted snapshot has already been uploaded.",
            )
        if incoming_sequence == current_sequence:
            if dict(current) == dict(incoming):
                return "idempotent"
            raise PhoneShareProviderError(
                "envelope_conflict",
                "The encrypted snapshot sequence is already in use.",
            )
        if incoming["captured_at"] < current["captured_at"]:
            raise PhoneShareProviderError(
                "stale_envelope",
                "A newer encrypted snapshot has already been uploaded.",
            )
        return "update"

    def delete_envelope(self, share_id: str) -> bool:
        existing = self.list_file(share_id)
        if existing is None:
            return False
        return self._delete_file(existing)

    def _delete_file(self, existing: DriveFile) -> bool:
        file_id = urllib.parse.quote(existing.file_id, safe="")
        response = self._request("DELETE", GOOGLE_DRIVE_FILES_ENDPOINT + "/" + file_id)
        if response.status == 404:
            return False
        if response.status not in {200, 204}:
            self._raise_drive_status(response.status)
        if response.body not in {b"", b"{}"}:
            raise PhoneShareProviderError(
                "invalid_drive_response", "Google Drive returned an invalid delete response."
            )
        return True

    def _create(self, envelope: Mapping[str, Any]) -> DriveFile:
        name = phone_share_filename(envelope["share_id"])
        boundary_bytes = self._random_bytes(18)
        if not isinstance(boundary_bytes, bytes) or len(boundary_bytes) != 18:
            raise PhoneShareProviderError(
                "multipart_randomness_failed", "The encrypted upload could not be prepared."
            )
        boundary = "rhmra_" + boundary_bytes.hex()
        metadata = json.dumps(
            {"name": name, "parents": ["appDataFolder"]},
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
        payload = self._encode_envelope(envelope)
        body = b"".join(
            (
                f"--{boundary}\r\n".encode("ascii"),
                b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
                metadata,
                f"\r\n--{boundary}\r\n".encode("ascii"),
                b"Content-Type: application/json\r\n\r\n",
                payload,
                f"\r\n--{boundary}--\r\n".encode("ascii"),
            )
        )
        url = GOOGLE_DRIVE_UPLOAD_ENDPOINT + "?" + urllib.parse.urlencode(
            {"fields": "id,name,modifiedTime,size", "uploadType": "multipart"}
        )
        response = self._request(
            "POST",
            url,
            body=body,
            content_type=f"multipart/related; boundary={boundary}",
        )
        if response.status not in {200, 201}:
            self._raise_drive_status(response.status)
        document = _json_object(
            response.body,
            "invalid_drive_response",
            "Google Drive returned invalid file metadata.",
        )
        return self._parse_drive_file(document, name)

    def _update(
        self,
        existing: DriveFile,
        envelope: Mapping[str, Any],
        *,
        if_match: Optional[str] = None,
    ) -> DriveFile:
        file_id = urllib.parse.quote(existing.file_id, safe="")
        url = (
            GOOGLE_DRIVE_UPLOAD_ENDPOINT
            + "/"
            + file_id
            + "?"
            + urllib.parse.urlencode(
                {"fields": "id,name,modifiedTime,size", "uploadType": "media"}
            )
        )
        response = self._request(
            "PATCH",
            url,
            body=self._encode_envelope(envelope),
            content_type="application/json",
            if_match=if_match,
        )
        if response.status != 200:
            self._raise_drive_status(response.status)
        document = _json_object(
            response.body,
            "invalid_drive_response",
            "Google Drive returned invalid file metadata.",
        )
        return self._parse_drive_file(document, existing.name)

    def _request(
        self,
        method: str,
        url: str,
        *,
        body: Optional[bytes] = None,
        content_type: Optional[str] = None,
        if_match: Optional[str] = None,
    ) -> HttpResponse:
        headers = {
            "Accept": "application/json",
            "Authorization": self.session.authorization_header(),
            "User-Agent": "RHMRA-Dashboard/2",
        }
        if content_type is not None:
            headers["Content-Type"] = content_type
        if if_match is not None:
            headers["If-Match"] = if_match
        response = self.transport.request(
            method,
            url,
            headers=headers,
            body=body,
            timeout=self.config.request_timeout_seconds,
            max_response_bytes=self.config.max_response_bytes,
        )
        if response.url != url:
            raise PhoneShareProviderError(
                "redirect_rejected",
                "Google Drive attempted an unexpected redirect.",
            )
        return response

    @staticmethod
    def _encode_envelope(envelope: Mapping[str, Any]) -> bytes:
        body = json.dumps(
            envelope, separators=(",", ":"), ensure_ascii=True
        ).encode("ascii")
        if len(body) > MAX_ENVELOPE_BYTES:
            raise PhoneShareProviderError(
                "invalid_envelope", "The encrypted phone-share envelope is too large."
            )
        return body

    @staticmethod
    def _parse_drive_file(value: Any, expected_name: str) -> DriveFile:
        if not isinstance(value, dict):
            raise PhoneShareProviderError(
                "invalid_drive_response", "Google Drive returned invalid file metadata."
            )
        file_id = value.get("id")
        name = value.get("name")
        modified_time = value.get("modifiedTime")
        raw_size = value.get("size")
        if (
            not isinstance(file_id, str)
            or not DRIVE_FILE_ID_RE.fullmatch(file_id)
            or name != expected_name
            or (modified_time is not None and not isinstance(modified_time, str))
        ):
            raise PhoneShareProviderError(
                "invalid_drive_response", "Google Drive returned invalid file metadata."
            )
        size = None
        if raw_size is not None:
            try:
                size = int(raw_size)
            except (TypeError, ValueError):
                raise PhoneShareProviderError(
                    "invalid_drive_response", "Google Drive returned invalid file metadata."
                ) from None
            if isinstance(raw_size, bool) or not 0 <= size <= MAX_ENVELOPE_BYTES:
                raise PhoneShareProviderError(
                    "invalid_drive_response", "Google Drive returned invalid file metadata."
                )
        return DriveFile(file_id, name, modified_time, size)

    def _raise_drive_status(self, status: int) -> None:
        if status in {401, 403}:
            self.session.clear_credentials()
            raise PhoneShareProviderError(
                "google_authorization_required",
                "Connect a Google account before using View on Phone.",
            )
        if status == 404:
            raise PhoneShareProviderError(
                "drive_file_not_found", "The encrypted snapshot does not exist."
            )
        if status in {409, 412}:
            raise PhoneShareProviderError(
                "drive_conflict", "Google Drive reported a snapshot conflict."
            )
        if status == 429 or 500 <= status <= 599:
            raise PhoneShareProviderError(
                "drive_unavailable", "Google Drive is temporarily unavailable."
            )
        raise PhoneShareProviderError(
            "drive_request_failed", "Google Drive rejected the snapshot request."
        )

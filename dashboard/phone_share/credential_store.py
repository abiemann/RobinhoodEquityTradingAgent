"""Current-user encrypted persistence for Google phone-share OAuth tokens.

The dashboard never writes OAuth credentials as plaintext.  On Windows this
module uses DPAPI in the current user's security context and stores only the
opaque DPAPI ciphertext below LocalAppData.  Other platforms deliberately
fall back to process memory until an equally strong native store is provided.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import re
import secrets
import stat
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

from .google_drive import DRIVE_APPDATA_SCOPE, OAuthCredentials


_FILE_MAGIC = b"RHMRA-GOOGLE-OAUTH-DPAPI\x00\x01"
_PROVIDER = "google-drive"
_SCHEMA_VERSION = 1
_MAX_PROTECTED_BYTES = 131072
_MAX_PLAINTEXT_BYTES = 32768
_MAX_FUTURE_EXPIRY_SECONDS = 2 * 24 * 60 * 60
_TOKEN_RE = re.compile(r"[^\x00-\x20\x7f]{8,4096}\Z")
_CLIENT_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_ENTROPY = b"RHMRA phone-share Google OAuth credentials v1"


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


class WindowsDPAPIProtector:
    """Protect bytes with Windows DPAPI for the current signed-in user."""

    CRYPTPROTECT_UI_FORBIDDEN = 0x01

    def __init__(self) -> None:
        self.available = False
        self._crypt32 = None
        self._kernel32 = None
        if os.name != "nt":
            return
        try:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            crypt32.CryptProtectData.argtypes = [
                ctypes.POINTER(_DataBlob),
                wintypes.LPCWSTR,
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(_DataBlob),
            ]
            crypt32.CryptProtectData.restype = wintypes.BOOL
            crypt32.CryptUnprotectData.argtypes = [
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.POINTER(_DataBlob),
                ctypes.c_void_p,
                ctypes.c_void_p,
                wintypes.DWORD,
                ctypes.POINTER(_DataBlob),
            ]
            crypt32.CryptUnprotectData.restype = wintypes.BOOL
            kernel32.LocalFree.argtypes = [ctypes.c_void_p]
            kernel32.LocalFree.restype = ctypes.c_void_p
        except (AttributeError, OSError):
            return
        self._crypt32 = crypt32
        self._kernel32 = kernel32
        self.available = True

    @staticmethod
    def _blob(value: bytes):
        if not isinstance(value, bytes) or not value:
            raise ValueError("invalid protected credential data")
        buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
        blob = _DataBlob(
            len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
        )
        return blob, buffer

    def _transform(self, value: bytes, *, protect: bool) -> bytes:
        if not self.available or self._crypt32 is None or self._kernel32 is None:
            raise OSError("secure credential persistence is unavailable")
        source, source_buffer = self._blob(value)
        entropy, entropy_buffer = self._blob(_ENTROPY)
        output = _DataBlob()
        function = (
            self._crypt32.CryptProtectData
            if protect
            else self._crypt32.CryptUnprotectData
        )
        description = "RHMRA Google Drive phone sharing" if protect else None
        description_argument = description if protect else None
        succeeded = function(
            ctypes.byref(source),
            description_argument,
            ctypes.byref(entropy),
            None,
            None,
            self.CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output),
        )
        # Keep input buffers alive until the native call has returned.
        del source_buffer, entropy_buffer
        if not succeeded:
            raise OSError(ctypes.get_last_error(), "DPAPI operation failed")
        try:
            if not output.pbData or output.cbData <= 0:
                raise OSError("DPAPI returned no protected data")
            return ctypes.string_at(output.pbData, output.cbData)
        finally:
            if output.pbData:
                self._kernel32.LocalFree(output.pbData)

    def protect(self, value: bytes) -> bytes:
        return self._transform(value, protect=True)

    def unprotect(self, value: bytes) -> bytes:
        return self._transform(value, protect=False)


def _reject_duplicate_keys(pairs):
    document = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate credential field")
        document[key] = value
    return document


def _reject_nonfinite(value):
    raise ValueError("non-finite credential value")


class SecureOAuthCredentialStore:
    """Persist an exact OAuth credential document using a native protector.

    ``path`` and ``protector`` are injectable so tests never touch a user's
    profile.  A protector must explicitly report ``available``; otherwise this
    store remains memory-only and does not create any file or directory.
    """

    def __init__(
        self,
        client_id: str,
        *,
        path: Optional[os.PathLike] = None,
        protector: Optional[Any] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if (
            not isinstance(client_id, str)
            or not 32 <= len(client_id) <= 512
            or any(ord(character) <= 32 or ord(character) == 127
                   for character in client_id)
        ):
            raise ValueError("invalid OAuth client ID")
        if not callable(clock):
            raise ValueError("invalid credential-store clock")
        self._client_hash = hashlib.sha256(client_id.encode("utf-8")).hexdigest()
        self._clock = clock
        self._protector = protector if protector is not None else WindowsDPAPIProtector()
        self._path = Path(path) if path is not None else self.default_path(client_id)
        self._write_failed = False

    @staticmethod
    def default_path(client_id: str) -> Optional[Path]:
        if os.name != "nt":
            return None
        local_app_data = os.environ.get("LOCALAPPDATA")
        if (
            not isinstance(local_app_data, str)
            or not local_app_data
            or "\x00" in local_app_data
        ):
            return None
        digest = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:16]
        return (
            Path(local_app_data)
            / "RHMRA"
            / "PhoneShare"
            / f"google-drive-{digest}.oauth.dpapi"
        )

    @property
    def available(self) -> bool:
        return (
            self._path is not None
            and bool(getattr(self._protector, "available", False))
            and callable(getattr(self._protector, "protect", None))
            and callable(getattr(self._protector, "unprotect", None))
        )

    @property
    def mode(self) -> str:
        return "secure" if self.available and not self._write_failed else "memory-only"

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(mode={self.mode!r}, path=<redacted>)"

    def _document(self, credentials: OAuthCredentials) -> Dict[str, Any]:
        if not isinstance(credentials, OAuthCredentials):
            raise ValueError("invalid OAuth credentials")
        if (
            not isinstance(credentials.access_token, str)
            or not _TOKEN_RE.fullmatch(credentials.access_token)
            or not isinstance(credentials.refresh_token, str)
            or not _TOKEN_RE.fullmatch(credentials.refresh_token)
            or isinstance(credentials.expires_at, bool)
            or not isinstance(credentials.expires_at, (int, float))
            or not math.isfinite(float(credentials.expires_at))
            or float(credentials.expires_at) <= 0
            or float(credentials.expires_at)
            > float(self._clock()) + _MAX_FUTURE_EXPIRY_SECONDS
            or credentials.token_type != "Bearer"
            or tuple(credentials.scopes) != (DRIVE_APPDATA_SCOPE,)
        ):
            raise ValueError("invalid OAuth credentials")
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider": _PROVIDER,
            "client_id_sha256": self._client_hash,
            "access_token": credentials.access_token,
            "refresh_token": credentials.refresh_token,
            "expires_at": float(credentials.expires_at),
            "scopes": [DRIVE_APPDATA_SCOPE],
            "token_type": "Bearer",
        }

    def _credentials(self, document: Mapping[str, Any]) -> OAuthCredentials:
        expected_keys = {
            "schema_version",
            "provider",
            "client_id_sha256",
            "access_token",
            "refresh_token",
            "expires_at",
            "scopes",
            "token_type",
        }
        if not isinstance(document, dict) or set(document) != expected_keys:
            raise ValueError("invalid credential schema")
        client_hash = document.get("client_id_sha256")
        credentials = OAuthCredentials(
            access_token=document.get("access_token"),
            refresh_token=document.get("refresh_token"),
            expires_at=document.get("expires_at"),
            scopes=tuple(document.get("scopes", ()))
            if isinstance(document.get("scopes"), list)
            else (),
            token_type=document.get("token_type"),
        )
        if (
            document.get("schema_version") != _SCHEMA_VERSION
            or document.get("provider") != _PROVIDER
            or not isinstance(client_hash, str)
            or not _CLIENT_HASH_RE.fullmatch(client_hash)
            or not secrets.compare_digest(client_hash, self._client_hash)
        ):
            raise ValueError("invalid credential binding")
        # Reuse the exact save-time validation for tokens, scope, and expiry.
        self._document(credentials)
        return credentials

    def save(self, credentials: OAuthCredentials) -> bool:
        if not self.available:
            return False
        try:
            document = self._document(credentials)
            plaintext = json.dumps(
                document,
                ensure_ascii=True,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("ascii")
            if not 1 <= len(plaintext) <= _MAX_PLAINTEXT_BYTES:
                raise ValueError("credential document is too large")
            protected = self._protector.protect(plaintext)
            if (
                not isinstance(protected, bytes)
                or not 1 <= len(protected) <= _MAX_PROTECTED_BYTES
            ):
                raise ValueError("protected credential data is invalid")
            self._atomic_write(_FILE_MAGIC + protected)
        except Exception:
            self._write_failed = True
            return False
        self._write_failed = False
        return True

    def load(self) -> Optional[OAuthCredentials]:
        if not self.available or self._path is None:
            return None
        try:
            if self._path.is_symlink():
                return None
            with self._path.open("rb") as stream:
                stored = stream.read(len(_FILE_MAGIC) + _MAX_PROTECTED_BYTES + 1)
        except FileNotFoundError:
            return None
        except OSError:
            return None
        try:
            if (
                not stored.startswith(_FILE_MAGIC)
                or not len(_FILE_MAGIC) < len(stored)
                <= len(_FILE_MAGIC) + _MAX_PROTECTED_BYTES
            ):
                raise ValueError("invalid protected credential file")
            plaintext = self._protector.unprotect(stored[len(_FILE_MAGIC):])
            if (
                not isinstance(plaintext, bytes)
                or not 1 <= len(plaintext) <= _MAX_PLAINTEXT_BYTES
            ):
                raise ValueError("invalid credential document")
            document = json.loads(
                plaintext.decode("ascii"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
            credentials = self._credentials(document)
        except Exception:
            self._discard_invalid_file()
            return None
        return credentials

    def clear(self) -> bool:
        if self._path is None:
            return False
        try:
            if self._path.is_symlink():
                return False
            self._path.unlink()
            return True
        except FileNotFoundError:
            return True
        except OSError:
            return False

    def _discard_invalid_file(self) -> None:
        # The file is already encrypted; deleting the single exact path avoids
        # retaining a permanently unusable or tampered credential blob.
        self.clear()

    def _atomic_write(self, value: bytes) -> None:
        if self._path is None:
            raise OSError("secure credential path is unavailable")
        parent = self._path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if parent.is_symlink() or (self._path.exists() and self._path.is_symlink()):
            raise OSError("unsafe credential path")
        try:
            os.chmod(parent, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        except OSError:
            pass

        temporary = parent / (
            f".{self._path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        )
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = None
        try:
            descriptor = os.open(temporary, flags, 0o600)
            view = memoryview(value)
            written = 0
            while written < len(view):
                count = os.write(descriptor, view[written:])
                if count <= 0:
                    raise OSError("credential write did not complete")
                written += count
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            try:
                os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            os.replace(temporary, self._path)
            try:
                os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


__all__ = ["SecureOAuthCredentialStore", "WindowsDPAPIProtector"]

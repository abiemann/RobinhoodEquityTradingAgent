"""Provider-neutral building blocks for RHMRA's encrypted phone sharing."""

from .google_drive import (
    DRIVE_APPDATA_SCOPE,
    DriveFile,
    GoogleDriveConfig,
    GoogleDriveProvider,
    HttpResponse,
    InMemoryOAuthSession,
    OAuthAuthorizationRequest,
    OAuthCredentials,
    PhoneShareProviderError,
    UrlLibTransport,
    phone_share_filename,
    validate_envelope,
)
from .credential_store import SecureOAuthCredentialStore, WindowsDPAPIProtector

__all__ = [
    "DRIVE_APPDATA_SCOPE",
    "DriveFile",
    "GoogleDriveConfig",
    "GoogleDriveProvider",
    "HttpResponse",
    "InMemoryOAuthSession",
    "OAuthAuthorizationRequest",
    "OAuthCredentials",
    "PhoneShareProviderError",
    "UrlLibTransport",
    "phone_share_filename",
    "validate_envelope",
    "SecureOAuthCredentialStore",
    "WindowsDPAPIProtector",
]

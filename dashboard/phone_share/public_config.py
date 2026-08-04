"""Public, non-secret defaults for the View on Phone clients.

The OAuth client ID is intentionally empty until the project maintainer
registers RHMRA's desktop OAuth client.  Desktop OAuth client IDs and viewer
URLs are public identifiers, not credentials; end-user access and refresh
tokens are protected for the current Windows user with DPAPI. Phone access
tokens remain in browser memory only.
"""

GOOGLE_DESKTOP_CLIENT_ID = ""
GOOGLE_PHONE_VIEWER_URL = "https://abiemann.github.io/RobinhoodEquityTradingDashboardViewer/"

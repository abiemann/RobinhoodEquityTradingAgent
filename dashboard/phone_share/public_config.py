"""Public, non-secret defaults for the View on Phone clients.

Desktop OAuth client IDs and viewer URLs are public identifiers, not
credentials. End-user access and refresh tokens are protected for the current
Windows user with DPAPI. Phone access tokens remain in browser memory only.
"""

GOOGLE_DESKTOP_CLIENT_ID = "13490783057-78kr2v2lluafbeomf9d1f2u24b2mpv1c.apps.googleusercontent.com"
GOOGLE_PHONE_VIEWER_URL = "https://abiemann.github.io/RobinhoodEquityTradingDashboardViewer/"

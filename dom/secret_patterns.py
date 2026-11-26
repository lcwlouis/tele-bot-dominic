"""Credential shapes shared by public-data checks and configuration diagnostics."""

import re

API_KEY = re.compile(
    r"(?<![\w-])(?:sk-(?:or-v1-)?[A-Za-z0-9_-]{16,}|"
    r"tvly-[A-Za-z0-9_-]{16,}|"
    r"(?:gsk|hf)_[A-Za-z0-9_-]{16,}|[a-z]{2,4}_[A-Za-z0-9]{20,})(?![\w-])"
)
BEARER_TOKEN = re.compile(r"\bbearer[ \t]+([A-Za-z0-9._~+/-]+=*)(?![\w._~+/=-])", re.IGNORECASE)
TELEGRAM_BOT_TOKEN = re.compile(r"(?<!\w)[0-9]{6,12}:[A-Za-z0-9_-]{30,}(?![\w-])")
BASIC_AUTH = re.compile(r"\bbasic[ \t]+([A-Za-z0-9+/]{4,}=*)(?![\w+/=])", re.IGNORECASE)
# The fields whose whole value is a credential, whatever it looks like: HTTP headers, and JSON keys of the same name.
CREDENTIAL_FIELDS = frozenset({"authorization", "proxy-authorization", "cookie", "set-cookie"})
# Such a field written in text: `Authorization: …` to the end of the line, or `"cookie": "…"` inside JSON-like text.
CREDENTIAL_FIELD = re.compile(
    r'(?<![\w-])(proxy-authorization|authorization|set-cookie|cookie)("?[ \t]*[:=][ \t]*)("(?:[^"\\]|\\.)*"|[^\r\n]+)',
    re.IGNORECASE,
)
# An environment variable whose name says it holds a credential.
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|PASS|AUTH|CREDENTIAL|COOKIE|SESSION", re.IGNORECASE)

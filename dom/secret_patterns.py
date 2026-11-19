"""Credential shapes shared by public-data checks and configuration diagnostics."""

import re

API_KEY = re.compile(
    r"(?<![\w-])(?:sk-(?:or-v1-)?[A-Za-z0-9_-]{16,}|"
    r"(?:gsk|hf)_[A-Za-z0-9_-]{16,}|[a-z]{2,4}_[A-Za-z0-9]{20,})(?![\w-])"
)
BEARER_TOKEN = re.compile(r"\bbearer[ \t]+([A-Za-z0-9._~+/-]+=*)(?![\w._~+/=-])", re.IGNORECASE)
TELEGRAM_BOT_TOKEN = re.compile(r"(?<!\w)[0-9]{6,12}:[A-Za-z0-9_-]{30,}(?![\w-])")

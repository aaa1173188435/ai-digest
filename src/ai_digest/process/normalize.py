"""URL and text canonicalisation.

Everything downstream depends on this module. Two feeds reporting the same
story give us different URLs; the canonical form is what makes them collapse
to one row. It is cheap to get right and quietly expensive to get wrong, so it
gets its own module and its own tests.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# Query parameters that never change *which* document is being addressed.
TRACKING_PREFIXES = ("utm_", "mc_", "pk_", "hsa_", "vero_")
TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "dclid",
    "msclkid",
    "yclid",
    "igshid",
    "spm",
    "share_token",
    "ref",
    "ref_src",
    "ref_url",
    "source",
    "src",
    "from",
}

_SCHEME_RE = re.compile(r"^https?://", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")
_SLASH_RE = re.compile(r"/{2,}")


def canonicalize_url(url: str) -> str:
    """Return a stable key for a URL, or the input if it is not http(s).

    Not every difference is normalised away: ``/a`` and ``/a/`` are merged, but
    ``http`` vs ``https`` is *not* forced together, because on some hosts that
    is a genuine redirect to different content.
    """
    url = (url or "").strip()
    if not _SCHEME_RE.match(url):
        return url

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    try:
        port = parts.port
    except ValueError:
        port = None
    netloc = host if port in (None, 80, 443) else f"{host}:{port}"

    path = _SLASH_RE.sub("/", parts.path)
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]

    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered in TRACKING_KEYS or lowered.startswith(TRACKING_PREFIXES):
            continue
        kept.append((key, value))
    kept.sort()

    return urlunsplit(
        (parts.scheme.lower(), netloc, path or "/", urlencode(kept), "")
    )


def normalize_text(text: str) -> str:
    """Fold unicode, drop zero-width junk, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u200b", " ").replace("\xa0", " ").replace("\ufeff", "")
    return _WS_RE.sub(" ", text).strip()


def content_hash(text: str) -> str:
    """Exact-duplicate key: same normalised text -> same hash."""
    return hashlib.sha256(normalize_text(text).lower().encode("utf-8")).hexdigest()

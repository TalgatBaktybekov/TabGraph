"""URL canonicalization: one page, one URL.

Tracking parameters identify campaigns and clicks, not content, so two visits
to the same article via different links must dedupe to one capture and one
Page node.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

TRACKING_PREFIXES = ("utm_", "gad_")
TRACKING_PARAMS = {
    "gclid",
    "gad",
    "gbraid",
    "wbraid",
    "fbclid",
    "msclkid",
    "yclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "_hsenc",
    "_hsmi",
    "ref",
    "ref_src",
}


def _is_tracking(param: str) -> bool:
    lowered = param.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def canonicalize(url: str) -> str:
    """Lowercase scheme/host, drop default ports, fragments, and tracking params.

    Non-web or unparseable URLs are returned unchanged.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        return url

    host = (parts.hostname or "").lower()
    netloc = host
    try:
        port = parts.port
    except ValueError:
        port = None
    if port and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"

    query = urlencode(
        [
            (k, v)
            for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not _is_tracking(k)
        ]
    )
    return urlunsplit((scheme, netloc, parts.path or "/", query, ""))

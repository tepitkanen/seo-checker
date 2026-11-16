"""URL helpers: matching hosts and pages across URL variants, and printing URLs safely."""

import re
from urllib.parse import urlparse

# Links to these file types are not HTML pages, so they are not checked.
NON_PAGE_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip",
                       ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".mp3", ".mp4")
DEFAULT_PORTS = {"http": 80, "https": 443}
# Control characters (incl. terminal escape sequences) and bidi overrides that can disguise text.
UNPRINTABLE_RE = re.compile("[\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def site_host(url):
    """Return the host without a leading 'www.', so example.fi and www.example.fi match.

    User info and the scheme's default port are dropped (https://example.fi:443 is example.fi);
    other ports are kept, as they can be a different site. Raises ValueError on a malformed URL.
    """
    parts = urlparse(url)
    host = parts.hostname or ""  # lowercased, without user info, port or IPv6 brackets
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and port != DEFAULT_PORTS.get(parts.scheme):
        host += f":{port}"
    return host.removeprefix("www.")


def printable(text):
    """Escape control characters so text from a crawled site can't drive the terminal."""
    return UNPRINTABLE_RE.sub(lambda m: f"\\x{ord(m.group()):02x}" if ord(m.group()) < 0x100
                              else f"\\u{ord(m.group()):04x}", str(text))


def link_key(url):
    """Key for matching the same page across http/https, www/non-www, a trailing slash
    and a directory's index file (/a/index.html is the same page as /a/).

    link_key("http://www.example.fi/a/index.html?x=1") == link_key("https://example.fi/a?x=1")
    """
    parts = urlparse(url)
    path = parts.path
    for index in ("/index.html", "/index.htm"):
        if path.lower().endswith(index):
            path = path[:-len(index)]
    key = site_host(url) + path.rstrip("/")
    return f"{key}?{parts.query}" if parts.query else key

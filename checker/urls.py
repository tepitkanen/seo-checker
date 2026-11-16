"""URL helpers: matching hosts and pages across URL variants."""

from urllib.parse import urlparse

# Links to these file types are not HTML pages, so they are not checked.
NON_PAGE_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip",
                       ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".mp3", ".mp4")


def site_host(url):
    """Return the host without a leading 'www.', so example.fi and www.example.fi match."""
    return urlparse(url).netloc.lower().removeprefix("www.")


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

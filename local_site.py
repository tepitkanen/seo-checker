"""Local test site for seo_checker.py: each page or sitemap has a deliberate problem.

Run:   python local_site.py [port] [--robots-status 403]   (default port 8765)
Crawl: python seo_checker.py http://127.0.0.1:8765 --delay 0 --check-links --csv reports/local.csv

Expected results are listed next to each page and sitemap below. Pages named /must-not-appear-*
are only listed in sitemaps the checker must reject; if one shows up in the report, its name says
which check failed. The next port (8766 by default) serves the "other host" for off-site redirects.

--robots-status 401/403 makes robots.txt return that status: the checker should warn, report no
AI crawlers blocked, fall back to the homepage links (its Sitemap: lines are ignored too) and,
with --check-links, request the /private/secret link instead of skipping it.
Not named test_*.py so pytest does not collect it.
"""
import argparse
import gzip
import html
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast

HOST = "127.0.0.1"  # not "localhost": on Windows that tries IPv6 first and adds ~2 s per request
SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
SHARED_TITLE = "Two local test pages share this title"
SHARED_DESC = "Two pages of the local test site share this meta description on purpose, to test duplicates."


def page(base, path, title=None, h1="Heading", body="", canonical=True, schema=True, desc=None):
    """Build a page with a unique, well sized title and description unless given; desc="" leaves it out."""
    if title is None:
        title = f"Local test site page {path} with a good title"  # 44-59 chars here
    if desc is None:
        desc = f"Meta description of the {path} page on the local test site, long enough to pass the checker."
    head = f"<title>{html.escape(title)}</title>"
    if desc:
        head += f'<meta name="description" content="{html.escape(desc)}">'
    if canonical:
        head += f'<link rel="canonical" href="{base}{path}">'
    if schema:
        head += '<script type="application/ld+json">{"@context":"https://schema.org","@type":"WebPage"}</script>'
    return f"<!doctype html><html><head>{head}</head><body><h1>{h1}</h1>{body}</body></html>"


def urlset(base, paths):
    urls = "".join(f"<url><loc>{base}{p}</loc></url>" for p in paths)
    return f'<?xml version="1.0"?><urlset xmlns="{SITEMAP_NS}">{urls}</urlset>'


def sitemap_index(base, paths):
    locs = "".join(f"<sitemap><loc>{base}{p}</loc></sitemap>" for p in paths)
    return f'<?xml version="1.0"?><sitemapindex xmlns="{SITEMAP_NS}">{locs}</sitemapindex>'


def gz(text):
    return gzip.compress(text.encode("utf-8"))


def corrupt(data):
    """Valid gzip header, garbage compressed body (raised zlib.error before the fix)."""
    data = bytearray(data)
    for i in range(12, len(data) - 8):
        data[i] ^= 0xFF
    return bytes(data)


def gzip_bomb(text, unpacked_mb):
    """Gzip text padded with whitespace to unpacked_mb MB, compressed in 1 MB chunks (~1 KB per MB)."""
    head, tail = text.encode("utf-8").split(b"<url>", 1)
    packer = zlib.compressobj(9, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    parts = [packer.compress(head)]
    chunk = b" " * (1024 * 1024)
    parts += [packer.compress(chunk) for _ in range(unpacked_mb)]
    parts += [packer.compress(b"<url>" + tail), packer.flush()]
    return b"".join(parts)


def build_site(port):
    """Return (routes for the main site, routes for the other host); each maps path -> response."""
    base, other = f"http://{HOST}:{port}", f"http://{HOST}:{port + 1}"
    home_links = "".join(f'<a href="{href}">{href}</a> ' for href in [
        "/about",           # fine
        "/missing",         # 404 -> broken
        "/old-about",       # redirect to /about -> fine
        "/server-error",    # 500 -> broken
        "/private/secret",  # robots.txt disallows -> skipped
        "/about/",          # trailing slash of a crawled page -> not requested again
        "https://example.com/",  # external -> ignored
    ])
    pages = {
        "/": page(base, "/", body=home_links),
        "/about": page(base, "/about", body='<a href="/">Home</a> <a href="/gone">Gone</a>'),
        "/noindex-header": page(base, "/noindex-header"),
        "/none-header": page(base, "/none-header"),
        # The only pages sharing a title and description -> "Duplicate title" and
        # "Duplicate meta description" on these two. (/old-about redirects to /about, so it
        # shares /about's title but is the same page, not a duplicate.)
        "/slow": page(base, "/slow", title=SHARED_TITLE, desc=SHARED_DESC),
        "/large": page(base, "/large", title=SHARED_TITLE, desc=SHARED_DESC,
                       body="<p>" + "x" * 600 * 1024 + "</p>"),
        "/sloppy": page(base, "/sloppy", title="Short", h1="One</h1><h1>Two",
                        body='<img src="a.png"><img src="b.png" alt="">',
                        canonical=False, schema=False, desc=""),
        "/meta-noindex": page(base, "/meta-noindex").replace(
            "<head>", '<head><meta name="robots" content="noindex">'),
        # Unique title/description; the CSV must show them with a leading ' (not as formulas).
        "/formula": page(base, "/formula", title='=HYPERLINK("https://example.com","click me")',
                         desc="@SUM(1+1) a description that starts like an Excel formula, long enough."),
        "/private/secret": page(base, "/private/secret", title="Private page that robots.txt disallows"),
    }
    main_paths = [p for p in pages if p != "/private/secret"] + ["/old-about", "/server-error"]
    # Listed only in the good child sitemap -> must be crawled.
    pages["/from-child-sitemap"] = page(
        base, "/from-child-sitemap", title="Page listed only in a gzipped child sitemap",
        desc="Found through the sitemap index and its gzipped child sitemap, so it must be in the report.")
    # Listed only in a sitemap inside a nested sitemap index -> must be crawled; the nested
    # index's own entry (/grandchild.xml) is a sitemap and must not be crawled as a page.
    pages["/from-nested-index"] = page(
        base, "/from-nested-index", title="Page listed only below a nested sitemap index",
        desc="Found through a sitemap index inside the sitemap index, so it must be in the report.")
    xml, gzipped = "application/xml", "application/gzip"
    good_child = gz(urlset(base, ["/from-child-sitemap"]))

    routes: dict = {path: (200, body, "text/html; charset=utf-8") for path, body in pages.items()}
    routes.update({
        "/llms.txt": (200, "# Test site\n", "text/plain"),
        "/server-error": (500, "<h1>Server error</h1>", "text/html; charset=utf-8"),
        "/old-about": (301, "/about", None),
        # Sitemaps from robots.txt
        "/sitemap.xml.gz": (200, gz(urlset(base, main_paths)), gzipped),  # read
        "/sitemap-index.xml": (200, sitemap_index(base, [
            "/child-ok.xml.gz",         # read -> /from-child-sitemap
            "/child-corrupt.xml.gz",    # skipped, no crash
            "/child-truncated.xml.gz",  # skipped
            "/child-huge.xml.gz",       # skipped (over the 50 MB limit once unpacked)
            "/child-moved.xml",         # skipped (redirects to another host)
            "/child-doctype.xml",       # skipped (DOCTYPE/ENTITY, as in "billion laughs")
            "/child-index.xml",         # an index inside the index -> /grandchild.xml -> /from-nested-index
        ]), xml),
        "/moved-sitemap.xml": (301, f"{other}/sitemap-a.xml", None),  # skipped (another host)
        # Children of the sitemap index
        "/child-ok.xml.gz": (200, good_child, gzipped),
        "/child-corrupt.xml.gz": (200, corrupt(gz(urlset(base, ["/must-not-appear-corrupt"]))), gzipped),
        "/child-truncated.xml.gz": (200, gz(urlset(base, ["/must-not-appear-truncated"]))[:-20], gzipped),
        "/child-huge.xml.gz": (200, gzip_bomb(urlset(base, ["/must-not-appear-huge"]), 60), gzipped),
        "/child-moved.xml": (301, f"{other}/sitemap-b.xml", None),
        "/child-doctype.xml": (200, urlset(base, ["/must-not-appear-doctype"]).replace(
            "<urlset", '<!DOCTYPE urlset [<!ENTITY x "y">]><urlset', 1), xml),
        "/child-index.xml": (200, sitemap_index(base, ["/grandchild.xml"]), xml),
        "/grandchild.xml": (200, urlset(base, ["/from-nested-index"]), xml),
    })
    # Pages listed on the other host use the main site's URLs, so only the redirect check stops them.
    other_routes = {
        "/sitemap-a.xml": (200, urlset(base, ["/must-not-appear-moved-sitemap"]), xml),
        "/sitemap-b.xml": (200, urlset(base, ["/must-not-appear-moved-child"]), xml),
    }
    return routes, other_routes


def robots_txt(port):
    base = f"http://{HOST}:{port}"
    return "\n".join([
        "User-agent: *",
        "Disallow: /private",
        "",
        "User-agent: GPTBot",  # -> "AI crawlers blocked by robots.txt: GPTBot, ClaudeBot"
        "User-agent: ClaudeBot",
        "Disallow: /",
        "",
        f"Sitemap: {base}/sitemap.xml.gz",
        f"Sitemap: {base}/sitemap-index.xml",
        f"Sitemap: {base}/moved-sitemap.xml",
        "Sitemap: https://example.com/sitemap.xml",  # other host -> ignored, never requested
        "",
    ])


HEADERS = {"/noindex-header": {"X-Robots-Tag": "noindex"},
           "/none-header": {"X-Robots-Tag": "googlebot: none"}}


class SiteServer(ThreadingHTTPServer):
    # HTTPServer turns this on; on Windows it lets a second server bind a port already in use
    # without an error. Off, starting twice on one port fails with "address already in use".
    allow_reuse_address = False

    def __init__(self, port, routes):
        super().__init__((HOST, port), Handler)
        self.port = port
        self.routes = routes  # path -> (status, body or redirect target, content type)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/slow":
            time.sleep(1.5)
        status, body, content_type = cast(SiteServer, self.server).routes.get(
            path, (404, "<h1>Not found</h1>", "text/html; charset=utf-8"))
        self.send_response(status)
        if 300 <= status < 400:
            self.send_header("Location", body)
            body = b""
        else:
            self.send_header("Content-Type", content_type)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_header("Content-Length", str(len(data)))
        for name, value in HEADERS.get(path, {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):  # noqa: A002 (name must match the base class)
        print(f"  server :{cast(SiteServer, self.server).port} {format % args}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Local test site for seo_checker.py")
    parser.add_argument("port", nargs="?", type=int, default=8765)
    parser.add_argument("--robots-status", type=int, default=200,
                        help="HTTP status for robots.txt, e.g. 403 (default 200)")
    args = parser.parse_args()
    routes, other_routes = build_site(args.port)
    routes["/robots.txt"] = (args.robots_status, robots_txt(args.port), "text/plain")
    main_server, other_server = SiteServer(args.port, routes), SiteServer(args.port + 1, other_routes)
    threading.Thread(target=other_server.serve_forever, daemon=True).start()
    print(f"Test site on http://{HOST}:{args.port} (robots.txt status {args.robots_status}), "
          f"other host on port {args.port + 1}", flush=True)
    try:
        main_server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""Network: fetching pages politely, robots.txt, sitemaps and link checks."""

import re
import time
import xml.etree.ElementTree as ET
import zlib
from urllib.robotparser import RobotFileParser

import requests
from bs4 import UnicodeDammit

from checker.pages import internal_links
from checker.urls import link_key, printable, site_host

USER_AGENT = "Mozilla/5.0 (compatible; seo-checker/1.0)"
TIMEOUT = 15
SITEMAP_PATHS = ("/sitemap_index.xml", "/sitemap.xml")
MAX_SITEMAP_BYTES = 50 * 1024 * 1024  # sitemaps.org limit for an uncompressed sitemap
# How many sitemap indexes deep to follow: 1 is what the spec allows, a few more covers plugins
# that nest them, and the limit stops an endless chain of index -> index -> ...
MAX_SITEMAP_DEPTH = 3
# Any download stops here, so a huge or endless response can't exhaust memory.
MAX_RESPONSE_BYTES = 50 * 1024 * 1024
DTD_RE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)
# robots.txt user agents of AI answer engines and training crawlers (GEO).
# RobotFileParser keeps only the part before "/", so the full USER_AGENT would be matched as "mozilla".
ROBOTS_AGENT = "seo-checker"
AI_CRAWLERS = ("GPTBot", "OAI-SearchBot", "ClaudeBot", "PerplexityBot", "Google-Extended")


def parse_sitemap(xml_text):
    """Return (urls, is_index) from sitemap XML; is_index is True for a sitemap index.

    Raises ET.ParseError on invalid XML, and on any DOCTYPE or ENTITY: real sitemaps never
    need them, and refusing them blocks entity-expansion attacks such as "billion laughs".
    """
    if DTD_RE.search(xml_text):
        raise ET.ParseError("DOCTYPE/ENTITY declarations are not allowed in a sitemap")
    root = ET.fromstring(xml_text)
    urls = []
    # Only the direct <loc> of each <url>/<sitemap>; skips nested ones like <image:loc>.
    for entry in root:
        for child in entry:
            if child.tag.split("}")[-1] == "loc" and child.text:
                urls.append(child.text.strip())
    return urls, root.tag.endswith("sitemapindex")


def decode_html(resp):
    """Return the response body as text, using the page's <meta charset> if the server omits it.

    Without a charset in the Content-Type header, requests assumes ISO-8859-1, which garbles
    UTF-8 pages (e.g. "KengÃ¤t" instead of "Kengät"). UnicodeDammit reads the meta tag instead;
    without one it tries UTF-8 and then windows-1252 before guessing, as guesses fail on short pages.
    """
    if "charset" in resp.headers.get("Content-Type", "").lower():
        return resp.text
    dammit = UnicodeDammit(resp.content, is_html=True, user_encodings=["utf-8", "windows-1252"])
    return dammit.unicode_markup or ""


def sitemap_text(resp):
    """Return a sitemap response as text, unpacking gzipped files such as sitemap.xml.gz.

    requests only unpacks gzip sent as Content-Encoding; a .gz file arrives as raw bytes.
    """
    if resp.content[:2] == b"\x1f\x8b":  # gzip magic number
        try:
            return _gunzip_capped(resp.content, MAX_SITEMAP_BYTES).decode("utf-8", errors="replace")
        except (EOFError, zlib.error, ValueError):
            # broken or oversized archive: parse_sitemap raises ParseError and it is skipped
            return ""
    return resp.text


def _gunzip_capped(data, limit):
    """Unpack gzip data like gzip.decompress, but raise ValueError past limit bytes (gzip bombs)."""
    out = bytearray()
    while data:  # a .gz file may hold several members back to back
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        out += d.decompress(data, limit + 1 - len(out))
        if len(out) > limit:
            raise ValueError("gzip data exceeds size limit")
        if not d.eof:
            raise EOFError("truncated gzip data")
        data = d.unused_data.lstrip(b"\x00")  # gzip.decompress also ignores zero padding
    return bytes(out)


class Crawler:
    def __init__(self, base_url, delay=1.0):
        self.base = base_url.rstrip("/")
        self.delay = delay
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.robots = RobotFileParser()
        resp = self.get(self.base + "/robots.txt")
        if resp is not None and resp.status_code in (401, 403):
            # Like Google, treat it as no robots.txt; Python's RobotFileParser.read() would block all.
            print(f"  warning: robots.txt returned {resp.status_code}; treating every page as allowed")
        elif resp is not None and resp.status_code >= 500:
            # Google stops crawling a site whose robots.txt fails; a checker would then report
            # nothing, so it carries on, but the error itself is worth knowing about.
            print(f"  warning: robots.txt returned {resp.status_code} (server error; Google would pause "
                  "crawling this site); treating every page as allowed")
        self.robots.parse(resp.text.splitlines() if resp is not None and resp.ok else [])

    def get(self, url):
        """GET a URL politely; return the response, or None on network error or over MAX_RESPONSE_BYTES."""
        time.sleep(self.delay)
        resp = None
        try:
            resp = self.session.get(url, timeout=TIMEOUT, stream=True)
            body = bytearray()
            # iter_content unpacks Content-Encoding: gzip, so the cap also stops compression bombs.
            for chunk in resp.iter_content(64 * 1024):
                body += chunk
                if len(body) > MAX_RESPONSE_BYTES:
                    resp.close()
                    print(f"  skipped (over {MAX_RESPONSE_BYTES // (1024 * 1024)} MB): {printable(url)}")
                    return None
            resp._content = bytes(body)  # what resp.content and resp.text read
            return resp
        except requests.RequestException:
            if resp is not None:
                resp.close()  # a failed download must not keep its connection checked out
            return None

    def allowed(self, url):
        return self.robots.can_fetch(ROBOTS_AGENT, url)

    def robots_sitemaps(self):
        """Return same-site sitemap URLs listed as 'Sitemap:' lines in robots.txt."""
        listed = self.robots.site_maps() or []
        return [u for u in dict.fromkeys(listed) if self._same_site(u)]

    def discover_urls(self, max_pages):
        """Find page URLs from the sitemap, falling back to homepage links.

        Sitemaps listed in robots.txt are used first (all of them, since sites often list
        several); without any, the usual /sitemap_index.xml and /sitemap.xml are tried.
        """
        urls, seen = [], set()  # seen: sitemaps already read, so none is fetched twice
        for sitemap_url in self.robots_sitemaps():
            if len(urls) >= max_pages:
                break
            # dict.fromkeys drops URLs listed more than once, keeping the first position.
            urls = list(dict.fromkeys(urls + self._read_sitemap(sitemap_url, max_pages - len(urls), seen=seen)))
        if urls:
            return urls[:max_pages]
        for path in SITEMAP_PATHS:
            urls = list(dict.fromkeys(self._read_sitemap(self.base + path, max_pages, seen=seen)))
            if urls:
                return urls[:max_pages]
        resp = self.get(self.base + "/")
        links = internal_links(resp.text, self.base + "/") if resp is not None and resp.ok else []
        # Skip other addresses of a page already listed, e.g. /index.html for the homepage.
        urls, seen = [], set()
        for u in [self.base + "/"] + links:
            if link_key(u) not in seen:
                seen.add(link_key(u))
                urls.append(u)
        return urls[:max_pages]

    def _same_site(self, url):
        try:
            return site_host(url) == site_host(self.base)
        except ValueError:  # malformed URL in a sitemap, e.g. "http://[x"
            return False

    def _get_sitemap(self, url):
        """GET a sitemap; return None on failure or if a redirect left the site being checked."""
        resp = self.get(url)
        if resp is None or not resp.ok or (resp.url and not self._same_site(resp.url)):
            return None
        return resp

    def _read_sitemap(self, sitemap_url, max_pages, depth=0, seen=None):
        """Fetch one sitemap (or sitemap index) and return its page URLs; [] if it fails.

        A sitemap index's children are read in turn; an index inside an index is followed too
        (the spec forbids it, but some plugins do it), up to MAX_SITEMAP_DEPTH levels. Its URLs
        are sitemaps, never pages. seen holds sitemaps already read, so a loop can't repeat.
        """
        seen = set() if seen is None else seen
        if sitemap_url in seen:
            return []
        seen.add(sitemap_url)
        resp = self._get_sitemap(sitemap_url)
        if resp is None:
            return []
        try:
            urls, is_index = parse_sitemap(sitemap_text(resp))
        except ET.ParseError:  # skip a broken sitemap instead of losing the others
            return []
        # Only follow URLs on the site being checked, never other hosts listed in a sitemap.
        urls = [u for u in urls if self._same_site(u)]
        if not is_index:
            return urls
        if depth >= MAX_SITEMAP_DEPTH:
            return []
        pages = []
        for child in urls:
            if len(pages) >= max_pages:
                break
            # dict.fromkeys drops pages that overlapping child sitemaps both list, so --max
            # counts unique pages.
            pages = list(dict.fromkeys(pages + self._read_sitemap(child, max_pages - len(pages), depth + 1, seen)))
        return pages

    def fetch_pages(self, max_pages):
        """Yield (url, status, html, final_url, info) for each discovered page allowed by robots.txt.

        final_url is where redirects ended up (same as url if there were none).
        info is a dict of response details for check_page (empty on network error).
        """
        for url in self.discover_urls(max_pages):
            if not self.allowed(url):
                print(f"  skipped (robots.txt): {printable(url)}")
                continue
            resp = self.get(url)
            if resp is None:
                yield url, 0, "", url, {}
            else:
                info = {
                    "x_robots_tag": resp.headers.get("X-Robots-Tag", ""),
                    "response_ms": round(resp.elapsed.total_seconds() * 1000),
                    "size_kb": round(len(resp.content) / 1024),
                }
                yield url, resp.status_code, decode_html(resp), resp.url or url, info

    def link_status(self, url):
        """Return the HTTP status of a link (after redirects), or 0 on network error."""
        resp = self.get(url)
        return 0 if resp is None else resp.status_code

    def blocked_ai_crawlers(self):
        """Return the AI crawlers that robots.txt does not allow to fetch the homepage."""
        return [bot for bot in AI_CRAWLERS if not self.robots.can_fetch(bot, self.base + "/")]

    def has_llms_txt(self):
        resp = self.get(self.base + "/llms.txt")
        return resp is not None and resp.ok and "text/html" not in resp.headers.get("Content-Type", "").lower()


def find_broken_links(page_links, known_status, crawler, max_links):
    """Check the internal links found on each page and return the broken ones.

    page_links maps page URL -> list of its internal links. known_status maps URLs that were
    already crawled to their status, so those are not requested again. At most max_links new
    URLs are requested; links blocked by robots.txt are not checked.
    Returns (broken, checked, capped): broken maps page URL -> list of broken links,
    checked is the number of new requests made, capped is True if max_links stopped checking.
    """
    statuses = {link_key(u): s for u, s in known_status.items()}
    broken, checked, capped = {}, 0, False
    for page, links in page_links.items():
        listed = set()  # /a and /a/ are one link, so a page lists each broken target once
        for link in links:
            key = link_key(link)
            if key in listed:
                continue
            listed.add(key)
            if key not in statuses:
                if not crawler.allowed(link):
                    continue
                if checked >= max_links:
                    capped = True
                    continue
                statuses[key] = crawler.link_status(link)
                checked += 1
            status = statuses[key]
            if status == 0 or status >= 400:
                broken.setdefault(page, []).append(link)
    return broken, checked, capped

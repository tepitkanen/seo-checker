"""SEO checker: crawls a site via its sitemap and reports common on-page SEO/GEO issues."""

import argparse
import csv
import json
import os
import re
import time
import xml.etree.ElementTree as ET
import zlib
from collections import Counter
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup, UnicodeDammit
from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

USER_AGENT = "Mozilla/5.0 (compatible; seo-checker/1.0)"
TIMEOUT = 15
TITLE_MIN, TITLE_MAX = 30, 60
DESC_MIN, DESC_MAX = 70, 160
SLOW_RESPONSE_MS = 1000  # time until the server's response arrived
LARGE_PAGE_KB = 500      # size of the HTML itself, without images, CSS or scripts
SITEMAP_PATHS = ("/sitemap_index.xml", "/sitemap.xml")
MAX_SITEMAP_BYTES = 50 * 1024 * 1024  # sitemaps.org limit for an uncompressed sitemap
# robots.txt user agents of AI answer engines and training crawlers (GEO).
AI_CRAWLERS = ("GPTBot", "OAI-SearchBot", "ClaudeBot", "PerplexityBot", "Google-Extended")
# Links to these file types are not HTML pages, so they are not checked.
NON_PAGE_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip",
                       ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".mp3", ".mp4")


# ---------- Page checks (pure functions, no network) ----------

def check_page(html, url, status=200, final_url=None, info=None):
    """Analyse one page's HTML and return a dict of findings and a list of issues.

    final_url is the address after redirects; the canonical is compared against it.
    info holds response details from Crawler.fetch_pages: x_robots_tag, response_ms, size_kb.
    """
    info = info or {}
    result = {
        "url": url, "status": status, "title": "", "title_length": 0,
        "description": "", "description_length": 0, "h1_count": 0, "images_missing_alt": 0,
        "noindex": False, "canonical": "", "redirects_to": "", "schema_types": "",
        "response_ms": info.get("response_ms", 0), "size_kb": info.get("size_kb", 0),
        "broken_links": "", "issues": [],
    }
    issues = result["issues"]
    # A sitemap should list final addresses; a redirect wastes crawl budget and slows users.
    # Trailing slashes are ignored because requests adds one to bare domains ("example.fi/").
    if final_url and final_url.rstrip("/") != url.rstrip("/"):
        result["redirects_to"] = final_url
        issues.append("Redirects to another URL")
    if status != 200:
        issues.append("Request failed" if status == 0 else f"HTTP {status}")
        return result

    if result["response_ms"] > SLOW_RESPONSE_MS:
        issues.append("Slow response")
    if result["size_kb"] > LARGE_PAGE_KB:
        issues.append("Large page")

    soup = BeautifulSoup(html, "html.parser")

    title = soup.title.get_text(strip=True) if soup.title else ""
    result["title"], result["title_length"] = title, len(title)
    if not title:
        issues.append("Missing title")
    elif len(title) < TITLE_MIN:
        issues.append("Title too short")
    elif len(title) > TITLE_MAX:
        issues.append("Title too long")

    desc = get_attr(find_meta(soup, "description"), "content")
    result["description"], result["description_length"] = desc, len(desc)
    if not desc:
        issues.append("Missing meta description")
    elif len(desc) < DESC_MIN:
        issues.append("Meta description too short")
    elif len(desc) > DESC_MAX:
        issues.append("Meta description too long")

    h1_count = len(soup.find_all("h1"))
    result["h1_count"] = h1_count
    if h1_count == 0:
        issues.append("Missing H1")
    elif h1_count > 1:
        issues.append("Multiple H1")

    missing_alt = sum(1 for img in soup.select("img") if not get_attr(img, "alt"))
    result["images_missing_alt"] = missing_alt
    if missing_alt:
        issues.append("Images missing alt")

    if "noindex" in get_attr(find_meta(soup, "robots"), "content").lower():
        result["noindex"] = True
        issues.append("Noindex")
    # The same rule can be sent as an HTTP header, e.g. "X-Robots-Tag: noindex" or "googlebot: noindex".
    # "none" means "noindex, nofollow".
    if re.search(r"\b(noindex|none)\b", info.get("x_robots_tag", "").lower()):
        result["noindex"] = True
        issues.append("Noindex (X-Robots-Tag)")

    canonical_href = get_attr(soup.select_one('link[rel~="canonical"]'), "href")
    if canonical_href:
        page_url = final_url or url  # if /a redirects to /b, /b's canonical should point to /b
        result["canonical"] = urljoin(page_url, canonical_href)
        if result["canonical"].rstrip("/") != page_url.rstrip("/"):
            issues.append("Canonical points elsewhere")
    else:
        issues.append("Missing canonical")

    # GEO: structured data helps search engines and AI answer engines understand the page.
    types = find_schema_types(soup)
    result["schema_types"] = ", ".join(sorted(types))
    if not types:
        issues.append("No structured data")

    return result


def get_attr(tag, name):
    """Return a tag attribute as a stripped string ('' if the tag or attribute is missing)."""
    value = tag.get(name) if tag else None
    if isinstance(value, list):  # multi-valued attributes such as rel or class
        value = " ".join(value)
    return (value or "").strip()


def find_meta(soup, name):
    """Return the first <meta name="..."> tag matching name (case-insensitive), or None."""
    for tag in soup.select("meta[name]"):
        if get_attr(tag, "name").lower() == name:
            return tag
    return None


def find_schema_types(soup):
    """Return the set of schema.org @type values found in JSON-LD blocks."""
    types = set()
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                t = item.get("@type")
                if isinstance(t, str):
                    types.add(t)
                elif isinstance(t, list):
                    types.update(x for x in t if isinstance(x, str))
                stack.append(item.get("@graph"))  # may be a list, a dict or missing
    return types


def parse_sitemap(xml_text):
    """Return (urls, is_index) from sitemap XML; is_index is True for a sitemap index."""
    root = ET.fromstring(xml_text)
    urls = []
    # Only the direct <loc> of each <url>/<sitemap>; skips nested ones like <image:loc>.
    for entry in root:
        for child in entry:
            if child.tag.split("}")[-1] == "loc" and child.text:
                urls.append(child.text.strip())
    return urls, root.tag.endswith("sitemapindex")


def internal_links(html, base_url):
    """Return unique same-host links found in the HTML, without fragments."""
    host = site_host(base_url)
    links = []
    for a in BeautifulSoup(html, "html.parser").select("a[href]"):
        link = urldefrag(urljoin(base_url, get_attr(a, "href")))[0]
        if site_host(link) != host or urlparse(link).path.lower().endswith(NON_PAGE_EXTENSIONS):
            continue
        if link not in links:
            links.append(link)
    return links


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


# ---------- Network ----------

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
        self.robots.parse(resp.text.splitlines() if resp is not None and resp.ok else [])

    def get(self, url):
        """GET a URL politely; return the response or None on network error."""
        time.sleep(self.delay)
        try:
            return self.session.get(url, timeout=TIMEOUT)
        except requests.RequestException:
            return None

    def allowed(self, url):
        return self.robots.can_fetch(USER_AGENT, url)

    def robots_sitemaps(self):
        """Return same-site sitemap URLs listed as 'Sitemap:' lines in robots.txt."""
        listed = self.robots.site_maps() or []
        return [u for u in dict.fromkeys(listed) if self._same_site(u)]

    def discover_urls(self, max_pages):
        """Find page URLs from the sitemap, falling back to homepage links.

        Sitemaps listed in robots.txt are used first (all of them, since sites often list
        several); without any, the usual /sitemap_index.xml and /sitemap.xml are tried.
        """
        urls = []
        for sitemap_url in self.robots_sitemaps():
            if len(urls) >= max_pages:
                break
            # dict.fromkeys drops URLs listed more than once, keeping the first position.
            urls = list(dict.fromkeys(urls + self._read_sitemap(sitemap_url, max_pages - len(urls))))
        if urls:
            return urls[:max_pages]
        for path in SITEMAP_PATHS:
            urls = list(dict.fromkeys(self._read_sitemap(self.base + path, max_pages)))
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
        return site_host(url) == site_host(self.base)

    def _get_sitemap(self, url):
        """GET a sitemap; return None on failure or if a redirect left the site being checked."""
        resp = self.get(url)
        if resp is None or not resp.ok or (resp.url and not self._same_site(resp.url)):
            return None
        return resp

    def _read_sitemap(self, sitemap_url, max_pages):
        """Fetch one sitemap (or sitemap index) and return its page URLs; [] if it fails."""
        resp = self._get_sitemap(sitemap_url)
        if resp is None:
            return []
        try:
            return self._expand_sitemap(sitemap_text(resp), max_pages)
        except ET.ParseError:
            return []

    def _expand_sitemap(self, xml_text, max_pages):
        urls, is_index = parse_sitemap(xml_text)
        # Only follow URLs on the site being checked, never other hosts listed in a sitemap.
        urls = [u for u in urls if self._same_site(u)]
        if not is_index:
            return urls
        pages = []
        for child in urls:
            if len(pages) >= max_pages:
                break
            resp = self._get_sitemap(child)
            if resp is None:
                continue
            try:
                pages += [u for u in parse_sitemap(sitemap_text(resp))[0] if self._same_site(u)]
            except ET.ParseError:  # skip one broken sub-sitemap instead of losing all pages
                continue
        return pages

    def fetch_pages(self, max_pages):
        """Yield (url, status, html, final_url, info) for each discovered page allowed by robots.txt.

        final_url is where redirects ended up (same as url if there were none).
        info is a dict of response details for check_page (empty on network error).
        """
        for url in self.discover_urls(max_pages):
            if not self.allowed(url):
                print(f"  skipped (robots.txt): {url}")
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
        return resp is not None and resp.ok and "text/html" not in resp.headers.get("Content-Type", "")


# ---------- Reporting ----------

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
        for link in links:
            key = link_key(link)
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


def mark_duplicates(results):
    """Add "Duplicate title" / "Duplicate meta description" to pages that share one.

    Comparison ignores case and surrounding whitespace. Two sitemap URLs that redirect to the
    same page are one page, not duplicates. Empty values are skipped (already "Missing ...").
    """
    for field, issue in (("title", "Duplicate title"), ("description", "Duplicate meta description")):
        pages = {}  # normalized value -> {page key: [results]}
        for r in results:
            value = " ".join(r.get(field, "").split()).casefold()
            if value:
                page = link_key(r.get("redirects_to") or r["url"])
                pages.setdefault(value, {}).setdefault(page, []).append(r)
        for same_value in pages.values():
            if len(same_value) > 1:
                for group in same_value.values():
                    for r in group:
                        r["issues"].append(issue)


CSV_FIELDS = ["url", "status", "title", "title_length", "description", "description_length", "h1_count",
              "images_missing_alt", "noindex", "canonical", "redirects_to", "schema_types",
              "response_ms", "size_kb", "broken_links", "issues"]


CSV_DELIMITERS = {"comma": ",", "semicolon": ";"}


def write_csv(results, path, delimiter=","):
    # utf-8-sig so Excel shows Finnish characters correctly;
    # use delimiter=";" for Excel in locales like Finnish that expect semicolons.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, delimiter=delimiter)
        writer.writeheader()
        for r in results:
            row = {**r, "issues": "; ".join(r["issues"])}
            writer.writerow({k: csv_safe(v) for k, v in row.items()})


def csv_safe(value):
    """Prefix ' to text that Excel would run as a formula, e.g. a crawled title "=HYPERLINK(...)"."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def excel_safe(value):
    """Remove control characters that Excel files can't store (openpyxl would raise an error)."""
    return ILLEGAL_CHARACTERS_RE.sub("", value) if isinstance(value, str) else value


def write_xlsx(results, path):
    """Write the report as an Excel file; avoids CSV delimiter problems in non-English Excel."""
    wb = Workbook()
    wb.remove(wb.worksheets[0])  # replace the default sheet with a named one
    ws = wb.create_sheet("SEO report")
    ws.append(CSV_FIELDS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in results:
        # Missing fields become blank cells, like csv.DictWriter does for the CSV report.
        row = [r.get(field, "") if field != "issues" else "; ".join(r["issues"]) for field in CSV_FIELDS]
        ws.append([excel_safe(value) for value in row])
        for cell in ws[ws.max_row]:
            # A crawled title like "=HYPERLINK(...)" must stay text, not become a live formula.
            if cell.data_type == "f":
                cell.data_type = "s"
    # Fit each column to its longest value, capped so long URLs don't make huge columns.
    for index, column in enumerate(ws.columns, start=1):
        longest = max(len(str(cell.value or "")) for cell in column)
        ws.column_dimensions[get_column_letter(index)].width = min(longest + 2, 60)
    wb.save(path)


def print_summary(results, llms_txt, blocked_ai=None):
    """Print totals and issue counts; blocked_ai is the list from Crawler.blocked_ai_crawlers."""
    counts = Counter(issue for r in results for issue in r["issues"])
    print(f"\nPages checked: {len(results)}")
    print(f"Pages with issues: {sum(1 for r in results if r['issues'])}")
    print(f"llms.txt: {'found' if llms_txt else 'missing'}")
    if blocked_ai is not None:
        print(f"AI crawlers blocked by robots.txt: {', '.join(blocked_ai) if blocked_ai else 'none'}")
    if counts:
        print("\nIssues by type:")
        for issue, n in counts.most_common():
            print(f"  {n:4}  {issue}")


def normalize_url(url):
    """Add https:// if the user typed just a domain, e.g. example.fi."""
    url = url.strip()
    return url if "://" in url else "https://" + url


def positive_int(value):
    """argparse type: accept only whole numbers >= 1."""
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a whole number of at least 1, got '{value}'")
    return number


def add_timestamp(path, stamp):
    """Insert a timestamp before the extension so earlier reports are never overwritten.

    add_timestamp("reports/report.xlsx", "2026-11-15_143012") -> "reports/report_2026-11-15_143012.xlsx"
    """
    root, ext = os.path.splitext(path)
    return f"{root}_{stamp}{ext}"


def report_path_problem(path):
    """Return an error message if the report file can't be written, else None.

    Checked before crawling so an invalid path doesn't fail after a long crawl.
    """
    folder = os.path.dirname(os.path.abspath(path))
    if not os.path.isdir(folder):
        return f"folder does not exist: {folder}"
    return None


def main():
    parser = argparse.ArgumentParser(description="Check a website for common SEO and GEO issues.")
    parser.add_argument("url", type=normalize_url, help="Site root, e.g. https://example.fi")
    parser.add_argument("--max", type=positive_int, default=50, help="Max pages to check (default 50)")
    parser.add_argument("--csv", help="Write a CSV report to this file")
    parser.add_argument("--csv-delimiter", choices=CSV_DELIMITERS, default="comma",
                        help="CSV column separator; use 'semicolon' for Finnish Excel (default comma)")
    parser.add_argument("--xlsx", help="Write an Excel report to this file")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests (default 1)")
    parser.add_argument("--check-links", action="store_true",
                        help="Also check internal links on each page for broken targets (extra requests)")
    parser.add_argument("--max-links", type=positive_int, default=100,
                        help="Max extra link URLs to request with --check-links (default 100)")
    args = parser.parse_args()
    if not args.delay >= 0:  # time.sleep() would crash on a negative or NaN delay
        parser.error("--delay must be 0 or more")
    # One timestamp for the whole run, so the CSV and Excel files of a run share it.
    stamp = time.strftime("%Y-%m-%d_%H%M%S")
    if args.csv:
        args.csv = add_timestamp(args.csv, stamp)
    if args.xlsx:
        args.xlsx = add_timestamp(args.xlsx, stamp)
    for path in (args.csv, args.xlsx):
        problem = path and report_path_problem(path)
        if problem:
            parser.error(problem)

    crawler = Crawler(args.url, args.delay)
    results = []
    page_links, known_status = {}, {}
    broken, checked, capped = {}, 0, False
    for url, status, html, final_url, info in crawler.fetch_pages(args.max):
        print(f"  [{status}] {url}")
        results.append(check_page(html, url, status, final_url, info))
        known_status[url] = known_status[final_url] = status
        if args.check_links and status == 200:
            page_links[url] = internal_links(html, final_url)
    mark_duplicates(results)

    if args.check_links:
        broken, checked, capped = find_broken_links(page_links, known_status, crawler, args.max_links)
        for r in results:
            if r["url"] in broken:
                r["broken_links"] = "; ".join(broken[r["url"]])
                r["issues"].append("Broken internal links")

    print_summary(results, crawler.has_llms_txt(), crawler.blocked_ai_crawlers())
    if args.check_links:
        total = sum(len(links) for links in broken.values())
        print(f"Links: {checked} extra URLs requested, {total} broken links found")
        if capped:
            print(f"  (stopped at --max-links {args.max_links}; some links were not checked)")
    if args.csv:
        write_csv(results, args.csv, CSV_DELIMITERS[args.csv_delimiter])
        print(f"\nCSV report written to {args.csv}")
    if args.xlsx:
        write_xlsx(results, args.xlsx)
        print(f"Excel report written to {args.xlsx}")


if __name__ == "__main__":
    main()

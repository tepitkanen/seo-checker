"""SEO checker: crawls a site via its sitemap and reports common on-page SEO/GEO issues."""

import argparse
import csv
import json
import os
import time
import xml.etree.ElementTree as ET
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
SITEMAP_PATHS = ("/sitemap_index.xml", "/sitemap.xml")
# Links to these file types are not HTML pages, so they are not checked.
NON_PAGE_EXTENSIONS = (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".zip",
                       ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".mp3", ".mp4")


# ---------- Page checks (pure functions, no network) ----------

def check_page(html, url, status=200, final_url=None):
    """Analyse one page's HTML and return a dict of findings and a list of issues.

    final_url is the address after redirects; the canonical is compared against it.
    """
    result = {
        "url": url, "status": status, "title": "", "title_length": 0,
        "description_length": 0, "h1_count": 0, "images_missing_alt": 0,
        "noindex": False, "canonical": "", "redirects_to": "", "schema_types": "", "issues": [],
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
    result["description_length"] = len(desc)
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


class Crawler:
    def __init__(self, base_url, delay=1.0):
        self.base = base_url.rstrip("/")
        self.delay = delay
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.robots = RobotFileParser()
        resp = self.get(self.base + "/robots.txt")
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

    def discover_urls(self, max_pages):
        """Find page URLs from the sitemap, falling back to homepage links."""
        for path in SITEMAP_PATHS:
            resp = self.get(self.base + path)
            if resp is None or not resp.ok:
                continue
            try:
                urls = self._expand_sitemap(resp.text, max_pages)
            except ET.ParseError:
                continue
            if urls:
                return urls[:max_pages]
        resp = self.get(self.base + "/")
        links = internal_links(resp.text, self.base + "/") if resp is not None and resp.ok else []
        return ([self.base + "/"] + [u for u in links if u.rstrip("/") != self.base])[:max_pages]

    def _same_site(self, url):
        return site_host(url) == site_host(self.base)

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
            resp = self.get(child)
            if resp is None or not resp.ok:
                continue
            try:
                pages += [u for u in parse_sitemap(resp.text)[0] if self._same_site(u)]
            except ET.ParseError:  # skip one broken sub-sitemap instead of losing all pages
                continue
        return pages

    def fetch_pages(self, max_pages):
        """Yield (url, status, html, final_url) for each discovered page allowed by robots.txt.

        final_url is where redirects ended up (same as url if there were none).
        """
        for url in self.discover_urls(max_pages):
            if not self.allowed(url):
                print(f"  skipped (robots.txt): {url}")
                continue
            resp = self.get(url)
            if resp is None:
                yield url, 0, "", url
            else:
                yield url, resp.status_code, decode_html(resp), resp.url or url

    def has_llms_txt(self):
        resp = self.get(self.base + "/llms.txt")
        return resp is not None and resp.ok and "text/html" not in resp.headers.get("Content-Type", "")


# ---------- Reporting ----------

CSV_FIELDS = ["url", "status", "title", "title_length", "description_length", "h1_count",
              "images_missing_alt", "noindex", "canonical", "redirects_to", "schema_types", "issues"]


CSV_DELIMITERS = {"comma": ",", "semicolon": ";"}


def write_csv(results, path, delimiter=","):
    # utf-8-sig so Excel shows Finnish characters correctly;
    # use delimiter=";" for Excel in locales like Finnish that expect semicolons.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, delimiter=delimiter)
        writer.writeheader()
        for r in results:
            writer.writerow({**r, "issues": "; ".join(r["issues"])})


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
        row = [r[field] if field != "issues" else "; ".join(r["issues"]) for field in CSV_FIELDS]
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


def print_summary(results, llms_txt):
    counts = Counter(issue for r in results for issue in r["issues"])
    print(f"\nPages checked: {len(results)}")
    print(f"Pages with issues: {sum(1 for r in results if r['issues'])}")
    print(f"llms.txt: {'found' if llms_txt else 'missing'}")
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
    for url, status, html, final_url in crawler.fetch_pages(args.max):
        print(f"  [{status}] {url}")
        results.append(check_page(html, url, status, final_url))

    print_summary(results, crawler.has_llms_txt())
    if args.csv:
        write_csv(results, args.csv, CSV_DELIMITERS[args.csv_delimiter])
        print(f"\nCSV report written to {args.csv}")
    if args.xlsx:
        write_xlsx(results, args.xlsx)
        print(f"Excel report written to {args.xlsx}")


if __name__ == "__main__":
    main()

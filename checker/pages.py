"""Page checks: pure functions on HTML and results, no network."""

import json
import re
from urllib.parse import urljoin, urlparse, urldefrag

from bs4 import BeautifulSoup

from checker.urls import NON_PAGE_EXTENSIONS, link_key, site_host

TITLE_MIN, TITLE_MAX = 30, 60
DESC_MIN, DESC_MAX = 70, 160
SLOW_RESPONSE_MS = 1000  # time until the server's response arrived
LARGE_PAGE_KB = 500      # size of the HTML itself, without images, CSS or scripts


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
        try:
            result["canonical"] = urljoin(page_url, canonical_href)
        except ValueError:  # malformed href such as "http://[x"
            result["canonical"] = canonical_href
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


def internal_links(html, base_url):
    """Return unique same-host links found in the HTML, without fragments."""
    host = site_host(base_url)
    links = []
    for a in BeautifulSoup(html, "html.parser").select("a[href]"):
        try:
            link = urldefrag(urljoin(base_url, get_attr(a, "href")))[0]
            if site_host(link) != host or urlparse(link).path.lower().endswith(NON_PAGE_EXTENSIONS):
                continue
        except ValueError:  # malformed href such as "http://[x" raises "Invalid IPv6 URL"
            continue
        if link not in links:
            links.append(link)
    return links


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

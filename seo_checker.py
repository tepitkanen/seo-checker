"""SEO checker: crawls a site via its sitemap and reports common on-page SEO/GEO issues.

This file is the command-line entry point. The work is done in the checker package:
checker.urls (URL helpers), checker.pages (HTML checks), checker.crawl (network)
and checker.report (CSV/Excel/summary output).
"""

import argparse
import os
import time

# Re-exported so "from seo_checker import ..." keeps working for tests and other scripts.
from checker.crawl import (AI_CRAWLERS, MAX_SITEMAP_BYTES, SITEMAP_PATHS, TIMEOUT,  # noqa: F401
                           USER_AGENT, Crawler, decode_html, find_broken_links, parse_sitemap,
                           sitemap_text)
from checker.pages import (DESC_MAX, DESC_MIN, LARGE_PAGE_KB, SLOW_RESPONSE_MS,  # noqa: F401
                           TITLE_MAX, TITLE_MIN, check_page, find_meta, find_schema_types,
                           get_attr, internal_links, mark_duplicates)
from checker.report import (CSV_DELIMITERS, CSV_FIELDS, csv_safe, excel_safe,  # noqa: F401
                            print_summary, write_csv, write_xlsx)
from checker.urls import NON_PAGE_EXTENSIONS, link_key, printable, site_host  # noqa: F401


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
        print(f"  [{status}] {printable(url)}")
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

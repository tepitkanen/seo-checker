import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import time

import pytest
import requests
from openpyxl import load_workbook

from seo_checker import (add_timestamp, check_page, internal_links, main, normalize_url,
                         parse_sitemap, positive_int, report_path_problem, write_csv, write_xlsx)

URL = "https://example.fi/page"

GOOD_HTML = f"""
<html><head>
<title>A well sized page title for testing SEO</title>
<meta name="description" content="A meta description that is long enough to pass the minimum length check. Extra text here.">
<link rel="canonical" href="{URL}">
<script type="application/ld+json">{{"@context": "https://schema.org", "@graph": [{{"@type": "Organization"}}, {{"@type": "WebPage"}}]}}</script>
</head><body>
<h1>Heading</h1>
<img src="a.jpg" alt="A picture">
</body></html>
"""


def test_good_page_has_no_issues():
    result = check_page(GOOD_HTML, URL)
    assert result["issues"] == []
    assert result["h1_count"] == 1
    assert result["schema_types"] == "Organization, WebPage"


def test_empty_page_reports_missing_elements():
    issues = check_page("<html><body></body></html>", URL)["issues"]
    for expected in ["Missing title", "Missing meta description", "Missing H1",
                     "Missing canonical", "No structured data"]:
        assert expected in issues


def test_title_length_limits():
    assert "Title too short" in check_page("<title>Short</title>", URL)["issues"]
    assert "Title too long" in check_page(f"<title>{'x' * 61}</title>", URL)["issues"]


def test_description_length_limits():
    short = '<meta name="description" content="Too short">'
    long = f'<meta name="description" content="{"x" * 161}">'
    assert "Meta description too short" in check_page(short, URL)["issues"]
    assert "Meta description too long" in check_page(long, URL)["issues"]


def test_multiple_h1_and_missing_alt():
    html = '<h1>One</h1><h1>Two</h1><img src="a.jpg"><img src="b.jpg" alt="">'
    result = check_page(html, URL)
    assert "Multiple H1" in result["issues"]
    assert result["images_missing_alt"] == 2


def test_noindex_and_foreign_canonical():
    html = ('<meta name="robots" content="noindex, follow">'
            '<link rel="canonical" href="https://example.fi/other">')
    result = check_page(html, URL)
    assert result["noindex"] is True
    assert "Noindex" in result["issues"]
    assert "Canonical points elsewhere" in result["issues"]


def test_invalid_json_ld_is_ignored():
    html = '<script type="application/ld+json">{not json</script>'
    assert "No structured data" in check_page(html, URL)["issues"]


def test_http_error_skips_html_checks():
    result = check_page("", URL, status=404)
    assert result["issues"] == ["HTTP 404"]


def test_parse_sitemap_and_index():
    urlset = ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" '
              'xmlns:image="http://www.google.com/schemas/sitemap-image/1.1">'
              '<url><loc>https://example.fi/a</loc>'
              '<image:image><image:loc>https://example.fi/a.png</image:loc></image:image>'
              '</url></urlset>')
    index = ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
             '<sitemap><loc>https://example.fi/page-sitemap.xml</loc></sitemap></sitemapindex>')
    assert parse_sitemap(urlset) == (["https://example.fi/a"], False)
    assert parse_sitemap(index) == (["https://example.fi/page-sitemap.xml"], True)


def test_internal_links_filters_external_and_fragments():
    html = '<a href="/a#top">A</a><a href="/a">A</a><a href="https://other.com/">X</a>'
    assert internal_links(html, "https://example.fi/") == ["https://example.fi/a"]


def test_internal_links_treats_www_as_same_site():
    html = '<a href="https://www.example.fi/a">A</a><a href="https://EXAMPLE.fi/b">B</a>'
    assert internal_links(html, "https://example.fi/") == [
        "https://www.example.fi/a", "https://EXAMPLE.fi/b"]


def test_internal_links_skips_files():
    html = '<a href="/brochure.PDF">PDF</a><a href="/img/logo.png">Logo</a><a href="/page">P</a>'
    assert internal_links(html, "https://example.fi/") == ["https://example.fi/page"]


def test_write_csv(tmp_path):
    path = tmp_path / "report.csv"
    write_csv([check_page("<title>Äätö</title>", URL)], path)
    text = path.read_text(encoding="utf-8-sig")
    assert "Äätö" in text and "Title too short" in text


def test_write_csv_semicolon(tmp_path):
    path = tmp_path / "report.csv"
    write_csv([check_page("<title>Äätö</title>", URL)], path, delimiter=";")
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert rows[0]["url"] == URL and rows[0]["title"] == "Äätö"
    # The issues field contains "; " itself, so it must survive as one quoted cell.
    assert rows[0]["issues"].startswith("Title too short; ")


def test_add_timestamp():
    assert add_timestamp("report.csv", "2026-11-15_143012") == "report_2026-11-15_143012.csv"
    assert add_timestamp(os.path.join("reports", "r.xlsx"), "S") == os.path.join("reports", "r_S.xlsx")
    assert add_timestamp("report", "S") == "report_S"  # no extension


def test_main_adds_same_timestamp_to_both_reports(tmp_path, monkeypatch):
    # Fake site: everything returns 404, so the run is fast and offline.
    class NotFound:
        status_code, ok, text, content, url, headers = 404, False, "", b"", "", {}
    monkeypatch.setattr(requests.Session, "get", lambda session, url, timeout=None: NotFound())
    monkeypatch.setattr(time, "strftime", lambda fmt: "2026-11-15_143012")
    monkeypatch.setattr(sys, "argv", ["seo_checker.py", "https://example.fi", "--delay", "0",
                                      "--csv", str(tmp_path / "r.csv"), "--xlsx", str(tmp_path / "r.xlsx")])
    main()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["r_2026-11-15_143012.csv",
                                                          "r_2026-11-15_143012.xlsx"]


def test_normalize_url_adds_https():
    assert normalize_url("example.fi") == "https://example.fi"
    assert normalize_url(" http://example.fi ") == "http://example.fi"


def test_positive_int():
    assert positive_int("5") == 5
    for bad in ["0", "-3", "abc", "2.5"]:
        with pytest.raises(argparse.ArgumentTypeError):
            positive_int(bad)


def test_report_path_problem(tmp_path):
    assert report_path_problem(str(tmp_path / "new.csv")) is None
    existing = tmp_path / "old.csv"
    existing.write_text("keep")
    assert report_path_problem(str(existing)) is None
    assert existing.read_text() == "keep"  # the check must not change the file
    problem = report_path_problem(str(tmp_path / "missing" / "r.csv"))
    assert problem and "folder does not exist" in problem


def test_write_xlsx(tmp_path):
    path = tmp_path / "report.xlsx"
    write_xlsx([check_page("<title>Äätö</title>", URL)], path)
    ws = load_workbook(path)["SEO report"]
    header = [cell.value for cell in ws[1]]
    row = dict(zip(header, [cell.value for cell in ws[2]]))
    assert ws[1][0].font.bold
    assert row["url"] == URL and row["title"] == "Äätö"
    assert "Title too short; " in str(row["issues"])

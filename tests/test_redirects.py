"""Tests for redirect handling (final_url, redirects_to column) and decode_html edge cases."""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from openpyxl import load_workbook

from seo_checker import CSV_FIELDS, Crawler, check_page, decode_html
from test_crawler import BASE, FakeResponse, make_site, run_main, sitemap  # noqa: F401 (fixture)

REDIRECT = "Redirects to another URL"


def canonical(href):
    return f'<link rel="canonical" href="{href}">'


# ---------- check_page: redirect + canonical combinations ----------

def test_canonical_pointing_to_original_url_after_redirect_is_flagged():
    # /old redirects to /new, but /new says its canonical is /old: a loop of mixed signals.
    result = check_page(canonical(f"{BASE}/old"), f"{BASE}/old", final_url=f"{BASE}/new")
    assert REDIRECT in result["issues"]
    assert "Canonical points elsewhere" in result["issues"]


def test_canonical_matching_final_url_is_fine_but_redirect_is_reported():
    result = check_page(canonical(f"{BASE}/new"), f"{BASE}/old", final_url=f"{BASE}/new")
    assert REDIRECT in result["issues"]
    assert "Canonical points elsewhere" not in result["issues"]


def test_root_relative_canonical_uses_final_host():
    result = check_page(canonical("/new"), "http://example.fi/old", final_url=f"{BASE}/new")
    assert result["canonical"] == f"{BASE}/new"
    assert "Canonical points elsewhere" not in result["issues"]


@pytest.mark.parametrize("status", [404, 500])
def test_redirect_to_error_page_reports_redirect_then_status(status):
    result = check_page("", f"{BASE}/old", status=status, final_url=f"{BASE}/gone")
    assert result["issues"] == [REDIRECT, f"HTTP {status}"]
    assert result["redirects_to"] == f"{BASE}/gone"


def test_http_to_https_redirect_is_reported():
    result = check_page("", "http://example.fi/a", final_url="https://example.fi/a")
    assert result["redirects_to"] == "https://example.fi/a"
    assert REDIRECT in result["issues"]


def test_www_redirect_is_reported():
    result = check_page("", "https://example.fi/a", final_url="https://www.example.fi/a")
    assert REDIRECT in result["issues"]


def test_query_string_change_is_a_redirect():
    result = check_page("", f"{BASE}/a?x=1", final_url=f"{BASE}/a")
    assert REDIRECT in result["issues"]


def test_same_query_string_is_not_a_redirect():
    result = check_page("", f"{BASE}/a?x=1", final_url=f"{BASE}/a?x=1")
    assert result["redirects_to"] == ""


def test_trailing_slash_only_is_not_a_redirect():
    result = check_page("", f"{BASE}/a", final_url=f"{BASE}/a/")
    assert result["redirects_to"] == ""
    assert REDIRECT not in result["issues"]


def test_case_difference_in_path_is_a_redirect():
    # Paths are case-sensitive, so /About -> /about is a real redirect.
    result = check_page("", f"{BASE}/About", final_url=f"{BASE}/about")
    assert REDIRECT in result["issues"]


def test_empty_final_url_means_no_redirect():
    result = check_page("", f"{BASE}/a", final_url="")
    assert result["redirects_to"] == ""
    assert REDIRECT not in result["issues"]


def test_network_error_has_no_redirect_issue():
    result = check_page("", f"{BASE}/a", status=0, final_url=f"{BASE}/a")
    assert result["issues"] == ["Request failed"]


def test_redirect_issue_comes_before_page_issues():
    result = check_page("<html></html>", f"{BASE}/old", final_url=f"{BASE}/new")
    assert result["issues"][0] == REDIRECT


# ---------- Crawler.fetch_pages ----------

def test_fetch_pages_final_url_defaults_to_url_when_response_has_none(make_site):
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a")),
               f"{BASE}/a": FakeResponse("<html></html>", url="")})
    [(url, _, _, final_url)] = list(Crawler(BASE, delay=0).fetch_pages(10))
    assert final_url == url


def test_fetch_pages_redirect_to_404_keeps_error_status(make_site):
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a")),
               f"{BASE}/a": FakeResponse("", status=404, url=f"{BASE}/gone")})
    assert list(Crawler(BASE, delay=0).fetch_pages(10)) == [(f"{BASE}/a", 404, "", f"{BASE}/gone")]


# ---------- main() end to end ----------

@pytest.fixture
def redirect_site(make_site):
    return make_site({
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/old", f"{BASE}/plain")),
        f"{BASE}/old": FakeResponse("<html></html>", content_type="text/html", url=f"{BASE}/new"),
        f"{BASE}/plain": FakeResponse("<html></html>", content_type="text/html", url=f"{BASE}/plain"),
    })


@pytest.mark.parametrize("name, delimiter", [("comma", ","), ("semicolon", ";")])
def test_main_csv_has_redirects_to_column(redirect_site, monkeypatch, tmp_path, name, delimiter):
    out_file = tmp_path / "r.csv"
    run_main(monkeypatch, BASE, "--delay", "0", "--csv", str(out_file), "--csv-delimiter", name)
    with open(out_file, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=delimiter))
    assert "redirects_to" in rows[0]
    assert rows[0]["redirects_to"] == f"{BASE}/new"
    assert REDIRECT in rows[0]["issues"]
    assert rows[1]["redirects_to"] == ""  # no redirect -> empty
    assert REDIRECT not in rows[1]["issues"]


def test_main_xlsx_has_redirects_to_column(redirect_site, monkeypatch, tmp_path):
    out_file = tmp_path / "r.xlsx"
    run_main(monkeypatch, BASE, "--delay", "0", "--xlsx", str(out_file))
    ws = load_workbook(out_file)["SEO report"]
    header = [c.value for c in ws[1]]
    col = header.index("redirects_to")
    assert header == CSV_FIELDS
    assert ws.cell(row=2, column=col + 1).value == f"{BASE}/new"
    assert ws.cell(row=3, column=col + 1).value in (None, "")


def test_main_console_summary_counts_redirects(redirect_site, monkeypatch, capsys):
    run_main(monkeypatch, BASE, "--delay", "0")
    out = capsys.readouterr().out
    assert "1  Redirects to another URL" in out


def test_main_passes_final_url_to_canonical_check(make_site, monkeypatch, tmp_path):
    html = f'<html>{canonical(f"{BASE}/new")}</html>'
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/old")),
               f"{BASE}/old": FakeResponse(html, content_type="text/html", url=f"{BASE}/new")})
    out_file = tmp_path / "r.csv"
    run_main(monkeypatch, BASE, "--delay", "0", "--csv", str(out_file))
    with open(out_file, newline="", encoding="utf-8-sig") as f:
        [row] = list(csv.DictReader(f))
    assert "Canonical points elsewhere" not in row["issues"]
    assert row["url"] == f"{BASE}/old"


# ---------- decode_html ----------

def html_response(content, content_type="text/html"):
    return FakeResponse(text=content.decode("latin-1"), content_type=content_type, content=content)


def test_decode_utf8_with_bom_has_no_bom_character():
    text = decode_html(html_response(b"\xef\xbb\xbf<title>Keng\xc3\xa4t</title>"))
    assert "Kengät" in text
    assert "﻿" not in text


def test_decode_utf8_without_meta_tag():
    assert "Kengät" in decode_html(html_response("<title>Kengät</title>".encode("utf-8")))


def test_decode_windows_1252_without_meta_tag():
    assert "Kengät" in decode_html(html_response("<title>Kengät</title>".encode("windows-1252")))


def test_decode_windows_1252_smart_quote_is_not_garbled():
    # 0x93/0x94 are curly quotes in windows-1252 (control characters in ISO-8859-1).
    text = decode_html(html_response(b"<title>\x93Hello\x94</title>"))
    assert "“Hello”" in text


def test_decode_invalid_utf8_bytes_does_not_crash():
    text = decode_html(html_response(b"<title>Kengat \xff\xfe broken</title>"))
    assert "<title>" in text and "broken" in text


def test_decode_utf8_with_one_stray_byte_keeps_other_text_readable():
    text = decode_html(html_response("<title>Kengät \xff</title>".encode("utf-8").replace(b"\xc3\xbf", b"\xff")))
    assert "title" in text


def test_decode_meta_charset_contradicting_bytes_does_not_crash():
    # Page claims ISO-8859-1 but the bytes are UTF-8; any readable result is acceptable.
    body = b'<meta charset="iso-8859-1"><title>Kengat \xc3\xa4</title>'
    text = decode_html(html_response(body))
    assert "<title>" in text


def test_decode_meta_charset_utf8_but_bytes_are_latin1_does_not_crash():
    body = b'<meta charset="utf-8"><title>Kengat \xe4</title>'
    text = decode_html(html_response(body))
    assert "Kengat" in text


def test_decode_empty_body_returns_empty_string():
    assert decode_html(html_response(b"")) == ""


def test_decode_header_charset_wins_over_content():
    resp = FakeResponse(text="Kengät", content_type="text/html; charset=utf-8", content=b"\xff\xff")
    assert decode_html(resp) == "Kengät"

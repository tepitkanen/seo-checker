"""Tests for the second round of fixes: decoding, Excel safety, status messages,
sitemap host filtering and main() argument checks."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from openpyxl import load_workbook

import seo_checker
from seo_checker import (CSV_FIELDS, Crawler, check_page, decode_html, excel_safe, main,
                         report_path_problem, write_xlsx)
from test_crawler import BASE, FakeResponse, sitemap, sitemap_index, make_site  # noqa: F401


# ---------- decode_html ----------

def make_resp(body_bytes, content_type="text/html"):  # content_type=None: no Content-Type header
    resp = FakeResponse("WRONG-requests-text", content_type=content_type or "", content=body_bytes)
    if content_type is None:
        resp.headers = {}
    return resp


def test_decode_meta_charset_windows_1252():
    body = '<html><head><meta charset="windows-1252"></head><body>Kengät</body></html>'
    assert "Kengät" in decode_html(make_resp(body.encode("windows-1252")))


def test_decode_meta_charset_iso_8859_1():
    body = ('<html><head><meta http-equiv="Content-Type" content="text/html; charset=ISO-8859-1">'
            '</head><body>Sää</body></html>')
    assert "Sää" in decode_html(make_resp(body.encode("iso-8859-1")))


def test_decode_short_utf8_without_meta():
    # Encoding guesses fail on short input, so UTF-8 must be tried first.
    assert "Kengät ääkköset" in decode_html(make_resp("<p>Kengät ääkköset</p>".encode("utf-8")))


def test_decode_short_latin1_without_meta():
    # Not valid UTF-8, so windows-1252 (a superset of ISO-8859-1 letters) is used next.
    assert "Kengät ääkköset" in decode_html(make_resp("<p>Kengät ääkköset</p>".encode("latin-1")))


def test_decode_realistic_utf8_without_meta():
    body = "<html><body><p>" + "Kengät ja saappaat ääkköset. " * 20 + "</p></body></html>"
    assert "Kengät ja saappaat ääkköset." in decode_html(make_resp(body.encode("utf-8")))


def test_decode_utf8_meta():
    body = '<meta charset="utf-8"><p>Kengät</p>'
    assert "Kengät" in decode_html(make_resp(body.encode("utf-8")))


def test_decode_empty_body_gives_empty_string():
    assert decode_html(make_resp(b"")) == ""


def test_decode_header_charset_uses_resp_text():
    resp = make_resp(b"ignored", content_type="text/html; charset=utf-8")
    assert decode_html(resp) == "WRONG-requests-text"


def test_decode_header_charset_is_case_insensitive():
    resp = make_resp(b"ignored", content_type="text/html; CHARSET=UTF-8")
    assert decode_html(resp) == "WRONG-requests-text"


def test_decode_missing_content_type_header():
    assert "Kengät" in decode_html(make_resp("<p>Kengät</p>".encode("utf-8"), content_type=None))


# ---------- excel_safe / write_xlsx ----------

def test_excel_safe_strips_control_characters():
    assert excel_safe("a\x00b\x07c\x1fd") == "abcd"


def test_excel_safe_keeps_normal_text_and_whitespace():
    assert excel_safe("Kengät\tline\nnext") == "Kengät\tline\nnext"


@pytest.mark.parametrize("value", [5, 2.5, True, False, None])
def test_excel_safe_leaves_non_strings_alone(value):
    assert excel_safe(value) is value


def result(**changes):
    base = {"url": f"{BASE}/", "status": 200, "title": "T", "title_length": 1,
            "description_length": 0, "h1_count": 1, "images_missing_alt": 0,
            "noindex": False, "canonical": "", "redirects_to": "", "schema_types": "", "issues": []}
    base.update(changes)
    return base


def read_first_row(path):
    ws = load_workbook(path)["SEO report"]
    return {name: cell for name, cell in zip(CSV_FIELDS, ws[2])}


def test_xlsx_strips_control_characters(tmp_path):
    path = tmp_path / "r.xlsx"
    write_xlsx([result(title="Bad\x00Ti\x0btle")], path)
    assert read_first_row(path)["title"].value == "BadTitle"


@pytest.mark.parametrize("text", ['=HYPERLINK("http://evil","x")', "=1+1", "+1+1", "-1+1", "@SUM(1)"])
def test_xlsx_formula_like_text_stays_text(tmp_path, text):
    path = tmp_path / "r.xlsx"
    write_xlsx([result(title=text)], path)
    cell = read_first_row(path)["title"]
    assert cell.data_type != "f"
    assert cell.value == text


def test_xlsx_formula_in_issues_and_url_stays_text(tmp_path):
    path = tmp_path / "r.xlsx"
    write_xlsx([result(url="=cmd()", issues=["=bad", "ok"])], path)
    row = read_first_row(path)
    assert row["url"].data_type != "f"
    assert row["issues"].data_type != "f"
    assert row["issues"].value == "=bad; ok"


def test_xlsx_numbers_and_booleans_keep_their_types(tmp_path):
    path = tmp_path / "r.xlsx"
    write_xlsx([result(status=200, title_length=42, noindex=True)], path)
    row = read_first_row(path)
    assert row["status"].value == 200
    assert row["title_length"].value == 42
    assert row["noindex"].value is True


def test_xlsx_column_width_is_capped_at_60(tmp_path):
    path = tmp_path / "r.xlsx"
    write_xlsx([result(title="x" * 500)], path)
    ws = load_workbook(path)["SEO report"]
    assert ws.column_dimensions["C"].width == 60
    assert ws.column_dimensions["A"].width < 60  # short column is not stretched


def test_xlsx_with_no_results_writes_header_only(tmp_path):
    path = tmp_path / "r.xlsx"
    write_xlsx([], path)
    assert [c.value for c in load_workbook(path)["SEO report"][1]] == CSV_FIELDS


# ---------- check_page status messages ----------

def test_check_page_status_zero_is_request_failed():
    assert check_page("", f"{BASE}/", status=0)["issues"] == ["Request failed"]


@pytest.mark.parametrize("status", [301, 403, 404, 500, 503])
def test_check_page_non_200_is_http_n(status):
    assert check_page("<html></html>", f"{BASE}/", status=status)["issues"] == [f"HTTP {status}"]


# ---------- sitemap host filtering ----------

def test_index_never_requests_other_host_child_sitemap(make_site):
    site = make_site({
        f"{BASE}/sitemap_index.xml": FakeResponse(sitemap_index(
            "https://evil.com/s.xml", f"{BASE}/s1.xml")),
        f"{BASE}/s1.xml": FakeResponse(sitemap(f"{BASE}/a")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a"]
    assert "https://evil.com/s.xml" not in site.requested


def test_other_host_page_urls_in_child_sitemap_are_dropped(make_site):
    make_site({
        f"{BASE}/sitemap_index.xml": FakeResponse(sitemap_index(f"{BASE}/s1.xml")),
        f"{BASE}/s1.xml": FakeResponse(sitemap(f"{BASE}/a", "https://evil.com/b")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a"]


def test_plain_sitemap_is_filtered_too(make_site):
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap(
        f"{BASE}/a", "https://evil.com/b", "https://example.fi.evil.com/c"))})
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a"]


def test_www_and_case_variants_are_kept(make_site):
    urls = ["https://www.example.fi/a", "https://EXAMPLE.FI/b", "https://Www.Example.fi/c"]
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap(*urls))})
    assert Crawler(BASE, delay=0).discover_urls(10) == urls


def test_www_base_accepts_bare_host_urls(make_site):
    base = "https://www.example.fi"
    make_site({f"{base}/sitemap.xml": FakeResponse(sitemap("https://example.fi/a"))})
    assert Crawler(base, delay=0).discover_urls(10) == ["https://example.fi/a"]


def test_subdomain_is_a_different_site(make_site):
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap("https://blog.example.fi/a", f"{BASE}/b"))})
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/b"]


def test_port_in_url_is_a_different_host(make_site):
    # netloc includes the port; documents current behaviour (port variants are dropped).
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap("https://example.fi:8080/a", f"{BASE}/b"))})
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/b"]


def test_index_with_only_other_hosts_falls_back_to_homepage(make_site):
    make_site({
        f"{BASE}/sitemap_index.xml": FakeResponse(sitemap_index("https://evil.com/s.xml")),
        f"{BASE}/": FakeResponse('<a href="/x">x</a>', content_type="text/html"),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/", f"{BASE}/x"]


# ---------- main(): --delay and report path checks ----------

def run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["seo_checker.py", BASE, *args])
    # Keep report names unchanged so tests can check the exact file.
    monkeypatch.setattr(seo_checker, "add_timestamp", lambda path, stamp: path)
    main()


@pytest.mark.parametrize("delay", ["-1", "-0.5", "nan", "NaN"])
def test_main_rejects_bad_delay(make_site, monkeypatch, capsys, delay):
    site = make_site({})
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, "--delay", delay)
    assert exc.value.code == 2
    assert "--delay" in capsys.readouterr().err
    assert site.requested == []  # rejected before any request


def test_main_rejects_non_numeric_delay(make_site, monkeypatch):
    make_site({})
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, "--delay", "abc")
    assert exc.value.code == 2


def test_main_accepts_zero_delay(make_site, monkeypatch, capsys):
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a"))})
    run_main(monkeypatch, "--delay", "0")
    assert "Pages checked: 1" in capsys.readouterr().out


def test_report_path_problem_missing_folder(tmp_path):
    problem = report_path_problem(str(tmp_path / "nope" / "r.csv"))
    assert problem and "folder does not exist" in problem


def test_report_path_problem_none_for_new_and_existing_file(tmp_path):
    assert report_path_problem(str(tmp_path / "new.csv")) is None
    existing = tmp_path / "old.csv"
    existing.write_text("keep me")
    assert report_path_problem(str(existing)) is None
    assert existing.read_text() == "keep me"  # the check must not change the file


def test_report_path_problem_relative_filename_in_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert report_path_problem("r.csv") is None


def test_main_missing_report_folder_exits_before_crawling(make_site, monkeypatch, capsys, tmp_path):
    site = make_site({})
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, "--csv", str(tmp_path / "nope" / "r.csv"))
    assert exc.value.code == 2
    assert "folder does not exist" in capsys.readouterr().err
    assert site.requested == []

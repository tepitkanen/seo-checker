"""Extra gap tests for headers / link checking: redirects, www hosts, XLSX, network errors."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from openpyxl import load_workbook

from seo_checker import CSV_FIELDS, Crawler, check_page, find_broken_links
from test_crawler import BASE, FakeResponse, make_site, run_main, sitemap  # noqa: F401 (fixture)

HTML = {"content_type": "text/html"}


def read_rows(path):
    ws = load_workbook(path).active
    head = [c.value for c in ws[1]]
    rows = {r[0].value: dict(zip(head, [c.value for c in r])) for r in ws.iter_rows(min_row=2)}
    return head, rows


def test_fetch_pages_info_is_empty_dict_on_network_error(make_site):
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a")),
               f"{BASE}/a": requests.ConnectionError("down")})
    [(url, status, html, final, info)] = list(Crawler(BASE, delay=0).fetch_pages(10))
    assert (status, html, info) == (0, "", {})
    result = check_page(html, url, status, final, info)
    assert result["issues"] == ["Request failed"]
    assert (result["response_ms"], result["size_kb"]) == (0, 0)


def test_fetch_pages_missing_header_gives_empty_string(make_site):
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a")),
               f"{BASE}/a": FakeResponse("<html></html>", **HTML)})
    [(*_, info)] = list(Crawler(BASE, delay=0).fetch_pages(10))
    assert info["x_robots_tag"] == ""


def test_x_robots_noindex_not_flagged_on_error_status():
    for status in (404, 500, 0):
        result = check_page("", f"{BASE}/a", status=status, info={"x_robots_tag": "noindex"})
        assert "Noindex (X-Robots-Tag)" not in result["issues"]
        assert result["noindex"] is False


def test_x_robots_noindex_still_flagged_on_redirected_200():
    result = check_page("<html></html>", f"{BASE}/old", 200, f"{BASE}/new",
                        {"x_robots_tag": "noindex"})
    assert "Noindex (X-Robots-Tag)" in result["issues"]


def test_known_status_redirect_final_url_not_requested_again(make_site):
    site = make_site({})
    crawler = Crawler(BASE, delay=0)
    site.requested.clear()
    known = {f"{BASE}/old": 200, f"{BASE}/new": 200}
    broken, checked, _ = find_broken_links({f"{BASE}/a": [f"{BASE}/new", f"{BASE}/old"]},
                                           known, crawler, 10)
    assert (broken, checked, site.requested) == ({}, 0, [])


def test_main_redirected_page_links_resolve_against_final_url(make_site, monkeypatch, tmp_path):
    site = make_site({
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/old")),
        f"{BASE}/old": FakeResponse('<a href="sib">s</a><a href="/dir/new">self</a><a href="/old">o</a>',
                                    url=f"{BASE}/dir/new", **HTML),
        f"{BASE}/dir/sib": FakeResponse("ok", **HTML),
    })
    run_main(monkeypatch, BASE, "--delay", "0", "--check-links", "--csv", str(tmp_path / "r.csv"))
    assert site.requested.count(f"{BASE}/dir/sib") == 1
    assert site.requested.count(f"{BASE}/dir/new") == 0  # final URL is known
    assert site.requested.count(f"{BASE}/old") == 1      # only the crawl itself
    assert f"{BASE}/sib" not in site.requested           # wrong base


def test_main_link_to_redirect_target_that_404s_is_broken(make_site, monkeypatch, tmp_path):
    make_site({
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/old", f"{BASE}/a")),
        f"{BASE}/old": FakeResponse("gone", status=404, url=f"{BASE}/new"),
        f"{BASE}/a": FakeResponse('<a href="/new">n</a>', **HTML),
    })
    out = tmp_path / "r.xlsx"
    run_main(monkeypatch, BASE, "--delay", "0", "--check-links", "--xlsx", str(out))
    _, rows = read_rows(out)
    assert rows[f"{BASE}/a"]["broken_links"] == f"{BASE}/new"


def test_xlsx_has_new_columns_and_values(make_site, monkeypatch, tmp_path):
    make_site({
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a", f"{BASE}/b")),
        f"{BASE}/a": FakeResponse('<a href="/missing">x</a>', headers={"X-Robots-Tag": "noindex"},
                                  elapsed_ms=1500, **HTML),
        f"{BASE}/b": FakeResponse("<html></html>", **HTML),
    })
    out = tmp_path / "r.xlsx"
    run_main(monkeypatch, BASE, "--delay", "0", "--check-links", "--xlsx", str(out))
    head, rows = read_rows(out)
    assert head == CSV_FIELDS
    a, b = rows[f"{BASE}/a"], rows[f"{BASE}/b"]
    assert a["response_ms"] == 1500
    assert a["size_kb"] == 0
    assert a["broken_links"] == f"{BASE}/missing"
    for issue in ("Slow response", "Noindex (X-Robots-Tag)", "Broken internal links"):
        assert issue in a["issues"]
    assert b["broken_links"] in (None, "")


def test_xlsx_without_check_links_has_blank_broken_links(make_site, monkeypatch, tmp_path):
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a")),
               f"{BASE}/a": FakeResponse("<html></html>", **HTML)})
    out = tmp_path / "r.xlsx"
    run_main(monkeypatch, BASE, "--delay", "0", "--xlsx", str(out))
    _, rows = read_rows(out)
    assert rows[f"{BASE}/a"]["broken_links"] in (None, "")


def test_xlsx_broken_links_formula_like_value_stays_text(make_site, monkeypatch, tmp_path):
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a")),
               f"{BASE}/a": FakeResponse('<a href="/=SUM(1)">x</a>', **HTML)})
    out = tmp_path / "r.xlsx"
    run_main(monkeypatch, BASE, "--delay", "0", "--check-links", "--xlsx", str(out))
    cell = load_workbook(out).active[2][CSV_FIELDS.index("broken_links")]
    assert cell.data_type != "f"


# ---------- www host variants ----------

def test_www_link_to_crawled_page_is_not_requested_again(make_site):
    site = make_site({})
    crawler = Crawler(BASE, delay=0)
    site.requested.clear()
    known = {f"{BASE}/b": 200}
    find_broken_links({f"{BASE}/a": ["https://www.example.fi/b"]}, known, crawler, 10)
    assert site.requested == [], "www. variant of a crawled page was requested again"


def test_main_www_links_are_internal_and_crawled_pages_not_refetched(make_site, monkeypatch, tmp_path):
    site = make_site({
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a", f"{BASE}/b")),
        f"{BASE}/a": FakeResponse('<a href="https://www.example.fi/b">b</a>'
                                  '<a href="https://www.example.fi/nope">n</a>', **HTML),
        f"{BASE}/b": FakeResponse("<html></html>", **HTML),
    })
    run_main(monkeypatch, BASE, "--delay", "0", "--check-links", "--csv", str(tmp_path / "r.csv"))
    assert "https://www.example.fi/nope" in site.requested  # treated as internal
    assert "https://www.example.fi/b" not in site.requested


def test_www_site_with_non_www_links_not_refetched(make_site, monkeypatch, tmp_path):
    base = "https://www.example.fi"
    site = make_site({
        f"{base}/sitemap.xml": FakeResponse(sitemap(f"{base}/a", f"{base}/b")),
        f"{base}/a": FakeResponse('<a href="https://example.fi/b">b</a>', **HTML),
        f"{base}/b": FakeResponse("<html></html>", **HTML),
    })
    run_main(monkeypatch, base, "--delay", "0", "--check-links", "--csv", str(tmp_path / "r.csv"))
    assert "https://example.fi/b" not in site.requested

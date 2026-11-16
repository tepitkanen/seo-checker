"""Tests for X-Robots-Tag, response time / page size and broken internal link checks."""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import requests

from seo_checker import (LARGE_PAGE_KB, SLOW_RESPONSE_MS, Crawler, check_page,
                         find_broken_links)
from test_crawler import BASE, FakeResponse, make_site, run_main, sitemap  # noqa: F401 (fixture)

URL = f"{BASE}/a"


# ---------- X-Robots-Tag ----------

@pytest.mark.parametrize("header", ["noindex", "NOINDEX, nofollow", "googlebot: noindex", "none"])
def test_x_robots_tag_noindex_is_flagged(header):
    result = check_page("<html></html>", URL, info={"x_robots_tag": header})
    assert result["noindex"] is True
    assert "Noindex (X-Robots-Tag)" in result["issues"]
    assert "Noindex" not in result["issues"]  # the meta tag issue is separate


def test_x_robots_tag_without_noindex_is_not_flagged():
    result = check_page("<html></html>", URL, info={"x_robots_tag": "nofollow"})
    assert result["noindex"] is False
    assert "Noindex (X-Robots-Tag)" not in result["issues"]


def test_meta_and_header_noindex_are_both_reported():
    html = '<meta name="robots" content="noindex">'
    issues = check_page(html, URL, info={"x_robots_tag": "noindex"})["issues"]
    assert "Noindex" in issues and "Noindex (X-Robots-Tag)" in issues


def test_fetch_pages_passes_header_time_and_size(make_site):
    make_site({
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(URL)),
        URL: FakeResponse("x" * 2048, headers={"X-Robots-Tag": "noindex"}, elapsed_ms=1234),
    })
    [(*_, info)] = list(Crawler(BASE, delay=0).fetch_pages(10))
    assert info == {"x_robots_tag": "noindex", "response_ms": 1234, "size_kb": 2}


# ---------- Response time and page size ----------

def test_slow_response_threshold():
    assert "Slow response" not in check_page("", URL, info={"response_ms": SLOW_RESPONSE_MS})["issues"]
    result = check_page("", URL, info={"response_ms": SLOW_RESPONSE_MS + 1})
    assert "Slow response" in result["issues"]
    assert result["response_ms"] == SLOW_RESPONSE_MS + 1


def test_large_page_threshold():
    assert "Large page" not in check_page("", URL, info={"size_kb": LARGE_PAGE_KB})["issues"]
    result = check_page("", URL, info={"size_kb": LARGE_PAGE_KB + 1})
    assert "Large page" in result["issues"]
    assert result["size_kb"] == LARGE_PAGE_KB + 1


def test_error_pages_only_report_status_not_speed():
    result = check_page("", URL, status=500, info={"response_ms": 5000, "size_kb": 900})
    assert result["issues"] == ["HTTP 500"]


def test_no_info_means_zero_and_no_issues():
    result = check_page("<html></html>", URL)
    assert (result["response_ms"], result["size_kb"], result["broken_links"]) == (0, 0, "")
    assert not {"Slow response", "Large page", "Noindex (X-Robots-Tag)"} & set(result["issues"])


# ---------- find_broken_links ----------

def crawler_for(make_site, pages):
    site = make_site(pages)
    return Crawler(BASE, delay=0), site


def test_broken_link_is_reported_on_the_right_page(make_site):
    crawler, _ = crawler_for(make_site, {f"{BASE}/ok": FakeResponse("fine")})  # /missing -> 404
    page_links = {f"{BASE}/a": [f"{BASE}/ok", f"{BASE}/missing"], f"{BASE}/b": [f"{BASE}/ok"]}
    broken, checked, capped = find_broken_links(page_links, {}, crawler, 100)
    assert broken == {f"{BASE}/a": [f"{BASE}/missing"]}
    assert (checked, capped) == (2, False)


def test_crawled_pages_are_not_requested_again(make_site):
    crawler, site = crawler_for(make_site, {})
    site.requested.clear()
    known = {f"{BASE}/gone": 404, f"{BASE}/ok/": 200}
    page_links = {f"{BASE}/a": [f"{BASE}/gone/", f"{BASE}/ok"]}  # trailing slashes ignored
    broken, checked, _ = find_broken_links(page_links, known, crawler, 100)
    assert broken == {f"{BASE}/a": [f"{BASE}/gone/"]}
    assert checked == 0 and site.requested == []


def test_each_link_is_requested_once(make_site):
    crawler, site = crawler_for(make_site, {})
    site.requested.clear()
    page_links = {f"{BASE}/a": [f"{BASE}/x"], f"{BASE}/b": [f"{BASE}/x"]}
    broken, checked, _ = find_broken_links(page_links, {}, crawler, 100)
    assert site.requested == [f"{BASE}/x"] and checked == 1
    assert set(broken) == {f"{BASE}/a", f"{BASE}/b"}


def test_network_error_counts_as_broken(make_site):
    crawler, _ = crawler_for(make_site, {f"{BASE}/down": requests.ConnectionError("boom")})
    broken, _, _ = find_broken_links({URL: [f"{BASE}/down"]}, {}, crawler, 100)
    assert broken == {URL: [f"{BASE}/down"]}


def test_robots_disallowed_link_is_not_requested(make_site):
    crawler, site = crawler_for(make_site, {
        f"{BASE}/robots.txt": FakeResponse("User-agent: *\nDisallow: /private\n")})
    site.requested.clear()
    broken, checked, _ = find_broken_links({URL: [f"{BASE}/private/x"]}, {}, crawler, 100)
    assert (broken, checked, site.requested) == ({}, 0, [])


def test_max_links_caps_requests(make_site):
    crawler, site = crawler_for(make_site, {})
    site.requested.clear()
    links = [f"{BASE}/{i}" for i in range(5)]
    broken, checked, capped = find_broken_links({URL: links}, {}, crawler, 2)
    assert checked == 2 and capped is True
    assert site.requested == links[:2]
    assert broken == {URL: links[:2]}


# ---------- main() ----------

LINK_SITE = {
    f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a", f"{BASE}/b")),
    f"{BASE}/a": FakeResponse('<a href="/b">B</a> <a href="/missing">X</a> <a href="https://other.fi/">O</a>',
                              content_type="text/html"),
    f"{BASE}/b": FakeResponse("<html></html>", content_type="text/html"),
}


def test_main_without_check_links_makes_no_extra_requests(make_site, monkeypatch, capsys):
    site = make_site(LINK_SITE)
    run_main(monkeypatch, BASE, "--delay", "0")
    assert f"{BASE}/missing" not in site.requested
    assert "Links:" not in capsys.readouterr().out


def test_main_check_links_writes_broken_links_to_csv(make_site, monkeypatch, tmp_path, capsys):
    site = make_site(LINK_SITE)
    out_file = tmp_path / "r.csv"
    run_main(monkeypatch, BASE, "--delay", "0", "--check-links", "--csv", str(out_file))
    assert "https://other.fi/" not in site.requested  # external links are ignored
    assert site.requested.count(f"{BASE}/b") == 1     # crawled page is not requested again
    assert "Links: 1 extra URLs requested, 1 broken links found" in capsys.readouterr().out
    with open(out_file, newline="", encoding="utf-8-sig") as f:
        rows = {r["url"]: r for r in csv.DictReader(f)}
    assert rows[f"{BASE}/a"]["broken_links"] == f"{BASE}/missing"
    assert "Broken internal links" in rows[f"{BASE}/a"]["issues"]
    assert rows[f"{BASE}/b"]["broken_links"] == ""
    assert "response_ms" in rows[f"{BASE}/a"] and "size_kb" in rows[f"{BASE}/a"]


def test_main_reports_when_max_links_is_hit(make_site, monkeypatch, capsys):
    pages = dict(LINK_SITE)
    pages[f"{BASE}/a"] = FakeResponse('<a href="/x1">1</a><a href="/x2">2</a>', content_type="text/html")
    make_site(pages)
    run_main(monkeypatch, BASE, "--delay", "0", "--check-links", "--max-links", "1")
    assert "stopped at --max-links 1" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["0", "-1", "abc"])
def test_main_invalid_max_links_is_rejected(value, make_site, monkeypatch):
    make_site({})
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, BASE, "--delay", "0", "--max-links", value)
    assert exc.value.code == 2

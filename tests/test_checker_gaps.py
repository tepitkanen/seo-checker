"""Tests for the few behaviours the other test files leave untested (found by tracing line coverage).

Offline: requests.Session.get is replaced by a fake; no real network or sleeping.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import requests

import seo_checker
from checker import crawl
from checker.crawl import Crawler, find_broken_links
from seo_checker import main

BASE = "https://example.fi"
HTML = "<html><head><title>Hi</title></head><body><h1>x</h1>%s</body></html>"


class Resp:
    def __init__(self, text="", status=200, content_type="text/html", fail_after=None):
        self.content = text.encode("utf-8")
        self.text = text
        self.status_code = status
        self.ok = status < 400
        self.url = ""
        self.headers = {"Content-Type": content_type}
        self.fail_after = fail_after  # raise a network error after this many chunks
        self.closed = False
        self.elapsed = __import__("datetime").timedelta(0)

    def iter_content(self, chunk_size=1):
        for n, i in enumerate(range(0, len(self.content), chunk_size)):
            if self.fail_after is not None and n >= self.fail_after:
                raise requests.exceptions.ChunkedEncodingError("connection broke")
            yield self.content[i:i + chunk_size]

    def close(self):
        self.closed = True


class Pages(dict):
    """URL -> Resp (or exception to raise); .requested lists the URLs asked for."""

    def __init__(self):
        super().__init__()
        self.requested = []


@pytest.fixture
def site(monkeypatch):
    pages = Pages()
    requested = pages.requested

    def get(session, url, **kwargs):
        requested.append(url)
        page = pages.get(url, Resp(status=404))
        if isinstance(page, Exception):
            raise page
        return page

    monkeypatch.setattr(requests.Session, "get", get)
    monkeypatch.setattr(crawl.time, "sleep", lambda s: None)
    return pages


def run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["seo_checker.py", BASE, "--delay", "0", *args])
    monkeypatch.setattr(seo_checker, "add_timestamp", lambda path, stamp: path)
    main()


# ---------- Crawler.get ----------

def test_get_closes_response_when_stream_breaks_midway(site):
    broken = Resp("x" * 200000, fail_after=1)
    site[f"{BASE}/page"] = broken
    c = Crawler(BASE, delay=0)
    assert c.get(f"{BASE}/page") is None
    assert broken.closed


def test_get_closes_response_when_over_size_cap(site, monkeypatch):
    monkeypatch.setattr(crawl, "MAX_RESPONSE_BYTES", 10)
    big = Resp("x" * 1000)
    site[f"{BASE}/big"] = big
    assert Crawler(BASE, delay=0).get(f"{BASE}/big") is None
    assert big.closed


def test_get_sleeps_for_the_configured_delay_before_each_request(site, monkeypatch):
    naps = []
    monkeypatch.setattr(crawl.time, "sleep", naps.append)
    c = Crawler(BASE, delay=2.5)  # robots.txt request
    c.get(f"{BASE}/a")
    assert naps == [2.5, 2.5]


def test_robots_401_warns_and_allows_everything(site, capsys):
    site[f"{BASE}/robots.txt"] = Resp("User-agent: *\nDisallow: /\n", status=401)
    c = Crawler(BASE, delay=0)
    assert "robots.txt returned 401" in capsys.readouterr().out
    assert c.allowed(f"{BASE}/anything")


def test_robots_404_allows_everything_without_warning(site, capsys):
    c = Crawler(BASE, delay=0)
    assert "warning" not in capsys.readouterr().out
    assert c.allowed(f"{BASE}/anything")


# ---------- Crawler.link_status ----------

@pytest.mark.parametrize("status", [200, 301, 404, 500])
def test_link_status_returns_http_status(site, status):
    site[f"{BASE}/x"] = Resp("", status=status)
    assert Crawler(BASE, delay=0).link_status(f"{BASE}/x") == status


def test_link_status_zero_on_network_error(site):
    site[f"{BASE}/x"] = requests.ConnectionError("down")
    assert Crawler(BASE, delay=0).link_status(f"{BASE}/x") == 0


def test_link_status_zero_when_over_size_cap(site, monkeypatch):
    monkeypatch.setattr(crawl, "MAX_RESPONSE_BYTES", 5)
    site[f"{BASE}/x"] = Resp("x" * 100)
    assert Crawler(BASE, delay=0).link_status(f"{BASE}/x") == 0


# ---------- Crawler.has_llms_txt ----------

@pytest.mark.parametrize("ctype,expected", [
    ("text/html; charset=utf-8", False),
    ("text/plain; charset=utf-8", True),
    ("text/markdown", True),
])
def test_has_llms_txt_by_content_type(site, ctype, expected):
    site[f"{BASE}/llms.txt"] = Resp("# Site", content_type=ctype)
    assert Crawler(BASE, delay=0).has_llms_txt() is expected


def test_has_llms_txt_html_content_type_is_case_insensitive(site):
    # MIME types are case-insensitive (RFC 9110), so "TEXT/HTML" is an HTML page, not llms.txt.
    site[f"{BASE}/llms.txt"] = Resp("<html></html>", content_type="TEXT/HTML")
    assert Crawler(BASE, delay=0).has_llms_txt() is False


# ---------- find_broken_links with the real Crawler ----------

def test_find_broken_links_uses_real_crawler_and_dedupes_fragments(site):
    site[f"{BASE}/ok"] = Resp("")
    site[f"{BASE}/gone"] = Resp("", status=404)
    c = Crawler(BASE, delay=0)
    before = len(site.requested)
    broken, checked, capped = find_broken_links(
        {f"{BASE}/p": [f"{BASE}/ok", f"{BASE}/ok#top", f"{BASE}/gone"]}, {}, c, 10)
    assert broken == {f"{BASE}/p": [f"{BASE}/gone"]}
    assert checked == 2 and capped is False
    assert len(site.requested) - before == 2


# ---------- main() ----------

def test_main_without_check_links_prints_no_links_line_and_makes_no_extra_requests(site, monkeypatch, capsys):
    site[f"{BASE}/sitemap.xml"] = Resp(
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>%s/a</loc></url></urlset>' % BASE)
    site[f"{BASE}/a"] = Resp(HTML % '<a href="/other">o</a>')
    run_main(monkeypatch)
    assert "Links:" not in capsys.readouterr().out
    assert f"{BASE}/other" not in site.requested


def test_main_check_links_ignores_links_of_non_200_pages(site, monkeypatch, capsys):
    site[f"{BASE}/sitemap.xml"] = Resp(
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>%s/a</loc></url>'
        "<url><loc>%s/b</loc></url></urlset>" % (BASE, BASE))
    site[f"{BASE}/a"] = Resp(HTML % '<a href="/from-a">x</a>')
    site[f"{BASE}/b"] = requests.ConnectionError("down")
    run_main(monkeypatch, "--check-links")
    out = capsys.readouterr().out
    assert "Links: 1 extra URLs requested, 1 broken links found" in out
    assert f"{BASE}/from-a" in site.requested


def test_main_slightly_negative_delay_is_rejected(site, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["seo_checker.py", BASE, "--delay", "-0.001"])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 2


def test_main_report_in_missing_folder_exits_2_naming_the_folder(site, monkeypatch, tmp_path, capsys):
    target = tmp_path / "nope" / "r.csv"
    monkeypatch.setattr(sys, "argv", ["seo_checker.py", BASE, "--delay", "0", "--csv", str(target)])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 2
    assert "folder does not exist" in capsys.readouterr().err
    assert site.requested == []


# ---------- fixes from the code review of this round ----------
# chr(228) is the Finnish "a with dots"; built with chr() so no tool can mangle it.
A_UML = chr(228)


def test_percent_encoded_url_is_not_a_redirect_or_foreign_canonical():
    from checker.pages import check_page
    raw = f"{BASE}/keng{A_UML}t"
    sent = requests.Request("GET", raw).prepare().url  # requests percent-encodes the path
    assert sent != raw
    html = f'<title>t</title><link rel="canonical" href="{raw}">'
    issues = check_page(html, raw, 200, sent)["issues"]
    assert "Redirects to another URL" not in issues
    assert "Canonical points elsewhere" not in issues


def test_real_redirect_and_foreign_canonical_are_still_flagged():
    from checker.pages import check_page
    html = f'<title>t</title><link rel="canonical" href="{BASE}/other">'
    issues = check_page(html, f"{BASE}/a", 200, f"{BASE}/b")["issues"]
    assert "Redirects to another URL" in issues and "Canonical points elsewhere" in issues


def test_same_address_rules():
    from checker.urls import same_address
    assert same_address(f"{BASE}/a/", f"{BASE}/a")             # trailing slash ignored
    assert same_address("https://EXAMPLE.fi/a", f"{BASE}/a")   # host case ignored
    assert same_address(f"{BASE}/a#top", f"{BASE}/a")          # fragment ignored
    assert not same_address(f"{BASE}/A", f"{BASE}/a")          # path case matters
    assert not same_address("http://example.fi/a", f"{BASE}/a")  # http -> https is a real redirect
    assert not same_address(f"{BASE}/a?x=1", f"{BASE}/a?x=2")


def test_unicode_domain_matches_its_punycode_form():
    from checker.urls import site_host
    idn = f"https://k{A_UML}rkk{A_UML}inen.fi/"
    sent = requests.Request("GET", idn).prepare().url
    assert "xn--" in sent and site_host(idn) == site_host(sent)


def test_robots_group_for_seo_checker_is_obeyed(site):
    site[f"{BASE}/robots.txt"] = Resp("User-agent: seo-checker\nDisallow: /private\n", status=200)
    c = Crawler(BASE, delay=0)
    assert not c.allowed(f"{BASE}/private/x")
    assert c.allowed(f"{BASE}/public")


@pytest.mark.parametrize("content,noindex", [
    ("none", True), ("noindex, nofollow", True), ("NOINDEX", True),
    ("nofollow", False), ("index, follow", False), ("nonexistent", False),
])
def test_meta_robots_none_means_noindex(content, noindex):
    from checker.pages import check_page
    result = check_page(f'<title>t</title><meta name="robots" content="{content}">', f"{BASE}/")
    assert result["noindex"] is noindex


def test_internal_links_dedupes_fast_and_keeps_order():
    import time
    from checker.pages import internal_links
    html = "".join(f'<a href="/p{i % 5000}">x</a>' for i in range(20000))
    start = time.perf_counter()
    links = internal_links(html, BASE + "/")
    assert links[:3] == [f"{BASE}/p0", f"{BASE}/p1", f"{BASE}/p2"] and len(links) == 5000
    assert time.perf_counter() - start < 10  # was quadratic: hundreds of millions of comparisons


# ---------- remaining low-priority findings ----------

def test_robots_5xx_warns_and_allows_everything(site, capsys):
    site[f"{BASE}/robots.txt"] = Resp("User-agent: *\nDisallow: /\n", status=503)
    c = Crawler(BASE, delay=0)
    out = capsys.readouterr().out
    assert "robots.txt returned 503" in out and "server error" in out
    assert c.allowed(f"{BASE}/anything")


def sitemap_xml(*urls):
    return ('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>")


def index_xml(*urls):
    return ('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            + "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls) + "</sitemapindex>")


def test_overlapping_child_sitemaps_still_reach_max_unique_pages(site):
    site[f"{BASE}/sitemap_index.xml"] = Resp(index_xml(f"{BASE}/a.xml", f"{BASE}/b.xml", f"{BASE}/c.xml"))
    site[f"{BASE}/a.xml"] = Resp(sitemap_xml(f"{BASE}/p1", f"{BASE}/p2", f"{BASE}/p3"))
    site[f"{BASE}/b.xml"] = Resp(sitemap_xml(f"{BASE}/p1", f"{BASE}/p2", f"{BASE}/p3"))  # same pages
    site[f"{BASE}/c.xml"] = Resp(sitemap_xml(f"{BASE}/p4"))
    assert Crawler(BASE, delay=0).discover_urls(5) == [f"{BASE}/p1", f"{BASE}/p2", f"{BASE}/p3", f"{BASE}/p4"]


class FakeLinkCrawler:
    def __init__(self, status):
        self.status, self.requested = status, []

    def allowed(self, url):
        return True

    def link_status(self, url):
        self.requested.append(url)
        return self.status


def test_broken_link_with_and_without_slash_is_listed_once():
    crawler = FakeLinkCrawler(404)
    page = f"{BASE}/page"
    broken, checked, capped = find_broken_links(
        {page: [f"{BASE}/x", f"{BASE}/x/", f"{BASE}/x/index.html"]}, {}, crawler, 10)
    assert broken == {page: [f"{BASE}/x"]} and checked == 1 and not capped


def test_same_broken_target_is_listed_on_every_page_that_links_it():
    crawler = FakeLinkCrawler(404)
    broken, checked, _ = find_broken_links(
        {f"{BASE}/a": [f"{BASE}/x"], f"{BASE}/b": [f"{BASE}/x/"]}, {}, crawler, 10)
    assert set(broken) == {f"{BASE}/a", f"{BASE}/b"} and checked == 1


def test_excel_safe_truncates_over_cell_limit():
    from checker.report import EXCEL_CELL_MAX, TRUNCATED, excel_safe
    assert excel_safe("x" * EXCEL_CELL_MAX) == "x" * EXCEL_CELL_MAX  # exactly at the limit: kept
    long = excel_safe("x" * (EXCEL_CELL_MAX + 5000))
    assert len(long) == EXCEL_CELL_MAX and long.endswith(TRUNCATED)


def test_xlsx_with_huge_broken_links_cell_stays_within_excel_limit(tmp_path):
    from openpyxl import load_workbook
    from checker.pages import check_page
    from checker.report import CSV_FIELDS, EXCEL_CELL_MAX, write_xlsx
    result = check_page("<title>t</title>", f"{BASE}/")
    result["broken_links"] = "; ".join(f"{BASE}/missing-page-{i}" for i in range(3000))  # ~110k chars
    path = tmp_path / "r.xlsx"
    write_xlsx([result], path)
    ws = load_workbook(path).active
    cell = ws.cell(row=2, column=CSV_FIELDS.index("broken_links") + 1).value
    assert len(cell) == EXCEL_CELL_MAX


@pytest.mark.parametrize("delay", ["inf", "-inf", "nan", "1e999"])
def test_main_rejects_non_finite_delay(site, monkeypatch, capsys, delay):
    monkeypatch.setattr(sys, "argv", ["seo_checker.py", BASE, "--delay", delay])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 2 and "--delay" in capsys.readouterr().err
    assert site.requested == []


def test_report_path_problem_rejects_a_folder(tmp_path):
    problem = seo_checker.report_path_problem(str(tmp_path))
    assert problem and "is a folder" in problem


def test_report_path_problem_reports_unwritable_file_and_leaves_no_probe(tmp_path, monkeypatch):
    target = tmp_path / "r.csv"
    assert seo_checker.report_path_problem(str(target)) is None
    assert not target.exists()  # the write test removed its probe file

    def locked(path, mode="r", *args, **kwargs):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(seo_checker, "open", locked, raising=False)
    problem = seo_checker.report_path_problem(str(target))
    assert problem and "can't write" in problem and "Permission denied" in problem


def test_failed_csv_write_still_writes_xlsx_and_exits_1(site, monkeypatch, tmp_path, capsys):
    site[f"{BASE}/"] = Resp(HTML % "")

    def broken_write(results, path, delimiter=","):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(seo_checker, "write_csv", broken_write)
    with pytest.raises(SystemExit) as e:
        run_main(monkeypatch, "--csv", str(tmp_path / "r.csv"), "--xlsx", str(tmp_path / "r.xlsx"))
    assert e.value.code == 1
    captured = capsys.readouterr()
    assert "could not write the CSV report" in captured.err and "No space left" in captured.err
    assert "Excel report written" in captured.out and (tmp_path / "r.xlsx").exists()


@pytest.mark.parametrize("option", ["--csv", "--xlsx"])
def test_main_rejects_a_folder_as_report_path(site, monkeypatch, tmp_path, capsys, option):
    monkeypatch.setattr(sys, "argv", ["seo_checker.py", BASE, "--delay", "0", option, str(tmp_path)])
    with pytest.raises(SystemExit) as e:
        main()
    assert e.value.code == 2 and "not a folder" in capsys.readouterr().err
    assert site.requested == [] and list(tmp_path.iterdir()) == []  # nothing crawled or written

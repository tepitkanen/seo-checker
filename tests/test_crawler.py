"""Tests for the Crawler class, print_summary, main() and extra check_page edge cases.

No real network: requests.Session.get is replaced by a fake that serves pages from a dict.
"""
import csv
import os
import sys
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import requests
from openpyxl import load_workbook

import seo_checker
from seo_checker import Crawler, check_page, link_key, main, print_summary

BASE = "https://example.fi"


class FakeResponse:
    def __init__(self, text="", status=200, content_type="text/plain", content=None, url="",
                 headers=None, elapsed_ms=0):
        self.text = text
        self.url = url  # final URL after redirects; "" means "same as requested"
        self.content = text.encode("utf-8") if content is None else content
        self.status_code = status
        self.ok = status < 400
        self.headers = {"Content-Type": content_type, **(headers or {})}
        self.elapsed = timedelta(milliseconds=elapsed_ms)

    def iter_content(self, chunk_size=1):
        """Like requests' streaming read, used by Crawler.get."""
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i:i + chunk_size]

    def close(self):
        pass


class FakeSite:
    """Maps URL -> FakeResponse (or an exception to raise). Unknown URLs return 404."""

    def __init__(self, pages):
        self.pages = pages
        self.requested = []

    def get(self, url):
        self.requested.append(url)
        page = self.pages.get(url, FakeResponse(status=404))
        if isinstance(page, Exception):
            raise page
        return page


@pytest.fixture
def make_site(monkeypatch):
    """Return a function that installs a fake site and returns it."""
    def install(pages):
        site = FakeSite(pages)
        # A plain function is needed here so Python passes the session as the first argument.
        monkeypatch.setattr(requests.Session, "get", lambda session, url, **kwargs: site.get(url))
        return site
    return install


def sitemap(*urls):
    locs = "".join(f"<url><loc>{u}</loc></url>" for u in urls)
    return f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</urlset>'


def sitemap_index(*urls):
    locs = "".join(f"<sitemap><loc>{u}</loc></sitemap>" for u in urls)
    return f'<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{locs}</sitemapindex>'


# ---------- Crawler.get / robots.txt ----------

def test_get_returns_none_on_network_error(make_site):
    make_site({f"{BASE}/robots.txt": requests.ConnectionError("down"),
               f"{BASE}/x": requests.Timeout("slow")})
    crawler = Crawler(BASE, delay=0)
    assert crawler.get(f"{BASE}/x") is None


def test_base_url_trailing_slash_is_removed(make_site):
    make_site({})
    assert Crawler(BASE + "/", delay=0).base == BASE


def test_robots_disallow_is_respected(make_site):
    make_site({f"{BASE}/robots.txt": FakeResponse("User-agent: *\nDisallow: /private\n")})
    crawler = Crawler(BASE, delay=0)
    assert not crawler.allowed(f"{BASE}/private/page")
    assert crawler.allowed(f"{BASE}/public")


def test_missing_robots_txt_allows_everything(make_site):
    make_site({})  # robots.txt -> 404
    assert Crawler(BASE, delay=0).allowed(f"{BASE}/anything")


def test_unreachable_robots_txt_allows_everything(make_site):
    make_site({f"{BASE}/robots.txt": requests.ConnectionError("down")})
    assert Crawler(BASE, delay=0).allowed(f"{BASE}/anything")


# ---------- discover_urls ----------

def test_discover_uses_sitemap_xml(make_site):
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a", f"{BASE}/b"))})
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a", f"{BASE}/b"]


def test_discover_expands_sitemap_index(make_site):
    make_site({
        f"{BASE}/sitemap_index.xml": FakeResponse(sitemap_index(f"{BASE}/s1.xml", f"{BASE}/s2.xml")),
        f"{BASE}/s1.xml": FakeResponse(sitemap(f"{BASE}/a")),
        f"{BASE}/s2.xml": FakeResponse(sitemap(f"{BASE}/b", f"{BASE}/c")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a", f"{BASE}/b", f"{BASE}/c"]


def test_discover_skips_failing_child_sitemap(make_site):
    make_site({
        f"{BASE}/sitemap_index.xml": FakeResponse(sitemap_index(f"{BASE}/s1.xml", f"{BASE}/s2.xml")),
        f"{BASE}/s2.xml": FakeResponse(sitemap(f"{BASE}/b")),  # s1.xml -> 404
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/b"]


def test_discover_stops_fetching_child_sitemaps_at_max_pages(make_site):
    site = make_site({
        f"{BASE}/sitemap_index.xml": FakeResponse(sitemap_index(f"{BASE}/s1.xml", f"{BASE}/s2.xml")),
        f"{BASE}/s1.xml": FakeResponse(sitemap(f"{BASE}/a", f"{BASE}/b")),
        f"{BASE}/s2.xml": FakeResponse(sitemap(f"{BASE}/c")),
    })
    urls = Crawler(BASE, delay=0).discover_urls(2)
    assert urls == [f"{BASE}/a", f"{BASE}/b"]
    assert f"{BASE}/s2.xml" not in site.requested


def test_discover_respects_max_pages_for_plain_sitemap(make_site):
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap(*[f"{BASE}/p{i}" for i in range(10)]))})
    assert len(Crawler(BASE, delay=0).discover_urls(3)) == 3


def test_discover_broken_xml_falls_back_to_next_source(make_site):
    make_site({
        f"{BASE}/sitemap_index.xml": FakeResponse("<urlset><url><loc>oops"),  # not valid XML
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a"]


def test_discover_falls_back_to_homepage_links(make_site):
    html = '<a href="/about">About</a><a href="https://other.com/x">Ext</a><a href="/doc.pdf">PDF</a>'
    make_site({f"{BASE}/": FakeResponse(html, content_type="text/html")})
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/", f"{BASE}/about"]


def test_discover_homepage_fallback_does_not_duplicate_homepage(make_site):
    make_site({f"{BASE}/": FakeResponse('<a href="/">Home</a><a href="/a">A</a>')})
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/", f"{BASE}/a"]


def test_discover_homepage_fallback_skips_index_html_duplicates(make_site):
    html = ('<a href="/index.html">Home</a><a href="/a/">A</a>'
            '<a href="/a/index.htm">A again</a><a href="/myindex.html">Other</a>')
    make_site({f"{BASE}/": FakeResponse(html)})
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/", f"{BASE}/a/", f"{BASE}/myindex.html"]


@pytest.mark.parametrize("a, b", [
    ("https://example.fi/", "https://example.fi/index.html"),
    ("https://example.fi/a/", "http://www.example.fi/a/INDEX.HTM"),
    ("https://example.fi/a?x=1", "https://example.fi/a/index.html?x=1"),
])
def test_link_key_treats_index_file_as_same_page(a, b):
    assert link_key(a) == link_key(b)


def test_link_key_keeps_other_files_and_queries_apart():
    assert link_key("https://example.fi/myindex.html") != link_key("https://example.fi/")
    assert link_key("https://example.fi/a?x=1") != link_key("https://example.fi/a?x=2")


def test_discover_homepage_fallback_respects_max_pages(make_site):
    links = "".join(f'<a href="/p{i}">x</a>' for i in range(10))
    make_site({f"{BASE}/": FakeResponse(links)})
    assert len(Crawler(BASE, delay=0).discover_urls(4)) == 4


def test_discover_when_everything_is_down_returns_homepage_only(make_site):
    make_site({})
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/"]


def test_discover_empty_sitemap_falls_back_to_homepage(make_site):
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap()),
               f"{BASE}/": FakeResponse('<a href="/a">A</a>')})
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/", f"{BASE}/a"]


# ---------- fetch_pages ----------

def test_fetch_pages_skips_robots_disallowed_urls(make_site, capsys):
    make_site({
        f"{BASE}/robots.txt": FakeResponse("User-agent: *\nDisallow: /secret\n"),
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/ok", f"{BASE}/secret")),
        f"{BASE}/ok": FakeResponse("<html>ok</html>"),
        f"{BASE}/secret": FakeResponse("hidden"),
    })
    pages = list(Crawler(BASE, delay=0).fetch_pages(10))
    assert [p[:4] for p in pages] == [(f"{BASE}/ok", 200, "<html>ok</html>", f"{BASE}/ok")]
    assert "skipped (robots.txt)" in capsys.readouterr().out


def test_fetch_pages_does_not_request_disallowed_url(make_site):
    site = make_site({
        f"{BASE}/robots.txt": FakeResponse("User-agent: *\nDisallow: /secret\n"),
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/secret")),
    })
    assert list(Crawler(BASE, delay=0).fetch_pages(10)) == []
    assert f"{BASE}/secret" not in site.requested


def test_fetch_pages_yields_status_0_on_network_error(make_site):
    make_site({
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a")),
        f"{BASE}/a": requests.ConnectionError("boom"),
    })
    assert list(Crawler(BASE, delay=0).fetch_pages(10)) == [(f"{BASE}/a", 0, "", f"{BASE}/a", {})]


def test_fetch_pages_passes_through_http_error_status(make_site):
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/gone"))})  # /gone -> 404
    [page] = list(Crawler(BASE, delay=0).fetch_pages(10))
    assert page[:4] == (f"{BASE}/gone", 404, "", f"{BASE}/gone")


def test_fetch_pages_reports_final_url_after_redirect(make_site):
    make_site({
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/old")),
        f"{BASE}/old": FakeResponse("<html></html>", url=f"{BASE}/new/"),
    })
    [(url, _, _, final_url, _)] = list(Crawler(BASE, delay=0).fetch_pages(10))
    assert (url, final_url) == (f"{BASE}/old", f"{BASE}/new/")


def test_canonical_is_compared_to_final_url():
    html = '<link rel="canonical" href="https://example.fi/new/">'
    result = check_page(html, "https://example.fi/old", final_url="https://example.fi/new/")
    assert "Canonical points elsewhere" not in result["issues"]
    assert result["url"] == "https://example.fi/old"  # the report still shows the sitemap URL


def test_redirect_is_reported():
    result = check_page("<html></html>", "https://example.fi/old", final_url="https://example.fi/new/")
    assert result["redirects_to"] == "https://example.fi/new/"
    assert "Redirects to another URL" in result["issues"]


def test_no_redirect_issue_without_redirect():
    # requests turns "https://example.fi" into "https://example.fi/"; that is not a redirect.
    for final in [None, "https://example.fi/", "https://example.fi"]:
        result = check_page("<html></html>", "https://example.fi", final_url=final)
        assert result["redirects_to"] == ""
        assert "Redirects to another URL" not in result["issues"]


def test_redirect_to_error_page_reports_both():
    result = check_page("", "https://example.fi/old", status=404, final_url="https://example.fi/gone")
    assert result["issues"] == ["Redirects to another URL", "HTTP 404"]


def test_relative_canonical_resolves_against_final_url():
    result = check_page('<link rel="canonical" href="page">', "https://example.fi/old/x",
                        final_url="https://example.fi/new/page")
    assert result["canonical"] == "https://example.fi/new/page"
    assert "Canonical points elsewhere" not in result["issues"]


# ---------- has_llms_txt ----------

def test_has_llms_txt_true_for_plain_text(make_site):
    make_site({f"{BASE}/llms.txt": FakeResponse("# Site", content_type="text/plain; charset=utf-8")})
    assert Crawler(BASE, delay=0).has_llms_txt() is True


def test_has_llms_txt_rejects_html_response(make_site):
    # Many sites serve their HTML 404 page with status 200 ("soft 404").
    make_site({f"{BASE}/llms.txt": FakeResponse("<html>Not found</html>", content_type="text/html; charset=utf-8")})
    assert Crawler(BASE, delay=0).has_llms_txt() is False


def test_has_llms_txt_false_on_404_and_on_network_error(make_site):
    make_site({})
    assert Crawler(BASE, delay=0).has_llms_txt() is False
    make_site({f"{BASE}/llms.txt": requests.ConnectionError("down")})
    assert Crawler(BASE, delay=0).has_llms_txt() is False


# ---------- print_summary ----------

def test_print_summary_counts_and_sorts_issues(capsys):
    results = [{"issues": ["Missing H1", "Missing title"]}, {"issues": ["Missing H1"]}, {"issues": []}]
    print_summary(results, llms_txt=True)
    out = capsys.readouterr().out
    assert "Pages checked: 3" in out
    assert "Pages with issues: 2" in out
    assert "llms.txt: found" in out
    assert out.index("Missing H1") < out.index("Missing title")  # most common first
    assert "2  Missing H1" in out


def test_print_summary_without_issues(capsys):
    print_summary([{"issues": []}], llms_txt=False)
    out = capsys.readouterr().out
    assert "llms.txt: missing" in out
    assert "Issues by type" not in out


def test_print_summary_with_no_pages(capsys):
    print_summary([], llms_txt=False)
    out = capsys.readouterr().out
    assert "Pages checked: 0" in out
    assert "Pages with issues: 0" in out


# ---------- main() end to end ----------

PAGE_HTML = "<html><head><title>Hi</title></head><body></body></html>"


def run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["seo_checker.py", *args])
    # Keep report names unchanged so tests can open the file they asked for.
    monkeypatch.setattr(seo_checker, "add_timestamp", lambda path, stamp: path)
    main()


@pytest.fixture
def site(make_site):
    return make_site({
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a", f"{BASE}/b")),
        f"{BASE}/a": FakeResponse(PAGE_HTML, content_type="text/html"),
        f"{BASE}/b": FakeResponse("", status=500),
    })


def test_main_prints_summary(site, monkeypatch, capsys):
    run_main(monkeypatch, "example.fi", "--delay", "0")  # domain only -> https:// added
    out = capsys.readouterr().out
    assert f"[200] {BASE}/a" in out
    assert f"[500] {BASE}/b" in out
    assert "Pages checked: 2" in out
    assert "llms.txt: missing" in out


def test_main_max_limits_pages(site, monkeypatch, capsys):
    run_main(monkeypatch, BASE, "--delay", "0", "--max", "1")
    assert "Pages checked: 1" in capsys.readouterr().out


def test_main_writes_csv_with_semicolon(site, monkeypatch, tmp_path):
    out_file = tmp_path / "report.csv"
    run_main(monkeypatch, BASE, "--delay", "0", "--csv", str(out_file), "--csv-delimiter", "semicolon")
    with open(out_file, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert [r["url"] for r in rows] == [f"{BASE}/a", f"{BASE}/b"]
    assert rows[1]["issues"] == "HTTP 500"


def test_main_writes_xlsx(site, monkeypatch, tmp_path):
    out_file = tmp_path / "report.xlsx"
    run_main(monkeypatch, BASE, "--delay", "0", "--xlsx", str(out_file))
    ws = load_workbook(out_file)["SEO report"]
    assert ws.max_row == 3  # header + 2 pages
    assert ws["A2"].value == f"{BASE}/a"


@pytest.mark.parametrize("args", [
    [],                                  # missing url
    [BASE, "--max", "0"],                # not positive
    [BASE, "--max", "abc"],              # not a number
    [BASE, "--csv-delimiter", "tab"],    # invalid choice
    [BASE, "--delay", "x"],              # not a float
])
def test_main_argument_errors_exit_with_code_2(args, monkeypatch, make_site, capsys):
    make_site({})
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, *args)
    assert exc.value.code == 2


def test_main_bad_report_folder_fails_before_crawling(monkeypatch, make_site, tmp_path, capsys):
    fake = make_site({})
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, BASE, "--delay", "0", "--csv", str(tmp_path / "nope" / "r.csv"))
    assert exc.value.code == 2
    assert "folder does not exist" in capsys.readouterr().err
    assert fake.requested == []  # no request was made


@pytest.mark.parametrize("delay", ["-1", "nan"])
def test_main_invalid_delay_is_rejected(delay, monkeypatch, make_site):
    # time.sleep() would raise ValueError; the CLI should reject it with a clean error.
    make_site({})
    with pytest.raises(SystemExit) as exc:
        run_main(monkeypatch, BASE, "--delay", delay)
    assert exc.value.code == 2


# ---------- check_page edge cases ----------

def test_meta_names_are_case_insensitive():
    html = ('<meta name="Description" content="x"><meta name="ROBOTS" content="NoIndex, follow">')
    result = check_page(html, "https://example.fi/")
    assert result["description_length"] == 1
    assert result["noindex"] is True


def test_schema_type_list_is_collected():
    html = '<script type="application/ld+json">{"@type": ["Organization", "LocalBusiness"]}</script>'
    assert check_page(html, "https://example.fi/")["schema_types"] == "LocalBusiness, Organization"


def test_invalid_json_ld_is_ignored():
    html = '<script type="application/ld+json">{not json</script>'
    assert "No structured data" in check_page(html, "https://example.fi/")["issues"]


def test_relative_canonical_is_resolved():
    url = "https://example.fi/page"
    result = check_page('<link rel="canonical" href="/page">', url)
    assert result["canonical"] == url
    assert "Canonical points elsewhere" not in result["issues"]
    assert "Missing canonical" not in result["issues"]


def test_canonical_trailing_slash_difference_is_ignored():
    result = check_page('<link rel="canonical" href="https://example.fi/page/">', "https://example.fi/page")
    assert "Canonical points elsewhere" not in result["issues"]


def test_canonical_to_other_page_is_flagged():
    result = check_page('<link rel="canonical" href="/other">', "https://example.fi/page")
    assert "Canonical points elsewhere" in result["issues"]


def test_whitespace_only_alt_counts_as_empty():
    html = '<img src="a.jpg" alt="   "><img src="b.jpg" alt="ok"><img src="c.jpg">'
    result = check_page(html, "https://example.fi/")
    assert result["images_missing_alt"] == 1  # c.jpg
    assert result["images_empty_alt"] == 1    # a.jpg


def test_only_empty_alt_gives_only_the_softer_issue():
    result = check_page('<img src="deco.png" alt="">', "https://example.fi/")
    assert "Images with empty alt" in result["issues"]
    assert "Images missing alt" not in result["issues"]


def test_non_200_page_only_reports_http_status():
    result = check_page("<html></html>", "https://example.fi/x", status=404)
    assert result["issues"] == ["HTTP 404"]


def test_status_0_reports_request_failed():
    assert check_page("", "https://example.fi/x", status=0)["issues"] == ["Request failed"]


# ---------- fixes: encoding, sitemap host, Excel safety ----------

def test_fetch_pages_uses_meta_charset_when_header_has_none(make_site):
    body = '<meta charset="utf-8"><title>Kengät</title>'.encode("utf-8")
    make_site({
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a")),
        # requests would decode this as ISO-8859-1, giving "KengÃ¤t" in .text
        f"{BASE}/a": FakeResponse(body.decode("latin-1"), content_type="text/html", content=body),
    })
    [(_, _, html, _, _)] = list(Crawler(BASE, delay=0).fetch_pages(10))
    assert check_page(html, f"{BASE}/a")["title"] == "Kengät"


def test_fetch_pages_trusts_charset_in_header(make_site):
    make_site({
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a")),
        f"{BASE}/a": FakeResponse("<title>Kengät</title>", content_type="text/html; charset=utf-8",
                                  content=b"ignored"),
    })
    [(_, _, html, _, _)] = list(Crawler(BASE, delay=0).fetch_pages(10))
    assert "Kengät" in html


def test_sitemap_urls_on_other_hosts_are_ignored(make_site):
    site = make_site({
        f"{BASE}/sitemap_index.xml": FakeResponse(sitemap_index(f"{BASE}/s1.xml", "https://evil.example/s.xml")),
        f"{BASE}/s1.xml": FakeResponse(sitemap(f"{BASE}/a", "https://other.com/b", "https://www.example.fi/c")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a", "https://www.example.fi/c"]
    assert "https://evil.example/s.xml" not in site.requested


def test_xlsx_keeps_formula_like_titles_as_text_and_strips_control_chars(tmp_path):
    from seo_checker import write_xlsx
    result = check_page("<title>=HYPERLINK(\"http://x\")\x0b</title>", "https://example.fi/")
    path = tmp_path / "r.xlsx"
    write_xlsx([result], path)
    cell = load_workbook(path)["SEO report"]["C2"]
    assert cell.value == '=HYPERLINK("http://x")'
    assert cell.data_type == "s"

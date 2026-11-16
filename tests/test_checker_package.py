"""Tests for the checker package, imported directly from checker.* (not via seo_checker).

Covers edge cases the other test files do not: URL key variants, JSON-LD oddities, multi-valued
attributes, link filtering, gzip limits, broken-link capping and spreadsheet-safe values.
Offline and fast; no real network.
"""
import csv
import gzip
import os
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from bs4 import BeautifulSoup
from openpyxl import load_workbook

import checker.crawl
import checker.pages
import checker.report
import checker.urls
import xml.etree.ElementTree as ET

from checker.crawl import _gunzip_capped, find_broken_links, parse_sitemap, sitemap_text
from checker.pages import (check_page, find_meta, find_schema_types, get_attr, internal_links,
                           mark_duplicates)
from checker.report import csv_safe, excel_safe, write_csv, write_xlsx
from checker.urls import NON_PAGE_EXTENSIONS, link_key, printable, site_host


def soup_of(html):
    return BeautifulSoup(html, "html.parser")


# ---------- urls ----------

@pytest.mark.parametrize("url,expected", [
    ("https://www.example.fi/a", "example.fi"),
    ("https://WWW.Example.FI/a", "example.fi"),
    ("http://example.fi:8080/", "example.fi:8080"),
    ("https://www.www.example.fi/", "www.example.fi"),  # only one prefix is removed
    ("https://wwwexample.fi/", "wwwexample.fi"),
    ("https://sub.www.example.fi/", "sub.www.example.fi"),
    ("", ""),
    # default ports and user info are not part of the site
    ("https://example.fi:443/a", "example.fi"),
    ("http://www.example.fi:80/", "example.fi"),
    ("https://user:pw@example.fi/", "example.fi"),
    ("http://example.fi:443/", "example.fi:443"),  # 443 is not http's default port
    ("http://[::1]:8080/", "[::1]:8080"),
])
def test_site_host_variants(url, expected):
    assert site_host(url) == expected


def test_default_port_is_same_page_for_link_key():
    assert link_key("https://example.fi:443/a/") == link_key("https://example.fi/a")


@pytest.mark.parametrize("text,expected", [
    ("https://example.fi/a?b=1", "https://example.fi/a?b=1"),
    ("https://example.fi/\x1b[2J", "https://example.fi/\\x1b[2J"),   # ANSI "clear screen"
    ("https://example.fi/a\nfake line", "https://example.fi/a\\x0afake line"),
    ("https://example.fi/\u202egnp.exe", "https://example.fi/\\u202egnp.exe"),  # bidi override
    ("https://example.fi/kengät", "https://example.fi/kengät"),  # normal non-ASCII is kept
])
def test_printable_escapes_control_characters(text, expected):
    assert printable(text) == expected


@pytest.mark.parametrize("prefix", [
    '<!DOCTYPE urlset [<!ENTITY a "x">]>',
    '<!doctype urlset>',
    '<!ENTITY a "x">',
])
def test_parse_sitemap_refuses_dtd(prefix):
    xml = prefix + '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>https://example.fi/a</loc></url></urlset>'
    with pytest.raises(ET.ParseError):
        parse_sitemap(xml)


@pytest.mark.parametrize("a,b", [
    ("http://www.example.fi/a/", "https://example.fi/a"),
    ("https://example.fi/a/index.html", "https://example.fi/a/"),
    ("https://example.fi/a/INDEX.HTM", "https://example.fi/a"),
    ("https://example.fi/index.html", "https://example.fi"),
    ("https://example.fi/", "https://example.fi"),
    ("https://example.fi/a?x=1", "http://www.example.fi/a/index.html?x=1"),
    ("https://example.fi/a#top", "https://example.fi/a"),
    ("https://example.fi/a?", "https://example.fi/a"),
])
def test_link_key_same_page(a, b):
    assert link_key(a) == link_key(b)


@pytest.mark.parametrize("a,b", [
    ("https://example.fi/a?x=1", "https://example.fi/a?x=2"),
    ("https://example.fi/a?x=1", "https://example.fi/a"),
    ("https://example.fi/a", "https://example.fi/b"),
    ("https://example.fi/myindex.html", "https://example.fi/my"),
    ("https://example.fi/a", "https://other.fi/a"),
    ("https://example.fi/A", "https://example.fi/a"),  # paths are case-sensitive
])
def test_link_key_different_pages(a, b):
    assert link_key(a) != link_key(b)


def test_non_page_extensions_is_tuple_of_lowercase_dotted():
    assert isinstance(NON_PAGE_EXTENSIONS, tuple)
    assert all(e.startswith(".") and e == e.lower() for e in NON_PAGE_EXTENSIONS)


# ---------- pages: get_attr / find_meta ----------

def test_get_attr_joins_multivalued_attribute():
    tag = soup_of('<a rel="nofollow  noopener" class="x y" href=" /p ">t</a>').a
    assert get_attr(tag, "rel") == "nofollow noopener"
    assert get_attr(tag, "class") == "x y"
    assert get_attr(tag, "href") == "/p"


def test_get_attr_missing_none_and_empty():
    tag = soup_of('<a href="">t</a>').a
    assert get_attr(tag, "href") == ""
    assert get_attr(tag, "nope") == ""
    assert get_attr(None, "href") == ""


def test_get_attr_empty_class():
    assert get_attr(soup_of('<p class="">x</p>').p, "class") == ""


def test_find_meta_case_insensitive_name_first_match_and_missing():
    soup = soup_of('<meta name="Description" content="first"><meta name="description" content="second">'
                   '<meta property="og:title" content="x"><meta name="">')
    assert get_attr(find_meta(soup, "description"), "content") == "first"
    assert find_meta(soup, "robots") is None
    assert find_meta(soup_of("<p>none</p>"), "description") is None


# ---------- pages: find_schema_types ----------

def ld(*blocks):
    return soup_of("".join('<script type="application/ld+json">%s</script>' % b for b in blocks))


def test_schema_graph_list_only_descends_into_graph():
    soup = ld('{"@context":"https://schema.org","@graph":[{"@type":"Organization"},'
              '{"@type":["WebSite","Thing"],"publisher":{"@type":"Person"}}]}')
    assert find_schema_types(soup) == {"Organization", "WebSite", "Thing"}


def test_schema_graph_as_dict_and_nested_graph():
    soup = ld('{"@graph":{"@type":"Article","@graph":[{"@type":"Product"}]}}')
    assert find_schema_types(soup) == {"Article", "Product"}


def test_schema_top_level_list():
    assert find_schema_types(ld('[{"@type":"A"},{"@type":"B"},[{"@type":"C"}]]')) == {"A", "B", "C"}


@pytest.mark.parametrize("block", [
    "", "   ", "{not json", "null", "42", '"string"', "[]", "{}", '{"@type": 5}', '{"@type": null}',
    '{"@type": {"x": 1}}', '{"@graph": null}', '{"@graph": 7}', '{"@graph": "x"}', '[1, null, "a"]',
])
def test_schema_odd_json_gives_no_types(block):
    assert find_schema_types(ld(block)) == set()


def test_schema_mixed_type_list_keeps_only_strings():
    assert find_schema_types(ld('{"@type":["A",1,null,{"x":1},"B"]}')) == {"A", "B"}


def test_schema_bad_block_does_not_hide_good_one():
    assert find_schema_types(ld("{broken", '{"@type":"FAQPage"}')) == {"FAQPage"}


def test_schema_ignores_other_script_types_and_empty_script_tag():
    soup = soup_of('<script type="application/json">{"@type":"X"}</script>'
                   '<script type="application/ld+json"></script>')
    assert find_schema_types(soup) == set()


# ---------- pages: internal_links ----------

def test_internal_links_filtering_and_order():
    html = """
    <a href="/a">1</a><a href="/a#frag">dup</a><a href="/a">dup2</a>
    <a href="https://www.example.fi/b">www</a><a href="HTTPS://EXAMPLE.FI/c">upper</a>
    <a href="https://other.fi/x">ext</a><a href="/doc.PDF">pdf</a><a href="/img.webp#x">img</a>
    <a href="#top">frag only</a><a>no href</a><a href="  /spaced  ">sp</a>
    <a href="rel/page">relative</a><a href="mailto:a@example.fi">m</a><a href="//example.fi/proto">pr</a>
    <a href="/page.pdf.html">looks like pdf</a><a href="/q?file=x.pdf">query pdf</a>
    """
    links = internal_links(html, "https://example.fi/dir/index.html")
    assert links == [
        "https://example.fi/a",
        "https://www.example.fi/b",
        "https://EXAMPLE.FI/c",  # urljoin keeps an absolute URL as written; host matching is case-blind
        "https://example.fi/dir/index.html",  # fragment-only href resolves to the page itself
        "https://example.fi/spaced",
        "https://example.fi/dir/rel/page",
        "https://example.fi/proto",
        "https://example.fi/page.pdf.html",
        "https://example.fi/q?file=x.pdf",
    ]


def test_internal_links_simple():
    html = '<a href="/a">1</a><a href="/a#x">2</a><a href="/b.zip">3</a><a href="http://x.fi/">4</a>'
    assert internal_links(html, "https://example.fi/") == ["https://example.fi/a"]


def test_internal_links_empty_html():
    assert internal_links("", "https://example.fi/") == []


def test_internal_links_other_scheme_same_host_is_internal():
    assert internal_links('<a href="http://example.fi/p">x</a>', "https://example.fi/") == ["http://example.fi/p"]


# ---------- pages: mark_duplicates ----------

def test_mark_duplicates_redirect_to_same_page_not_duplicate():
    a = {"url": "https://example.fi/a", "redirects_to": "", "title": "Same", "description": "", "issues": []}
    b = {"url": "https://example.fi/old", "redirects_to": "https://www.example.fi/a/", "title": "same ",
         "description": "", "issues": []}
    mark_duplicates([a, b])
    assert a["issues"] == [] and b["issues"] == []


def test_mark_duplicates_flags_both_and_ignores_empty():
    rs = [{"url": "https://e.fi/%d" % i, "title": t, "description": "", "issues": []}
          for i, t in enumerate(["Hello   World", "hello world", "", ""])]
    mark_duplicates(rs)
    assert rs[0]["issues"] == ["Duplicate title"] == rs[1]["issues"]
    assert rs[2]["issues"] == [] and rs[3]["issues"] == []


def test_mark_duplicates_empty_list():
    mark_duplicates([])


# ---------- crawl: _gunzip_capped / sitemap_text ----------

def test_gunzip_multi_member():
    data = gzip.compress(b"hello ") + gzip.compress(b"world")
    assert _gunzip_capped(data, 100) == b"hello world"


def test_gunzip_trailing_zero_padding_ignored():
    assert _gunzip_capped(gzip.compress(b"abc") + b"\x00" * 7, 100) == b"abc"


def test_gunzip_limit_boundary():
    data = gzip.compress(b"x" * 100)
    assert _gunzip_capped(data, 100) == b"x" * 100
    with pytest.raises(ValueError):
        _gunzip_capped(data, 99)


def test_gunzip_limit_applies_across_members():
    data = gzip.compress(b"x" * 60) + gzip.compress(b"y" * 60)
    assert len(_gunzip_capped(data, 120)) == 120
    with pytest.raises(ValueError):
        _gunzip_capped(data, 119)


def test_gunzip_bomb_stops_with_value_error():
    bomb = gzip.compress(b"\0" * (20 * 1024 * 1024))
    with pytest.raises(ValueError):
        _gunzip_capped(bomb, 1024)


def test_gunzip_truncated_raises_eoferror():
    data = gzip.compress(b"some reasonably long content " * 20)
    with pytest.raises(EOFError):
        _gunzip_capped(data[:-10], 10000)


def test_gunzip_garbage_after_member_raises_zlib_error():
    with pytest.raises(zlib.error):
        _gunzip_capped(gzip.compress(b"abc") + b"junk-not-gzip", 100)


def test_gunzip_empty_input():
    assert _gunzip_capped(b"", 10) == b""


class Resp:
    def __init__(self, content, text=""):
        self.content, self.text = content, text


def test_sitemap_text_plain_gzip_and_broken():
    assert sitemap_text(Resp(b"<x/>", "<x/>")) == "<x/>"
    assert sitemap_text(Resp(gzip.compress("<urlset>ä</urlset>".encode("utf-8")))) == "<urlset>ä</urlset>"
    assert sitemap_text(Resp(b"\x1f\x8bnot really gzip")) == ""
    assert sitemap_text(Resp(gzip.compress(b"<a>" * 1000)[:-8])) == ""


def test_sitemap_text_invalid_utf8_is_replaced():
    assert sitemap_text(Resp(gzip.compress(b"<a>\xff</a>"))) == "<a>�</a>"


def test_sitemap_text_oversize_returns_empty(monkeypatch):
    monkeypatch.setattr(checker.crawl, "MAX_SITEMAP_BYTES", 10)
    assert sitemap_text(Resp(gzip.compress(b"x" * 11))) == ""
    assert sitemap_text(Resp(gzip.compress(b"x" * 10))) == "x" * 10


# ---------- crawl: find_broken_links ----------

class StubCrawler:
    def __init__(self, statuses, blocked=()):
        self.statuses, self.blocked, self.requested = statuses, set(blocked), []

    def allowed(self, url):
        return url not in self.blocked

    def link_status(self, url):
        self.requested.append(url)
        return self.statuses.get(url, 200)


def test_broken_links_cap_and_reuse():
    links = ["https://e.fi/%d" % i for i in range(5)]
    c = StubCrawler({l: 404 for l in links})
    broken, checked, capped = find_broken_links({"p1": links[:3], "p2": links[1:]}, {}, c, 3)
    assert checked == 3 and capped is True
    assert broken == {"p1": links[:3], "p2": links[1:3]}  # unchecked links are not reported
    assert len(c.requested) == 3  # shared links requested once


def test_broken_links_exact_cap_not_capped():
    links = ["https://e.fi/1", "https://e.fi/2"]
    _, checked, capped = find_broken_links({"p": links}, {}, StubCrawler({}), 2)
    assert (checked, capped) == (2, False)


def test_broken_links_zero_max_caps_everything():
    c = StubCrawler({})
    assert find_broken_links({"p": ["https://e.fi/1"]}, {}, c, 0) == ({}, 0, True)
    assert c.requested == []


def test_broken_links_robots_blocked_skipped_and_do_not_cap():
    c = StubCrawler({"https://e.fi/ok": 500}, blocked={"https://e.fi/blocked"})
    broken, checked, capped = find_broken_links(
        {"p": ["https://e.fi/blocked", "https://e.fi/ok"]}, {}, c, 1)
    assert broken == {"p": ["https://e.fi/ok"]}
    assert (checked, capped) == (1, False)
    assert c.requested == ["https://e.fi/ok"]


def test_broken_links_known_status_matched_by_key_and_not_requested():
    c = StubCrawler({})
    known = {"https://www.e.fi/a/index.html": 404, "https://e.fi/b": 200, "https://e.fi/net": 0}
    broken, checked, capped = find_broken_links(
        {"p": ["https://e.fi/a/", "http://e.fi/b/", "https://e.fi/net"]}, known, c, 5)
    assert broken == {"p": ["https://e.fi/a/", "https://e.fi/net"]}
    assert (checked, capped) == (0, False) and c.requested == []


def test_broken_links_status_boundaries():
    c = StubCrawler({"https://e.fi/399": 399, "https://e.fi/400": 400, "https://e.fi/0": 0})
    links = ["https://e.fi/399", "https://e.fi/400", "https://e.fi/0"]
    broken, _, _ = find_broken_links({"p": links}, {}, c, 9)
    assert broken == {"p": ["https://e.fi/400", "https://e.fi/0"]}


def test_broken_links_empty_input():
    assert find_broken_links({}, {}, StubCrawler({}), 5) == ({}, 0, False)


# ---------- report ----------

@pytest.mark.parametrize("value", ["=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"])
def test_csv_safe_prefixes_formula_starters(value):
    assert csv_safe(value) == "'" + value


@pytest.mark.parametrize("value", ["", "plain", " =lead space", "a=b", 5, 0, -3, None, True, 1.5])
def test_csv_safe_leaves_others_untouched(value):
    assert csv_safe(value) == value


def test_excel_safe_removes_control_chars_only():
    assert excel_safe("a\x00b\x07c\x1fd") == "abcd"
    assert excel_safe("tab\tnl\ncr\rok ä€") == "tab\tnl\ncr\rok ä€"
    assert excel_safe(12) == 12 and excel_safe(None) is None


def row(**kw):
    base = {"url": "https://e.fi/", "status": 200, "title": "T", "issues": []}
    base.update(kw)
    return base


def test_write_csv_escapes_formulas_and_joins_issues(tmp_path):
    p = tmp_path / "r.csv"
    write_csv([row(title='=HYPERLINK("x")', issues=["A", "B"])], str(p), delimiter=";")
    assert p.read_bytes().startswith(b"\xef\xbb\xbf")
    with open(str(p), newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    assert rows[0]["title"] == "'=HYPERLINK(\"x\")"
    assert rows[0]["issues"] == "A; B"
    assert rows[0]["description"] == ""  # missing fields are blank


def test_write_xlsx_keeps_formula_text_and_strips_control_chars(tmp_path):
    p = tmp_path / "r.xlsx"
    write_xlsx([row(title="=1+1", description="bad\x00char", issues=["X", "Y"]), row(url="u2")], str(p))
    ws = load_workbook(str(p)).active
    assert ws.title == "SEO report"
    header = [c.value for c in ws[1]]
    r2 = dict(zip(header, [c.value for c in ws[2]]))
    assert r2["title"] == "=1+1" and ws.cell(2, header.index("title") + 1).data_type == "s"
    assert r2["description"] == "badchar"
    assert r2["issues"] == "X; Y"
    assert ws.max_row == 3
    assert all(ws.column_dimensions[k].width <= 60 for k in ws.column_dimensions)


def test_write_xlsx_no_results_has_header_only(tmp_path):
    p = tmp_path / "e.xlsx"
    write_xlsx([], str(p))
    assert load_workbook(str(p)).active.max_row == 1


# ---------- backwards compatibility of seo_checker ----------

# Every top-level name of the pre-split seo_checker.py that tests or scripts could import.
OLD_PUBLIC_NAMES = [
    "USER_AGENT", "TIMEOUT", "TITLE_MIN", "TITLE_MAX", "DESC_MIN", "DESC_MAX", "SLOW_RESPONSE_MS",
    "LARGE_PAGE_KB", "SITEMAP_PATHS", "NON_PAGE_EXTENSIONS", "MAX_SITEMAP_BYTES", "AI_CRAWLERS",
    "CSV_FIELDS", "CSV_DELIMITERS",
    "check_page", "get_attr", "find_meta", "find_schema_types", "parse_sitemap", "internal_links",
    "site_host", "link_key", "decode_html", "sitemap_text", "Crawler", "find_broken_links",
    "mark_duplicates", "write_csv", "csv_safe", "excel_safe", "write_xlsx", "print_summary",
    "normalize_url", "positive_int", "add_timestamp", "report_path_problem", "main",
]


def test_seo_checker_still_exposes_all_old_names():
    import seo_checker
    missing = [n for n in OLD_PUBLIC_NAMES if not hasattr(seo_checker, n)]
    assert missing == []


def test_seo_checker_reexports_are_the_package_objects():
    import seo_checker
    pairs = [(checker.crawl, "Crawler"), (checker.pages, "check_page"),
             (checker.report, "write_csv"), (checker.urls, "link_key")]
    for mod, name in pairs:
        assert getattr(seo_checker, name) is getattr(mod, name)


# ---------- malformed URLs from crawled sites must not crash the run ----------
# "http://[x" makes urllib raise ValueError("Invalid IPv6 URL").

def test_internal_links_skips_malformed_href():
    html = '<a href="http://[x">bad</a><a href="/ok">ok</a>'
    assert internal_links(html, "https://example.fi/") == ["https://example.fi/ok"]


def test_check_page_keeps_malformed_canonical_as_text():
    html = '<title>t</title><link rel="canonical" href="http://[x">'
    result = check_page(html, "https://example.fi/")
    assert result["canonical"] == "http://[x"
    assert "Canonical points elsewhere" in result["issues"]


def test_same_site_is_false_for_malformed_url():
    crawler = checker.crawl.Crawler.__new__(checker.crawl.Crawler)  # skip __init__ (no network)
    crawler.base = "https://example.fi"
    assert crawler._same_site("http://[x/page") is False
    assert crawler._same_site("https://example.fi/page") is True

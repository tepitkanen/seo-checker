"""Tests for duplicate titles/descriptions, AI crawler access and robots.txt sitemaps."""
import gzip
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seo_checker import AI_CRAWLERS, Crawler, check_page, mark_duplicates, print_summary
from test_crawler import (BASE, FakeResponse, make_site, run_main, sitemap,  # noqa: F401 (fixture)
                          sitemap_index)

LONG_DESC = "A description that is long enough to pass the length check for meta descriptions here."


def page(url, title="", desc="", final_url=None):
    html = f"<title>{title}</title>" + (f'<meta name="description" content="{desc}">' if desc else "")
    return check_page(html, url, final_url=final_url)


# ---------- mark_duplicates ----------

def test_duplicate_titles_are_flagged_on_every_page():
    results = [page(f"{BASE}/a", "Same title"), page(f"{BASE}/b", "Same title"),
               page(f"{BASE}/c", "Other title")]
    mark_duplicates(results)
    assert ["Duplicate title" in r["issues"] for r in results] == [True, True, False]


def test_duplicate_comparison_ignores_case_and_whitespace():
    results = [page(f"{BASE}/a", "Same  Title "), page(f"{BASE}/b", "same title")]
    mark_duplicates(results)
    assert all("Duplicate title" in r["issues"] for r in results)


def test_duplicate_descriptions_are_flagged():
    results = [page(f"{BASE}/a", "Title A", LONG_DESC), page(f"{BASE}/b", "Title B", LONG_DESC)]
    mark_duplicates(results)
    for r in results:
        assert "Duplicate meta description" in r["issues"]
        assert "Duplicate title" not in r["issues"]


def test_empty_values_are_not_duplicates():
    results = [page(f"{BASE}/a"), page(f"{BASE}/b"), check_page("", f"{BASE}/c", status=404)]
    mark_duplicates(results)
    for r in results:
        assert "Duplicate title" not in r["issues"]
        assert "Duplicate meta description" not in r["issues"]


def test_urls_of_the_same_page_are_not_duplicates():
    # /old redirects to /new, and /x/index.html is the same page as /x/.
    results = [page(f"{BASE}/old", "T", LONG_DESC, final_url=f"{BASE}/new"),
               page(f"{BASE}/new", "T", LONG_DESC),
               page(f"{BASE}/x/", "U"), page(f"{BASE}/x/index.html", "U")]
    mark_duplicates(results)
    assert not any("Duplicate" in issue for r in results for issue in r["issues"])


def test_description_text_is_in_the_result():
    assert page(f"{BASE}/a", "T", LONG_DESC)["description"] == LONG_DESC


# ---------- AI crawlers ----------

def test_no_robots_txt_blocks_no_ai_crawlers(make_site):
    make_site({})
    assert Crawler(BASE, delay=0).blocked_ai_crawlers() == []


def test_specific_ai_crawlers_are_reported(make_site):
    make_site({f"{BASE}/robots.txt": FakeResponse(
        "User-agent: GPTBot\nDisallow: /\n\nUser-agent: Google-Extended\nDisallow: /\n\n"
        "User-agent: *\nAllow: /\n")})
    assert Crawler(BASE, delay=0).blocked_ai_crawlers() == ["GPTBot", "Google-Extended"]


def test_blocking_everyone_blocks_all_ai_crawlers(make_site):
    make_site({f"{BASE}/robots.txt": FakeResponse("User-agent: *\nDisallow: /\n")})
    assert Crawler(BASE, delay=0).blocked_ai_crawlers() == list(AI_CRAWLERS)


def test_blocking_only_a_folder_does_not_count(make_site):
    make_site({f"{BASE}/robots.txt": FakeResponse("User-agent: ClaudeBot\nDisallow: /private/\n")})
    assert Crawler(BASE, delay=0).blocked_ai_crawlers() == []


def test_summary_lists_blocked_ai_crawlers(capsys):
    print_summary([], llms_txt=True, blocked_ai=["GPTBot", "ClaudeBot"])
    assert "AI crawlers blocked by robots.txt: GPTBot, ClaudeBot" in capsys.readouterr().out
    print_summary([], llms_txt=True, blocked_ai=[])
    assert "AI crawlers blocked by robots.txt: none" in capsys.readouterr().out
    print_summary([], llms_txt=True)
    assert "AI crawlers" not in capsys.readouterr().out


# ---------- Sitemap: lines in robots.txt ----------

def test_sitemap_from_robots_txt_is_used(make_site):
    site = make_site({
        f"{BASE}/robots.txt": FakeResponse(f"User-agent: *\nAllow: /\nSitemap: {BASE}/custom/map.xml\n"),
        f"{BASE}/custom/map.xml": FakeResponse(sitemap(f"{BASE}/a", f"{BASE}/b")),
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/wrong")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a", f"{BASE}/b"]
    assert f"{BASE}/sitemap.xml" not in site.requested


def test_all_robots_sitemaps_are_combined_without_duplicates(make_site):
    make_site({
        f"{BASE}/robots.txt": FakeResponse(
            f"Sitemap: {BASE}/posts.xml\nSitemap: {BASE}/pages.xml\nSitemap: {BASE}/posts.xml\n"),
        f"{BASE}/posts.xml": FakeResponse(sitemap(f"{BASE}/p1", f"{BASE}/shared")),
        f"{BASE}/pages.xml": FakeResponse(sitemap(f"{BASE}/shared", f"{BASE}/about")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/p1", f"{BASE}/shared", f"{BASE}/about"]


def test_robots_sitemap_can_be_an_index(make_site):
    make_site({
        f"{BASE}/robots.txt": FakeResponse(f"Sitemap: {BASE}/index.xml\n"),
        f"{BASE}/index.xml": FakeResponse(sitemap_index(f"{BASE}/child.xml")),
        f"{BASE}/child.xml": FakeResponse(sitemap(f"{BASE}/a")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a"]


def test_robots_sitemaps_respect_max_pages(make_site):
    site = make_site({
        f"{BASE}/robots.txt": FakeResponse(f"Sitemap: {BASE}/one.xml\nSitemap: {BASE}/two.xml\n"),
        f"{BASE}/one.xml": FakeResponse(sitemap(f"{BASE}/a", f"{BASE}/b")),
        f"{BASE}/two.xml": FakeResponse(sitemap(f"{BASE}/c")),
    })
    assert Crawler(BASE, delay=0).discover_urls(2) == [f"{BASE}/a", f"{BASE}/b"]
    assert f"{BASE}/two.xml" not in site.requested


def test_robots_sitemap_on_other_host_is_ignored(make_site):
    site = make_site({
        f"{BASE}/robots.txt": FakeResponse("Sitemap: https://other.fi/sitemap.xml\n"),
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a"]
    assert "https://other.fi/sitemap.xml" not in site.requested


def test_failing_robots_sitemap_falls_back_to_default_paths(make_site):
    make_site({
        f"{BASE}/robots.txt": FakeResponse(f"Sitemap: {BASE}/missing.xml\n"),  # 404
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a"]


def test_url_listed_twice_in_a_sitemap_is_checked_once(make_site):
    make_site({f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a", f"{BASE}/b", f"{BASE}/a"))})
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a", f"{BASE}/b"]


def test_gzipped_sitemap_is_read(make_site):
    gz = gzip.compress(sitemap(f"{BASE}/a", f"{BASE}/b").encode("utf-8"))
    make_site({
        f"{BASE}/robots.txt": FakeResponse(f"Sitemap: {BASE}/sitemap.xml.gz\n"),
        f"{BASE}/sitemap.xml.gz": FakeResponse(content=gz, content_type="application/x-gzip"),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a", f"{BASE}/b"]


def test_gzipped_child_sitemap_is_read(make_site):
    make_site({
        f"{BASE}/sitemap.xml": FakeResponse(sitemap_index(f"{BASE}/child.xml.gz")),
        f"{BASE}/child.xml.gz": FakeResponse(content=gzip.compress(sitemap(f"{BASE}/a").encode())),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a"]


def test_broken_gzip_sitemap_falls_back(make_site):
    make_site({
        f"{BASE}/robots.txt": FakeResponse(f"Sitemap: {BASE}/bad.xml.gz\n"),
        f"{BASE}/bad.xml.gz": FakeResponse(content=b"\x1f\x8bnot really gzip"),
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a"]


# ---------- main() ----------

def test_main_reports_duplicates_and_ai_crawlers(make_site, monkeypatch, capsys):
    html = "<title>Same title for both pages</title>"
    make_site({
        f"{BASE}/robots.txt": FakeResponse("User-agent: PerplexityBot\nDisallow: /\n"),
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a", f"{BASE}/b")),
        f"{BASE}/a": FakeResponse(html, content_type="text/html"),
        f"{BASE}/b": FakeResponse(html, content_type="text/html"),
    })
    run_main(monkeypatch, BASE, "--delay", "0")
    out = capsys.readouterr().out
    assert "2  Duplicate title" in out
    assert "AI crawlers blocked by robots.txt: PerplexityBot" in out


# ---------- extra edge cases ----------

def test_three_pages_with_same_title_get_one_issue_each():
    results = [page(f"{BASE}/{n}", "Same") for n in "abc"]
    mark_duplicates(results)
    for r in results:
        assert r["issues"].count("Duplicate title") == 1


def test_truncated_gzip_sitemap_falls_back(make_site):
    good = gzip.compress(sitemap(f"{BASE}/a").encode())
    make_site({
        f"{BASE}/robots.txt": FakeResponse(f"Sitemap: {BASE}/cut.xml.gz\n"),
        f"{BASE}/cut.xml.gz": FakeResponse(content=good[:len(good) // 2]),
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/ok")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/ok"]


def test_corrupt_deflate_body_in_gzip_sitemap_does_not_crash(make_site):
    # Valid gzip header, garbage compressed data: gzip.decompress raises zlib.error.
    good = bytearray(gzip.compress(sitemap(f"{BASE}/a").encode()))
    for i in range(12, len(good) - 8):
        good[i] ^= 0xFF
    make_site({
        f"{BASE}/robots.txt": FakeResponse(f"Sitemap: {BASE}/corrupt.xml.gz\n"),
        f"{BASE}/corrupt.xml.gz": FakeResponse(content=bytes(good)),
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/ok")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/ok"]


def test_gzip_sitemap_over_size_limit_is_skipped(make_site, monkeypatch):
    monkeypatch.setattr("checker.crawl.MAX_SITEMAP_BYTES", 1000)
    big = sitemap(*[f"{BASE}/p{i}" for i in range(100)])  # well over 1000 bytes unpacked
    make_site({
        f"{BASE}/robots.txt": FakeResponse(f"Sitemap: {BASE}/big.xml.gz\n"),
        f"{BASE}/big.xml.gz": FakeResponse(content=gzip.compress(big.encode())),
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/ok")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/ok"]


def test_multi_member_gzip_sitemap_is_read_whole(make_site):
    text = sitemap(f"{BASE}/a", f"{BASE}/b").encode()
    half = len(text) // 2
    make_site({
        f"{BASE}/robots.txt": FakeResponse(f"Sitemap: {BASE}/multi.xml.gz\n"),
        f"{BASE}/multi.xml.gz": FakeResponse(content=gzip.compress(text[:half]) + gzip.compress(text[half:])),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a", f"{BASE}/b"]


# ---------- sitemap redirects, robots.txt 401/403, CSV formulas ----------

def test_sitemap_redirecting_to_other_host_is_ignored(make_site):
    make_site({
        f"{BASE}/robots.txt": FakeResponse(f"Sitemap: {BASE}/moved.xml\n"),
        f"{BASE}/moved.xml": FakeResponse(sitemap(f"{BASE}/evil"), url="https://other.example/sitemap.xml"),
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/ok")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/ok"]


def test_child_sitemap_redirecting_to_other_host_is_skipped(make_site):
    from test_crawler import sitemap_index
    make_site({
        f"{BASE}/sitemap_index.xml": FakeResponse(sitemap_index(f"{BASE}/a.xml", f"{BASE}/b.xml")),
        f"{BASE}/a.xml": FakeResponse(sitemap(f"{BASE}/evil"), url="http://192.168.1.1/a.xml"),
        f"{BASE}/b.xml": FakeResponse(sitemap(f"{BASE}/good")),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/good"]


def test_sitemap_redirect_within_site_is_followed(make_site):
    make_site({
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a"), url=f"{BASE}/new/sitemap.xml"),
    })
    assert Crawler(BASE, delay=0).discover_urls(10) == [f"{BASE}/a"]


def test_robots_txt_403_warns_and_allows_all(make_site, capsys):
    make_site({f"{BASE}/robots.txt": FakeResponse("User-agent: *\nDisallow: /", status=403)})
    crawler = Crawler(BASE, delay=0)
    assert "robots.txt returned 403" in capsys.readouterr().out
    assert crawler.allowed(f"{BASE}/page") and crawler.blocked_ai_crawlers() == []


def test_robots_txt_404_does_not_warn(make_site, capsys):
    make_site({})
    Crawler(BASE, delay=0)
    assert "warning" not in capsys.readouterr().out


def test_csv_neutralises_formulas(tmp_path):
    import csv
    from seo_checker import write_csv
    page = check_page('<title>=HYPERLINK("http://evil.example","x")</title>'
                      '<meta name="description" content="@SUM(1)">', BASE + "/")
    path = tmp_path / "r.csv"
    write_csv([page], path)
    with open(path, encoding="utf-8-sig", newline="") as f:
        row = next(csv.DictReader(f))
    assert row["title"] == '\'=HYPERLINK("http://evil.example","x")'
    assert row["description"] == "'@SUM(1)"
    assert row["url"] == BASE + "/" and row["status"] == "200"

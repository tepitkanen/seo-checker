"""Tests for local_site.py (the manual end-to-end test site) and seo_checker.py run against it.

Servers bind only to 127.0.0.1 on a free port (port and port+1 are both needed).
"""
import gzip
import os
import socket
import sys
import threading
import zlib

import pytest
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import local_site  # noqa: E402
from local_site import HOST, SiteServer, build_site, corrupt, gzip_bomb, robots_txt  # noqa: E402
from seo_checker import MAX_SITEMAP_BYTES, Crawler, check_page, find_broken_links, sitemap_text  # noqa: E402


# ---------- helpers ----------

def free_port():
    """A port p where p and p+1 were both free a moment ago."""
    for _ in range(50):
        with socket.socket() as s:
            s.bind((HOST, 0))
            port = s.getsockname()[1]
        if port >= 65535:
            continue
        try:
            with socket.socket() as s2:
                s2.bind((HOST, port + 1))
        except OSError:
            continue
        return port
    raise RuntimeError("no free port pair")


class Site:
    def __init__(self, robots_status=200):
        last = None
        for _ in range(10):  # retry if another process grabs the ports meanwhile
            self.port = free_port()
            self.base = f"http://{HOST}:{self.port}"
            self.other = f"http://{HOST}:{self.port + 1}"
            self.routes, self.other_routes = build_site(self.port)
            self.routes["/robots.txt"] = (robots_status, robots_txt(self.port), "text/plain")
            try:
                self.main = SiteServer(self.port, self.routes)
            except OSError as e:
                last = e
                continue
            try:
                self.second = SiteServer(self.port + 1, self.other_routes)
            except OSError as e:
                self.main.server_close()
                last = e
                continue
            break
        else:
            raise last
        self.servers = [self.main, self.second]
        self.threads = [threading.Thread(target=s.serve_forever, kwargs={"poll_interval": 0.02},
                                         daemon=True) for s in self.servers]
        for t in self.threads:
            t.start()

    def close(self):
        for s in self.servers:
            s.shutdown()
            s.server_close()
        for t in self.threads:
            t.join(5)


class RecordingCrawler(Crawler):
    def __init__(self, *args, **kwargs):
        self.requested = []
        super().__init__(*args, **kwargs)

    def get(self, url):
        self.requested.append(url)
        return super().get(url)


@pytest.fixture(scope="module")
def site():
    s = Site(200)
    yield s
    s.close()


@pytest.fixture(scope="module")
def site403():
    s = Site(403)
    yield s
    s.close()


@pytest.fixture(scope="module")
def crawl(site):
    c = RecordingCrawler(site.base, delay=0)
    urls = c.discover_urls(100)
    return c, urls


@pytest.fixture(scope="module")
def crawl403(site403):
    c = RecordingCrawler(site403.base, delay=0)
    urls = c.discover_urls(100)
    return c, urls


def paths(site, urls):
    return {u[len(site.base):] if u.startswith(site.base) else u for u in urls}


# ---------- helper functions ----------

def test_corrupt_keeps_gzip_header_but_breaks_body():
    good = gzip.compress(b"<urlset>" + b"abc " * 50 + b"</urlset>")
    bad = corrupt(good)
    assert bad[:12] == good[:12]
    assert len(bad) == len(good)
    assert bad != good
    with pytest.raises((zlib.error, OSError, EOFError)):
        gzip.decompress(bad)


def test_gzip_bomb_is_small_but_unpacks_beyond_the_limit():
    data = gzip_bomb("<urlset><url><loc>x</loc></url></urlset>", 60)
    assert len(data) < 500 * 1024
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = d.decompress(data, MAX_SITEMAP_BYTES + 1)
    assert len(out) > MAX_SITEMAP_BYTES  # unpacked size exceeds the 50 MB cap
    assert out.startswith(b"<urlset>")


def test_gzip_bomb_unpacked_size_matches_request():
    data = gzip_bomb("<a><url>t</url></a>", 3)
    out = gzip.decompress(data)
    assert out.startswith(b"<a>") and out.endswith(b"<url>t</url></a>")
    assert 3 * 1024 * 1024 < len(out) < 3 * 1024 * 1024 + 100


def test_robots_txt_content():
    text = robots_txt(9000)
    lines = text.splitlines()
    assert "Disallow: /private" in lines
    assert "Sitemap: http://127.0.0.1:9000/sitemap.xml.gz" in lines
    assert "Sitemap: http://127.0.0.1:9000/sitemap-index.xml" in lines
    assert "Sitemap: http://127.0.0.1:9000/moved-sitemap.xml" in lines
    assert "Sitemap: https://example.com/sitemap.xml" in lines


def test_build_site_routes_use_given_port_and_other_host_is_port_plus_one():
    routes, other = build_site(9100)
    assert routes["/moved-sitemap.xml"] == (301, "http://127.0.0.1:9101/sitemap-a.xml", None)
    assert routes["/child-moved.xml"] == (301, "http://127.0.0.1:9101/sitemap-b.xml", None)
    assert routes["/old-about"] == (301, "/about", None)
    assert set(other) == {"/sitemap-a.xml", "/sitemap-b.xml"}
    assert "http://127.0.0.1:9100/must-not-appear-moved-sitemap" in other["/sitemap-a.xml"][1]


def test_must_not_appear_pages_have_no_real_route():
    routes, _ = build_site(9200)
    assert not [p for p in routes if p.startswith("/must-not-appear")]


def test_every_listed_must_not_appear_is_only_in_rejected_sitemaps():
    routes, other = build_site(9300)
    good = [p for p in ("/sitemap.xml.gz", "/child-ok.xml.gz")]
    for p in good:
        assert "must-not-appear" not in sitemap_text(_FakeResp(routes[p][1]))


class _FakeResp:
    def __init__(self, content):
        self.content = content if isinstance(content, bytes) else content.encode()
        self.text = self.content.decode("utf-8", "replace")


# ---------- the HTTP handler ----------

def test_handler_serves_pages_redirects_and_404(site):
    r = requests.get(site.base + "/about", timeout=5)
    assert r.status_code == 200 and "text/html" in r.headers["Content-Type"]
    r = requests.get(site.base + "/old-about", allow_redirects=False, timeout=5)
    assert r.status_code == 301 and r.headers["Location"] == "/about" and r.content == b""
    assert requests.get(site.base + "/nope", timeout=5).status_code == 404
    assert requests.get(site.base + "/server-error", timeout=5).status_code == 500


def test_handler_ignores_query_string_and_sends_x_robots_tag(site):
    r = requests.get(site.base + "/noindex-header?x=1", timeout=5)
    assert r.status_code == 200
    assert r.headers["X-Robots-Tag"] == "noindex"
    assert requests.get(site.base + "/none-header", timeout=5).headers["X-Robots-Tag"] == "googlebot: none"
    assert "X-Robots-Tag" not in requests.get(site.base + "/about", timeout=5).headers


def test_handler_content_length_matches_for_binary_and_non_ascii(site):
    r = requests.get(site.base + "/child-ok.xml.gz", timeout=5)
    assert int(r.headers["Content-Length"]) == len(r.content)
    assert r.content[:2] == b"\x1f\x8b"


def test_other_host_serves_sitemaps_on_port_plus_one(site):
    r = requests.get(site.other + "/sitemap-a.xml", timeout=5)
    assert r.status_code == 200 and "must-not-appear-moved-sitemap" in r.text
    assert requests.get(site.other + "/about", timeout=5).status_code == 404


def test_server_instance_stores_port_and_routes(site):
    assert site.main.port == site.port and site.second.port == site.port + 1
    assert site.main.routes is site.routes


# ---------- sitemap discovery against the live site (robots.txt 200) ----------

def test_discovery_never_finds_must_not_appear_pages(site, crawl):
    _, urls = crawl
    assert not [u for u in urls if "must-not-appear" in u]


def test_discovery_reads_gzipped_sitemap_and_good_child(site, crawl):
    _, urls = crawl
    found = paths(site, urls)
    assert "/from-child-sitemap" in found
    assert {"/", "/about", "/formula", "/sloppy", "/old-about", "/server-error"} <= found


def test_discovery_has_no_duplicates_and_no_private_page_in_sitemap(site, crawl):
    _, urls = crawl
    assert len(urls) == len(set(urls))
    assert "/private/secret" not in paths(site, urls)


def test_discovery_exact_set(site, crawl):
    _, urls = crawl
    expected = {"/", "/about", "/noindex-header", "/none-header", "/slow", "/large", "/sloppy",
                "/meta-noindex", "/formula", "/old-about", "/server-error", "/from-child-sitemap",
                "/from-nested-index"}
    assert paths(site, urls) == expected


def test_example_com_sitemap_is_never_requested(crawl):
    c, _ = crawl
    assert not [u for u in c.requested if "example.com" in u]


def test_every_listed_sitemap_is_requested_and_children_too(site, crawl):
    c, _ = crawl
    req = set(c.requested)
    for p in ("/sitemap.xml.gz", "/sitemap-index.xml", "/moved-sitemap.xml", "/child-ok.xml.gz",
              "/child-corrupt.xml.gz", "/child-truncated.xml.gz", "/child-huge.xml.gz",
              "/child-moved.xml"):
        assert site.base + p in req, p


def test_discovery_respects_max_pages(site):
    c = Crawler(site.base, delay=0)
    urls = c.discover_urls(3)
    assert len(urls) == 3


def test_each_bad_sitemap_individually_yields_nothing(site, crawl):
    c, _ = crawl
    for p in ("/child-corrupt.xml.gz", "/child-truncated.xml.gz", "/child-huge.xml.gz",
              "/child-moved.xml", "/moved-sitemap.xml"):
        assert c._read_sitemap(site.base + p, 100) == [], p
    assert c._read_sitemap(site.base + "/child-ok.xml.gz", 100) == [site.base + "/from-child-sitemap"]


def test_sitemap_text_handles_bad_gzip_without_raising(site):
    for p in ("/child-corrupt.xml.gz", "/child-truncated.xml.gz", "/child-huge.xml.gz"):
        resp = requests.get(site.base + p, timeout=10)
        assert sitemap_text(resp) == "", p


# ---------- robots.txt behaviour ----------

def test_robots_200_blocks_gptbot_and_claudebot_only(crawl):
    c, _ = crawl
    assert c.blocked_ai_crawlers() == ["GPTBot", "ClaudeBot"]


def test_robots_200_disallows_private(site, crawl):
    c, _ = crawl
    assert not c.allowed(site.base + "/private/secret")
    assert c.allowed(site.base + "/about")


def test_llms_txt_detected(crawl):
    c, _ = crawl
    assert c.has_llms_txt()


def test_fetch_pages_skips_private_and_reports_statuses(site, capsys):
    c = Crawler(site.base, delay=0)
    # /slow takes 1.5 s, so crawl a subset by discovering and fetching selected URLs only
    results = {}
    for url in [site.base + p for p in ("/about", "/old-about", "/server-error", "/noindex-header",
                                        "/none-header", "/meta-noindex", "/sloppy", "/formula")]:
        resp = c.get(url)
        results[url[len(site.base):]] = (resp.status_code, resp.url, resp.headers.get("X-Robots-Tag", ""))
    assert results["/about"][0] == 200
    assert results["/old-about"][0] == 200 and results["/old-about"][1] == site.base + "/about"
    assert results["/server-error"][0] == 500
    assert results["/noindex-header"][2] == "noindex"


def test_check_page_results_on_site_pages(site):
    def issues(path, **kw):
        r = requests.get(site.base + path, timeout=5)
        info = {"x_robots_tag": r.headers.get("X-Robots-Tag", "")}
        return check_page(r.text, site.base + path, **kw, info=info)["issues"]

    assert issues("/about") == []
    assert issues("/") == []
    sloppy = issues("/sloppy")
    for i in ("Title too short", "Missing meta description", "Multiple H1", "Images missing alt",
              "Missing canonical", "No structured data"):
        assert i in sloppy
    assert "Noindex" in issues("/meta-noindex")
    assert "Noindex (X-Robots-Tag)" in issues("/noindex-header")
    assert "Noindex (X-Robots-Tag)" in issues("/none-header")
    assert issues("/formula") == []


def test_large_page_flagged(site):
    r = requests.get(site.base + "/large", timeout=5)
    res = check_page(r.text, site.base + "/large", info={"size_kb": round(len(r.content) / 1024)})
    assert "Large page" in res["issues"]


def test_formula_page_has_formula_like_title_and_description(site):
    r = requests.get(site.base + "/formula", timeout=5)
    res = check_page(r.text, site.base + "/formula")
    assert res["title"].startswith("=") and res["description"].startswith("@")


def test_link_check_on_homepage_matches_comments(site):
    c = Crawler(site.base, delay=0)
    home = requests.get(site.base + "/", timeout=5).text
    from seo_checker import internal_links
    links = internal_links(home, site.base + "/")
    known = {site.base + "/": 200, site.base + "/about": 200}
    broken, checked, capped = find_broken_links({site.base + "/": links}, known, c, 50)
    got = {u[len(site.base):] for u in broken[site.base + "/"]}
    assert got == {"/missing", "/server-error"}  # /old-about redirects to /about: fine
    assert not capped
    # /about/ is the trailing-slash form of a crawled page, /private/secret is disallowed,
    # example.com is external: only /missing, /old-about, /server-error are requested.
    assert checked == 3


# ---------- robots.txt returns 403 ----------

def test_robots_403_warns_and_treats_everything_allowed(site403, capsys):
    c = Crawler(site403.base, delay=0)
    assert "robots.txt returned 403" in capsys.readouterr().out
    assert c.allowed(site403.base + "/private/secret")
    assert c.blocked_ai_crawlers() == []
    assert c.robots_sitemaps() == []


def test_robots_403_falls_back_to_homepage_links(site403, crawl403):
    c, urls = crawl403
    found = paths(site403, urls)
    assert "/" in found and "/about" in found
    assert "/from-child-sitemap" not in found  # Sitemap: lines ignored
    assert not [u for u in urls if "must-not-appear" in u]
    # none of the listed sitemaps were requested (default /sitemap.xml names may be tried)
    assert site403.base + "/sitemap.xml.gz" not in c.requested
    assert site403.base + "/sitemap-index.xml" not in c.requested


def test_robots_403_link_check_requests_private_secret(site403, crawl403):
    c, urls = crawl403
    assert site403.base + "/private/secret" in urls  # homepage links it; nothing disallows it
    broken, _, _ = find_broken_links({site403.base + "/": [site403.base + "/private/secret"]}, {}, c, 10)
    assert broken == {}
    assert site403.base + "/private/secret" in c.requested


def test_main_default_constants():
    assert local_site.HOST == "127.0.0.1"


# ---------- duplicate titles/descriptions and port clashes ----------

def test_only_slow_and_large_are_duplicates():
    from seo_checker import mark_duplicates
    base = "http://127.0.0.1:1"
    routes, _ = build_site(1)
    results = [check_page(body, base + path) for path, (status, body, _) in routes.items()
               if status == 200 and isinstance(body, str) and body.startswith("<!doctype")]
    # /old-about redirects to /about: same page, so it must not count as a duplicate.
    results.append(check_page(routes["/about"][1], base + "/old-about", final_url=base + "/about"))
    mark_duplicates(results)
    dup_title = sorted(r["url"][len(base):] for r in results if "Duplicate title" in r["issues"])
    dup_desc = sorted(r["url"][len(base):] for r in results if "Duplicate meta description" in r["issues"])
    assert dup_title == dup_desc == ["/large", "/slow"]


def test_second_server_on_same_port_fails(site):
    with pytest.raises(OSError):
        SiteServer(site.port, {})

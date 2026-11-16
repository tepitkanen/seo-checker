"""Extra edge-case tests for the low-priority fixes: alt handling, sitemap DTD refusal,
the response size cap (against a real local HTTP server), site_host and printable."""
import csv
import gzip
import os
import sys
import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import requests
from openpyxl import load_workbook

from checker import crawl
from checker.crawl import Crawler, parse_sitemap
from checker.pages import check_page
from checker.report import CSV_FIELDS, write_csv, write_xlsx
from checker.urls import printable, site_host


def page(imgs):
    return check_page(f"<html><head><title>t</title></head><body><h1>x</h1>{imgs}</body></html>",
                      "https://e.fi/")


# ---------- alt handling ----------

@pytest.mark.parametrize("imgs,missing,empty", [
    ("<img src=a.png>", 1, 0),
    ("<img src=a.png alt>", 0, 1),            # valueless attribute parses as alt=""
    ("<img src=a.png ALT='Hi'>", 0, 0),       # attribute names are case-insensitive
    ("<IMG SRC=a.png ALT=''>", 0, 1),
    ("<img src=a.png alt='  \t\n '>", 0, 1),
    ("<img src=a.png alt='0'>", 0, 0),
    ("<img srcset='a.png 1x' alt='ok'>", 0, 0),
    ("<picture><source srcset='a.webp'><img src=a.png></picture>", 1, 0),
    ("<picture><source srcset='a.webp' alt='x'><img src=a.png alt='ok'></picture>", 0, 0),
    ("<img src=a alt='ok'><img src=b><img src=c alt=''><img src=d alt=' '>", 1, 2),
    ("", 0, 0),
])
def test_alt_counts(imgs, missing, empty):
    r = page(imgs)
    assert (r["images_missing_alt"], r["images_empty_alt"]) == (missing, empty)
    assert ("Images missing alt" in r["issues"]) == bool(missing)
    assert ("Images with empty alt" in r["issues"]) == bool(empty)


def test_missing_alt_issue_listed_before_empty_alt():
    r = page("<img src=a><img src=b alt=''>")
    assert r["issues"].index("Images missing alt") < r["issues"].index("Images with empty alt")


# ---------- sitemap DTD refusal ----------

LOC = "<url><loc>https://e.fi/a</loc></url>"


def sm(body, head=""):
    return f'<?xml version="1.0"?>{head}<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{body}</urlset>'


@pytest.mark.parametrize("head", [
    "<!DOCTYPE urlset>", "<!doctype urlset>", "<! DOCTYPE urlset>",
    '<!DOCTYPE x [<!ENTITY a "b">]>', '<!ENTITY a "b">',
])
def test_dtd_variants_refused(head):
    with pytest.raises(ET.ParseError):
        parse_sitemap(sm(LOC, head))


def test_sitemap_index_with_doctype_refused():
    xml = ('<!DOCTYPE sitemapindex><sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<sitemap><loc>https://e.fi/s.xml</loc></sitemap></sitemapindex>')
    with pytest.raises(ET.ParseError):
        parse_sitemap(xml)


def test_doctype_in_comment_or_cdata_is_also_refused():
    # Known, acceptable over-blocking: a regex can't tell comments/CDATA from real declarations.
    with pytest.raises(ET.ParseError):
        parse_sitemap(sm(LOC + "<!-- <!DOCTYPE x> -->"))
    with pytest.raises(ET.ParseError):
        parse_sitemap(sm("<url><loc><![CDATA[https://e.fi/<!ENTITY]]></loc></url>"))


def test_url_containing_doctype_word_without_bang_is_fine():
    urls, is_index = parse_sitemap(sm("<url><loc>https://e.fi/doctype</loc></url>"))
    assert urls == ["https://e.fi/doctype"] and not is_index


# ---------- Crawler.get against a real server ----------

BODY = b"<html><title>caf\xc3\xa9</title></html>"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, headers, data=b""):
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = self.path
        if p == "/gzip":
            data = gzip.compress(b"A" * 5000)
            self._send(200, {"Content-Encoding": "gzip", "Content-Type": "text/html; charset=utf-8",
                             "Content-Length": str(len(data))}, data)
        elif p == "/chunked":
            chunks = b"".join(b"400\r\n" + b"B" * 1024 + b"\r\n" for _ in range(5)) + b"0\r\n\r\n"
            self._send(200, {"Transfer-Encoding": "chunked", "Content-Type": "text/html; charset=utf-8"}, chunks)
        elif p.startswith("/size/"):
            data = b"C" * int(p.rsplit("/", 1)[1].split("?")[0])
            self._send(200, {"Content-Type": "text/plain", "Content-Length": str(len(data))}, data)
        elif p == "/page":
            self._send(200, {"Content-Type": "text/html; charset=utf-8", "Content-Length": str(len(BODY))}, BODY)
        elif p == "/redirect":
            self._send(302, {"Location": "/page", "Content-Length": "0"})
        else:
            self._send(404, {"Content-Length": "0"})


@pytest.fixture(scope="module")
def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def crawler(server):
    c = Crawler(server, delay=0)
    yield c
    c.session.close()


def test_get_normal_response_attributes(crawler, server):
    resp = crawler.get(server + "/page")
    assert resp.status_code == 200 and resp.ok
    assert resp.content == BODY
    assert resp.text == BODY.decode("utf-8")
    assert resp.url == server + "/page"
    assert resp.elapsed.total_seconds() >= 0
    assert resp.headers["Content-Type"].startswith("text/html")


def test_get_follows_redirects_and_keeps_history(crawler, server):
    resp = crawler.get(server + "/redirect")
    assert resp.url == server + "/page" and resp.content == BODY
    assert [r.status_code for r in resp.history] == [302]


def test_get_404_returns_response(crawler, server):
    resp = crawler.get(server + "/nope")
    assert resp.status_code == 404 and not resp.ok and resp.content == b""


def test_get_content_readable_repeatedly(crawler, server):
    resp = crawler.get(server + "/page")
    assert resp.content == resp.content == BODY
    assert resp.text == resp.text


def test_cap_boundaries(crawler, server, monkeypatch, capsys):
    monkeypatch.setattr(crawl, "MAX_RESPONSE_BYTES", 100_000)
    assert len(crawler.get(server + "/size/99999").content) == 99999
    assert len(crawler.get(server + "/size/100000").content) == 100000  # exactly at cap is allowed
    capsys.readouterr()
    assert crawler.get(server + "/size/100001") is None
    assert "skipped (over" in capsys.readouterr().out


def test_cap_message_escapes_control_chars_in_url(crawler, server, monkeypatch, capsys):
    monkeypatch.setattr(crawl, "MAX_RESPONSE_BYTES", 10)
    assert crawler.get(server + "/size/1000?x=%1b[31m") is None
    out = capsys.readouterr().out
    assert "skipped (over" in out and "\x1b" not in out


def test_cap_applies_to_decoded_gzip_bytes(crawler, server, monkeypatch):
    assert len(gzip.compress(b"A" * 5000)) < 1000
    monkeypatch.setattr(crawl, "MAX_RESPONSE_BYTES", 4000)  # above the wire size, below the decoded size
    assert crawler.get(server + "/gzip") is None
    monkeypatch.setattr(crawl, "MAX_RESPONSE_BYTES", 5000)
    resp = crawler.get(server + "/gzip")
    assert resp.content == b"A" * 5000 and resp.text == "A" * 5000


def test_cap_applies_to_chunked(crawler, server, monkeypatch):
    monkeypatch.setattr(crawl, "MAX_RESPONSE_BYTES", 5000)
    assert crawler.get(server + "/chunked") is None
    monkeypatch.setattr(crawl, "MAX_RESPONSE_BYTES", 5 * 1024)
    assert crawler.get(server + "/chunked").content == b"B" * 5 * 1024


def test_connection_closed_after_success(crawler, server):
    resp = crawler.get(server + "/page")
    assert resp.raw.closed or resp.raw.isclosed()


def test_repeated_capped_fetches_do_not_exhaust_pool(crawler, server, monkeypatch):
    monkeypatch.setattr(crawl, "MAX_RESPONSE_BYTES", 10)
    for _ in range(30):  # would hang if capped responses leaked their connections (pool size 10)
        assert crawler.get(server + "/size/1000") is None


def test_connection_error_returns_none():
    c = Crawler.__new__(Crawler)
    c.delay = 0
    c.session = requests.Session()
    assert c.get("http://127.0.0.1:1/") is None  # port 1 on loopback: refused immediately


# ---------- site_host ----------

@pytest.mark.parametrize("url,expected", [
    ("HTTPS://Example.FI/x", "example.fi"),
    ("HTTPS://Example.FI:443/x", "example.fi"),   # urlparse lowercases the scheme
    ("http://www.example.fi:80", "example.fi"),
    ("http://example.fi:443", "example.fi:443"),
    ("https://example.fi:80", "example.fi:80"),
    ("https://u:p@example.fi:8443/", "example.fi:8443"),
    ("https://[::1]:443/", "[::1]"),
    ("https://[::1]:8080/", "[::1]:8080"),
    ("https://[2001:DB8::1]/", "[2001:db8::1]"),
    ("//example.fi/x", "example.fi"),
    ("https://www.www.example.fi", "www.example.fi"),
    ("https://example.fi:/", "example.fi"),
    ("", ""),
    ("example.fi/path", ""),                      # no scheme, so no netloc
    ("mailto:a@example.fi", ""),
])
def test_site_host_variants(url, expected):
    assert site_host(url) == expected


@pytest.mark.parametrize("url", ["https://example.fi:abc/", "https://example.fi:99999/", "https://example.fi:-1/"])
def test_site_host_invalid_port_raises_valueerror(url):
    with pytest.raises(ValueError):
        site_host(url)


def test_site_host_bad_ipv6_raises_valueerror():
    with pytest.raises(ValueError):
        site_host("https://[::1/")


# ---------- printable ----------

@pytest.mark.parametrize("text,expected", [
    ("plain åäö", "plain åäö"),
    ("a\tb", "a\\x09b"),
    ("a\nb\rc", "a\\x0ab\\x0dc"),
    ("a\x7fb", "a\\x7fb"),
    ("a\x85b\x9bc", "a\\x85b\\x9bc"),             # C1 incl. CSI
    ("\x1b[31mred", "\\x1b[31mred"),
    ("\x00", "\\x00"),
    ("a\u202eb", "a\\u202eb"),
    ("a\u2066b\u2069", "a\\u2066b\\u2069"),
    ("a\u200eb\u200f", "a\\u200eb\\u200f"),
    ("\xa0ok", "\xa0ok"),                          # NBSP (0xa0) is not a control
    ("", ""),
])
def test_printable(text, expected):
    assert printable(text) == expected


@pytest.mark.parametrize("value,expected", [(None, "None"), (123, "123"), (b"x", "b'x'")])
def test_printable_non_str(value, expected):
    assert printable(value) == expected


def test_printable_result_has_no_controls():
    out = printable("".join(chr(i) for i in range(0, 0xa0)) + "\u202a\u202b\u202c\u202d\u202e")
    assert all(ord(ch) >= 0x20 and not 0x7f <= ord(ch) <= 0x9f for ch in out)
    assert not any(0x202a <= ord(ch) <= 0x202e for ch in out)


# ---------- report columns ----------

EXPECTED = ["url", "status", "title", "title_length", "description", "description_length", "h1_count",
            "images_missing_alt", "images_empty_alt", "noindex", "canonical", "redirects_to",
            "schema_types", "response_ms", "size_kb", "broken_links", "issues"]


def test_column_order_constant():
    assert CSV_FIELDS == EXPECTED


def test_csv_and_xlsx_headers_and_values(tmp_path):
    r = page("<img src=a><img src=b alt=''><img src=c alt=' '>")
    write_csv([r], tmp_path / "r.csv")
    write_xlsx([r], tmp_path / "r.xlsx")
    with open(tmp_path / "r.csv", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    assert rows[0] == EXPECTED
    row = dict(zip(rows[0], rows[1]))
    assert (row["images_missing_alt"], row["images_empty_alt"]) == ("1", "2")
    assert "Images with empty alt" in row["issues"]
    ws = load_workbook(tmp_path / "r.xlsx")["SEO report"]
    assert [c.value for c in ws[1]] == EXPECTED
    assert [c.value for c in ws[2]][7:9] == [1, 2]


def test_report_tolerates_old_result_without_empty_alt(tmp_path):
    r = page("")
    del r["images_empty_alt"]
    write_csv([r], tmp_path / "r.csv")
    write_xlsx([r], tmp_path / "r.xlsx")

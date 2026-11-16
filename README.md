# seo-checker

[![Tests](https://github.com/tepitkanen/seo-checker/actions/workflows/tests.yml/badge.svg)](https://github.com/tepitkanen/seo-checker/actions/workflows/tests.yml)

A small command-line tool that crawls a website and reports common on-page SEO and GEO
(generative engine optimization) issues. The output is a console summary and an optional
CSV and/or Excel report.

Built with AI assistance (Claude Code): I designed the checks, reviewed the code and tested it.

## What it checks

For each page:

| Check | Flagged when |
|---|---|
| HTTP status | Anything other than 200 |
| Redirect | The URL redirects elsewhere (sitemaps should list final URLs); the target is reported |
| `<title>` | Missing, under 30 or over 60 characters |
| Meta description | Missing, under 70 or over 160 characters |
| H1 | Missing, or more than one |
| Images | `<img>` without `alt` text (count) |
| Robots meta | `noindex` |
| `X-Robots-Tag` header | `noindex` or `none` sent as an HTTP header (e.g. `noindex` or `googlebot: noindex`) |
| Canonical | Missing, or points to another URL |
| Structured data (GEO) | No JSON-LD / schema.org data; found `@type`s are listed |
| Response time | Server response took over 1000 ms |
| Page size | HTML over 500 KB (images, CSS and scripts not included) |
| Broken internal links | With `--check-links`: links on the page to same-site URLs that return 4xx/5xx or fail |

For the whole site, it checks whether `/llms.txt` exists. Structured data and `llms.txt` help
AI answer engines (ChatGPT, Perplexity, Google AI Overviews) understand and cite a site.

## How it finds pages

1. Reads `/sitemap_index.xml` or `/sitemap.xml` (generated automatically by Yoast and Rank Math
   on WordPress sites) and follows sub-sitemaps.
2. If there is no sitemap, it collects internal links from the homepage.

It respects `robots.txt`, waits between requests (`--delay`, default 1 s) and stops at `--max` pages.

## Install

Requires Python 3.9 or newer.

```
git clone https://github.com/tepitkanen/seo-checker.git
cd seo-checker
python -m venv .venv
.venv\Scripts\activate        # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Run the commands (and later `python seo_checker.py ...`) inside the `seo-checker` folder.

## Usage

```
python seo_checker.py https://example.fi --max 50 --csv report.csv
```

| Option | Default | Meaning |
|---|---|---|
| `--max` | 50 | Maximum number of pages to check |
| `--csv` | none | Write a per-page CSV report to this file |
| `--csv-delimiter` | `comma` | CSV column separator: `comma` or `semicolon` (use `semicolon` for Finnish and other European Excel versions) |
| `--xlsx` | none | Write the same report as an Excel file (opens correctly in any Excel language) |
| `--delay` | 1.0 | Seconds to wait between requests |
| `--check-links` | off | Also check internal links on each page for broken targets. Pages already crawled are not requested again; links blocked by `robots.txt` are skipped |
| `--max-links` | 100 | With `--check-links`: maximum number of extra link URLs to request (each waits `--delay`) |

### Example output

```
$ python seo_checker.py https://wordpress.org --max 5 --csv report.csv
  [200] https://wordpress.org/about/privacy/cookies/
  [200] https://wordpress.org/about/privacy/data-export-request/
  [200] https://wordpress.org/about/privacy/data-erasure-request/
  [200] https://wordpress.org/download/
  [200] https://wordpress.org/download/releases/

Pages checked: 5
Pages with issues: 5
llms.txt: found

Issues by type:
     4  Meta description too long
     4  No structured data
     2  Title too short

CSV report written to report_2026-11-15_143012.csv
```

A timestamp is added to report file names so earlier reports are never overwritten:
`--csv report.csv` writes e.g. `report_2026-11-15_143012.csv`. `--csv` and `--xlsx` can be used
together and share the same timestamp. The report has one row per page: URL, status, title and its length, description length, H1 count,
images missing alt, noindex, canonical, redirect target, schema types, response time (ms), page size
(KB), broken links and the list of issues.

## Tests

```
pytest
```

The checks live in `check_page(html, url)`, which takes plain HTML and does no network calls,
so the tests run offline against HTML samples.

## Possible next steps

- Suggest a meta description for pages missing one using a language model.

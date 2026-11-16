"""Output: CSV and Excel reports and the printed summary."""

import csv
from collections import Counter

from openpyxl import Workbook
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

CSV_FIELDS = ["url", "status", "title", "title_length", "description", "description_length", "h1_count",
              "images_missing_alt", "images_empty_alt", "noindex", "canonical", "redirects_to", "schema_types",
              "response_ms", "size_kb", "broken_links", "issues"]


CSV_DELIMITERS = {"comma": ",", "semicolon": ";"}


def write_csv(results, path, delimiter=","):
    # utf-8-sig so Excel shows Finnish characters correctly;
    # use delimiter=";" for Excel in locales like Finnish that expect semicolons.
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, delimiter=delimiter)
        writer.writeheader()
        for r in results:
            row = {**r, "issues": "; ".join(r["issues"])}
            writer.writerow({k: csv_safe(v) for k, v in row.items()})


def csv_safe(value):
    """Prefix ' to text that Excel would run as a formula, e.g. a crawled title "=HYPERLINK(...)"."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


EXCEL_CELL_MAX = 32767  # longest text Excel allows in a cell; longer cells make it "repair" the file
TRUNCATED = " [truncated]"


def excel_safe(value):
    """Remove control characters that Excel files can't store (openpyxl would raise an error),
    and shorten text that is over Excel's cell limit, e.g. a very long broken links list."""
    if not isinstance(value, str):
        return value
    value = ILLEGAL_CHARACTERS_RE.sub("", value)
    if len(value) > EXCEL_CELL_MAX:
        value = value[:EXCEL_CELL_MAX - len(TRUNCATED)] + TRUNCATED
    return value


def write_xlsx(results, path):
    """Write the report as an Excel file; avoids CSV delimiter problems in non-English Excel."""
    wb = Workbook()
    wb.remove(wb.worksheets[0])  # replace the default sheet with a named one
    ws = wb.create_sheet("SEO report")
    ws.append(CSV_FIELDS)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for r in results:
        # Missing fields become blank cells, like csv.DictWriter does for the CSV report.
        row = [r.get(field, "") if field != "issues" else "; ".join(r["issues"]) for field in CSV_FIELDS]
        ws.append([excel_safe(value) for value in row])
        for cell in ws[ws.max_row]:
            # A crawled title like "=HYPERLINK(...)" must stay text, not become a live formula.
            if cell.data_type == "f":
                cell.data_type = "s"
    # Fit each column to its longest value, capped so long URLs don't make huge columns.
    for index, column in enumerate(ws.columns, start=1):
        longest = max(len(str(cell.value or "")) for cell in column)
        ws.column_dimensions[get_column_letter(index)].width = min(longest + 2, 60)
    wb.save(path)


def print_summary(results, llms_txt, blocked_ai=None):
    """Print totals and issue counts; blocked_ai is the list from Crawler.blocked_ai_crawlers."""
    counts = Counter(issue for r in results for issue in r["issues"])
    print(f"\nPages checked: {len(results)}")
    print(f"Pages with issues: {sum(1 for r in results if r['issues'])}")
    print(f"llms.txt: {'found' if llms_txt else 'missing'}")
    if blocked_ai is not None:
        print(f"AI crawlers blocked by robots.txt: {', '.join(blocked_ai) if blocked_ai else 'none'}")
    if counts:
        print("\nIssues by type:")
        for issue, n in counts.most_common():
            print(f"  {n:4}  {issue}")

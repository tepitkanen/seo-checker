"""Tests for the report timestamp: add_timestamp() and how main() applies it."""
import sys
import time

import pytest
from openpyxl import load_workbook

import seo_checker
from seo_checker import add_timestamp, main
from test_crawler import BASE, PAGE_HTML, FakeResponse, make_site, sitemap  # noqa: F401 (fixture)

STAMP = "2026-11-15_143012"


# --- add_timestamp -------------------------------------------------------

@pytest.mark.parametrize("path, expected", [
    ("report.csv", f"report_{STAMP}.csv"),
    ("report.v2.csv", f"report.v2_{STAMP}.csv"),        # only the last dot is the extension
    ("report", f"report_{STAMP}"),                      # no extension
    (".csv", f".csv_{STAMP}"),                          # hidden-style name: no extension at all
    ("my.reports/report", f"my.reports/report_{STAMP}"),  # dot in folder, none in file
    ("my.reports/report.csv", f"my.reports/report_{STAMP}.csv"),
    ("reports/report.xlsx", f"reports/report_{STAMP}.xlsx"),
    ("report.", f"report_{STAMP}."),                    # trailing dot is kept
    ("REPORT.CSV", f"REPORT_{STAMP}.CSV"),              # case is preserved
])
def test_add_timestamp_relative_paths(path, expected):
    assert add_timestamp(path, STAMP) == expected


def test_add_timestamp_absolute_windows_path():
    path = r"C:\Users\me\my.reports\report.v2.xlsx"
    result = add_timestamp(path, STAMP)
    assert result == rf"C:\Users\me\my.reports\report.v2_{STAMP}.xlsx"


def test_add_timestamp_windows_path_dot_only_in_folder():
    # splitext on Windows treats the backslash as a separator, so the folder dot is ignored.
    if sys.platform != "win32":
        pytest.skip("backslash is a separator only on Windows")
    path = r"C:\my.reports\report"
    assert add_timestamp(path, STAMP) == rf"C:\my.reports\report_{STAMP}"


def test_add_timestamp_keeps_folder_part_unchanged():
    result = add_timestamp("a/b.c/d.txt", STAMP)
    assert result.startswith("a/b.c/")


# --- main() end to end, with the real add_timestamp ----------------------

@pytest.fixture
def site(make_site):
    return make_site({
        f"{BASE}/sitemap.xml": FakeResponse(sitemap(f"{BASE}/a")),
        f"{BASE}/a": FakeResponse(PAGE_HTML, content_type="text/html"),
    })


def run_with_stamp(monkeypatch, stamp, *args):
    """Run main() with a fixed clock and WITHOUT patching add_timestamp."""
    monkeypatch.setattr(sys, "argv", ["seo_checker.py", BASE, "--delay", "0", *args])
    monkeypatch.setattr(time, "strftime", lambda fmt, *a: stamp)
    main()


def test_main_csv_gets_timestamped_name(site, monkeypatch, tmp_path):
    run_with_stamp(monkeypatch, STAMP, "--csv", str(tmp_path / "report.csv"))
    assert (tmp_path / f"report_{STAMP}.csv").exists()
    assert not (tmp_path / "report.csv").exists()


def test_main_xlsx_gets_timestamped_name(site, monkeypatch, tmp_path):
    run_with_stamp(monkeypatch, STAMP, "--xlsx", str(tmp_path / "report.xlsx"))
    out_file = tmp_path / f"report_{STAMP}.xlsx"
    assert load_workbook(out_file)["SEO report"]["A2"].value == f"{BASE}/a"
    assert not (tmp_path / "report.xlsx").exists()


def test_main_uses_expected_strftime_format(site, monkeypatch, tmp_path):
    formats = []

    def fake_strftime(fmt, *a):
        formats.append(fmt)
        return STAMP

    monkeypatch.setattr(sys, "argv", ["seo_checker.py", BASE, "--delay", "0",
                                      "--csv", str(tmp_path / "r.csv")])
    monkeypatch.setattr(time, "strftime", fake_strftime)
    main()
    assert formats == ["%Y-%m-%d_%H%M%S"]  # computed once per run


def test_main_real_clock_stamp_looks_like_a_timestamp(site, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "argv", ["seo_checker.py", BASE, "--delay", "0",
                                      "--csv", str(tmp_path / "r.csv")])
    main()
    files = list(tmp_path.glob("r_*.csv"))
    assert len(files) == 1
    stamp = files[0].stem[len("r_"):]
    time.strptime(stamp, "%Y-%m-%d_%H%M%S")  # raises ValueError if the format is wrong


def test_main_existing_plain_file_is_left_untouched(site, monkeypatch, tmp_path):
    old = tmp_path / "report.csv"
    old.write_text("precious old data", encoding="utf-8")
    run_with_stamp(monkeypatch, STAMP, "--csv", str(old))
    assert old.read_text(encoding="utf-8") == "precious old data"
    assert (tmp_path / f"report_{STAMP}.csv").exists()


def test_main_two_runs_with_different_stamps_make_two_files(site, monkeypatch, tmp_path):
    target = str(tmp_path / "report.csv")
    run_with_stamp(monkeypatch, "2026-11-15_100000", "--csv", target)
    run_with_stamp(monkeypatch, "2026-11-15_100001", "--csv", target)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["report_2026-11-15_100000.csv", "report_2026-11-15_100001.csv"]


def test_main_csv_and_xlsx_share_the_stamp(site, monkeypatch, tmp_path):
    run_with_stamp(monkeypatch, STAMP, "--csv", str(tmp_path / "out.csv"),
                   "--xlsx", str(tmp_path / "out.xlsx"))
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == [f"out_{STAMP}.csv", f"out_{STAMP}.xlsx"]


def test_main_console_shows_timestamped_names(site, monkeypatch, tmp_path, capsys):
    csv_path = tmp_path / "out.csv"
    xlsx_path = tmp_path / "out.xlsx"
    run_with_stamp(monkeypatch, STAMP, "--csv", str(csv_path), "--xlsx", str(xlsx_path))
    out = capsys.readouterr().out
    assert f"CSV report written to {tmp_path / f'out_{STAMP}.csv'}" in out
    assert f"Excel report written to {tmp_path / f'out_{STAMP}.xlsx'}" in out
    assert str(csv_path) not in out  # the plain name must not appear
    assert str(xlsx_path) not in out


def test_main_missing_folder_fails_before_any_request(monkeypatch, make_site, tmp_path, capsys):
    fake = make_site({})
    missing = tmp_path / "nope" / "r.csv"
    with pytest.raises(SystemExit) as exc:
        run_with_stamp(monkeypatch, STAMP, "--csv", str(missing))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "folder does not exist" in err
    assert str(tmp_path / "nope") in err
    assert fake.requested == []
    assert not (tmp_path / "nope").exists()


def test_main_missing_folder_for_xlsx_fails_before_any_request(monkeypatch, make_site, tmp_path):
    fake = make_site({})
    with pytest.raises(SystemExit) as exc:
        run_with_stamp(monkeypatch, STAMP, "--xlsx", str(tmp_path / "nope" / "r.xlsx"))
    assert exc.value.code == 2
    assert fake.requested == []


def test_main_without_report_options_creates_no_files(site, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    run_with_stamp(monkeypatch, STAMP)
    assert list(tmp_path.iterdir()) == []


def test_main_folder_with_dot_and_extensionless_name(site, monkeypatch, tmp_path):
    folder = tmp_path / "my.reports"
    folder.mkdir()
    run_with_stamp(monkeypatch, STAMP, "--csv", str(folder / "report"))
    assert (folder / f"report_{STAMP}").exists()
    assert not (folder / "report").exists()


def test_main_same_stamp_twice_overwrites_the_file(site, monkeypatch, tmp_path):
    # Same stamp twice (two runs in the same second): the second run overwrites the first file.
    target = str(tmp_path / "report.csv")
    run_with_stamp(monkeypatch, STAMP, "--csv", target)
    run_with_stamp(monkeypatch, STAMP, "--csv", target)
    assert [p.name for p in tmp_path.iterdir()] == [f"report_{STAMP}.csv"]

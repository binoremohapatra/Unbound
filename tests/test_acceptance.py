"""Tests for tools/acceptance.py table/exit-code logic (Binore). Checks themselves are
exercised live by running the tool; here we verify the verdict math with fake results."""
import pytest

from tools.acceptance import (CheckResult, FAIL, PASS, SKIP, clock_violations,
                              exit_code, format_table, summarize)


def test_all_pass_is_green():
    rs = [CheckResult("a", PASS, "ok"), CheckResult("b", PASS, "ok")]
    assert exit_code(rs) == 0
    assert "ACCEPTANCE: PASS" in format_table(rs)


def test_any_fail_blocks():
    rs = [CheckResult("a", PASS), CheckResult("b", FAIL, "boom")]
    assert exit_code(rs) == 1
    assert "ACCEPTANCE: FAIL" in format_table(rs)


def test_required_skip_never_counts_as_pass():
    rs = [CheckResult("a", PASS), CheckResult("b", SKIP, "no camera", required=True)]
    assert exit_code(rs) == 1                       # required skip = unpassed
    assert summarize(rs) == (0, 1)


def test_optional_skip_is_allowed():
    rs = [CheckResult("a", PASS), CheckResult("bundle", SKIP, "no pyinstaller", required=False)]
    assert exit_code(rs) == 0


def test_fail_and_required_skip_both_count():
    rs = [CheckResult("a", FAIL), CheckResult("b", SKIP, required=True),
          CheckResult("c", SKIP, required=False)]
    fails, unpassed = summarize(rs)
    assert fails == 1 and unpassed == 2             # FAIL + required SKIP
    assert exit_code(rs) == 1


def test_table_lists_every_check_with_detail():
    rs = [CheckResult("env", PASS, "py3.11"), CheckResult("doctor", FAIL, "camera FAIL")]
    table = format_table(rs)
    assert "env" in table and "py3.11" in table and "camera FAIL" in table
    assert "1 passed, 0 skipped, 1 failed" in table


def test_clock_violations_flags_real_calls_only(tmp_path):
    good = tmp_path / "good.py"
    good.write_text("import time\n# time.time() in a comment is fine\n"
                    "x = 'time.monotonic()'\nnow = __import__('core.clock', fromlist=['now']).now()\n")
    bad = tmp_path / "bad.py"
    bad.write_text("import time\nt = time.time()\nm = time.monotonic()\n")
    assert clock_violations(good) == []
    hits = clock_violations(bad)
    assert len(hits) == 2 and all("time." in h for h in hits)

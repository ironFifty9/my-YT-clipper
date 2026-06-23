"""
tests/test_utils.py — Unit tests for core/utils.py

All three functions (time_to_seconds, safe_filename, progress_bar) are pure
and stateless — no Flask, no I/O, no mocking needed.  These tests run fast
and document the expected behaviour for every supported input format.
"""

import pytest

from core.utils import progress_bar, safe_filename, time_to_seconds


# ══════════════════════════════════════════════════════════════════════════════
# time_to_seconds
# ══════════════════════════════════════════════════════════════════════════════

class TestTimeToSeconds:
    """Tests for time_to_seconds(t: str) -> float."""

    # ── Plain seconds ──────────────────────────────────────────────────────────

    def test_integer_seconds(self):
        assert time_to_seconds("90") == 90.0

    def test_zero_seconds(self):
        assert time_to_seconds("0") == 0.0

    def test_float_seconds(self):
        assert time_to_seconds("90.5") == 90.5

    def test_integer_with_decimal_zero(self):
        assert time_to_seconds("60.0") == 60.0

    # ── MM:SS format ──────────────────────────────────────────────────────────

    def test_mm_ss_basic(self):
        assert time_to_seconds("1:30") == 90.0

    def test_mm_ss_zero_minutes(self):
        assert time_to_seconds("0:05") == 5.0

    def test_mm_ss_sub_second(self):
        # Fractional seconds are supported: "1:30.5" = 90.5 s
        assert time_to_seconds("1:30.5") == 90.5

    def test_mm_ss_large_minutes(self):
        assert time_to_seconds("60:00") == 3600.0

    # ── HH:MM:SS format ───────────────────────────────────────────────────────

    def test_hh_mm_ss_basic(self):
        assert time_to_seconds("1:02:30") == 3750.0

    def test_hh_mm_ss_zero_hours(self):
        assert time_to_seconds("0:00:45") == 45.0

    def test_hh_mm_ss_all_zeros(self):
        assert time_to_seconds("0:00:00") == 0.0

    def test_hh_mm_ss_large(self):
        # 1 hour = 3600 s
        assert time_to_seconds("1:00:00") == 3600.0

    def test_hh_mm_ss_fractional(self):
        assert time_to_seconds("0:01:30.5") == 90.5

    # ── Input coercion ─────────────────────────────────────────────────────────

    def test_leading_trailing_whitespace_stripped(self):
        assert time_to_seconds("  90  ") == 90.0

    def test_non_string_coerced(self):
        # Function coerces via str() before parsing
        assert time_to_seconds(90) == 90.0   # type: ignore[arg-type]

    # ── Invalid formats raise ValueError ──────────────────────────────────────

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError, match="Invalid time format"):
            time_to_seconds("abc")

    def test_negative_not_supported(self):
        with pytest.raises(ValueError):
            time_to_seconds("-5")

    def test_too_many_colons(self):
        # Four segments are not a recognised format
        with pytest.raises(ValueError):
            time_to_seconds("1:2:3:4")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            time_to_seconds("")


# ══════════════════════════════════════════════════════════════════════════════
# safe_filename
# ══════════════════════════════════════════════════════════════════════════════

class TestSafeFilename:
    """Tests for safe_filename(name: str) -> str."""

    def test_plain_name_unchanged(self):
        assert safe_filename("my_clip") == "my_clip"

    def test_spaces_replaced(self):
        assert safe_filename("my clip") == "my_clip"

    def test_special_chars_replaced(self):
        result = safe_filename("hello world!")
        assert "!" not in result
        assert " " not in result

    def test_dots_and_hyphens_preserved(self):
        assert safe_filename("clip-v1.0") == "clip-v1.0"

    def test_path_traversal_sanitised(self):
        # "../../../etc/passwd" must not survive as-is
        result = safe_filename("../../../etc/passwd")
        assert ".." not in result
        assert "/" not in result

    def test_all_special_chars_returns_fallback(self):
        # "!@#$%" → all replaced → "____" → stripped → "" → fallback "clip"
        assert safe_filename("!@#$%") == "clip"

    def test_empty_string_returns_fallback(self):
        assert safe_filename("") == "clip"

    def test_leading_underscores_stripped(self):
        result = safe_filename("!!!hello")
        assert not result.startswith("_")

    def test_trailing_underscores_stripped(self):
        result = safe_filename("hello!!!")
        assert not result.endswith("_")

    def test_unicode_safe(self):
        # Non-ASCII word chars (e.g. accented letters) are replaced with _
        result = safe_filename("café")
        assert isinstance(result, str)
        assert len(result) > 0


# ══════════════════════════════════════════════════════════════════════════════
# progress_bar
# ══════════════════════════════════════════════════════════════════════════════

class TestProgressBar:
    """Tests for progress_bar(step, total=4, width=18) -> str."""

    def test_step_zero_is_empty(self):
        bar = progress_bar(0)
        assert "░" in bar
        assert "█" not in bar
        assert "0%" in bar

    def test_step_four_is_full(self):
        bar = progress_bar(4)
        assert "█" in bar
        assert "░" not in bar
        assert "100%" in bar

    def test_step_two_is_half(self):
        bar = progress_bar(2)
        assert "50%" in bar
        # Half filled — both characters should be present
        assert "█" in bar
        assert "░" in bar

    def test_output_is_code_span(self):
        # Result must be wrapped in backticks for Telegram monospace rendering
        bar = progress_bar(1)
        assert bar.startswith("`")
        assert bar.endswith("`")

    def test_output_contains_brackets(self):
        bar = progress_bar(0)
        assert "[" in bar and "]" in bar

    def test_total_width_respected(self):
        # With width=10, the bar section should have exactly 10 characters
        bar = progress_bar(1, total=4, width=10)
        # Strip backticks and brackets to isolate the bar body
        inner = bar.strip("`").split("[")[1].split("]")[0]
        assert len(inner) == 10

    def test_custom_total(self):
        # step=5 out of 10 = 50%
        bar = progress_bar(5, total=10)
        assert "50%" in bar

    def test_step_one_of_four(self):
        bar = progress_bar(1)
        assert "25%" in bar

    def test_step_three_of_four(self):
        bar = progress_bar(3)
        assert "75%" in bar

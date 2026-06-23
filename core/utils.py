"""
core/utils.py — Shared utility functions.

Pure, stateless functions with no external dependencies beyond the standard
library. They have no side effects and can be unit-tested in isolation without
spinning up Flask, Telegram, or ffmpeg.
"""

import re   # regular expressions used in time_to_seconds and safe_filename


def time_to_seconds(t: str) -> float:
    """
    Convert a human-readable time string into a float number of seconds.

    Accepted input formats (all case-insensitive, leading/trailing spaces allowed):
      - Plain seconds (int or float):  "90"  →  90.0
                                       "90.5" →  90.5
      - MM:SS:                         "1:30"  →  90.0
                                       "0:05"  →  5.0
      - HH:MM:SS:                      "1:02:30" →  3750.0
                                       "0:00:45" →  45.0

    Args:
        t: The time string to convert. Non-string types are coerced via str().

    Returns:
        Equivalent number of seconds as a float.

    Raises:
        ValueError: If the string doesn't match any recognised format.
                    The error message includes the rejected value and a format hint.

    Examples:
        >>> time_to_seconds("90")
        90.0
        >>> time_to_seconds("1:30")
        90.0
        >>> time_to_seconds("1:02:30")
        3750.0
    """
    t = str(t).strip()   # normalise: coerce to str, remove surrounding whitespace

    # Pattern: one or more digits, optionally followed by a decimal part.
    # Matches "90", "90.5", "0", etc. — raw second values.
    if re.match(r"^\d+(\.\d+)?$", t):
        return float(t)

    # Split on ":" to detect MM:SS vs HH:MM:SS
    parts = t.split(":")
    if len(parts) == 2:
        # MM:SS — e.g. "1:30" → 1 * 60 + 30.0 = 90.0
        # parts[1] is float() to support sub-second precision: "1:30.5"
        return int(parts[0]) * 60 + float(parts[1])
    elif len(parts) == 3:
        # HH:MM:SS — e.g. "1:02:30" → 3600 + 120 + 30 = 3750.0
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])

    # Nothing matched — raise with a clear, actionable message.
    raise ValueError(
        f"Invalid time format: '{t}'. Use HH:MM:SS, MM:SS, or plain seconds."
    )


def safe_filename(name: str) -> str:
    """
    Sanitise a user-supplied filename so it is safe for the local filesystem
    and for display in Telegram captions.

    Rules applied:
      - Any character that is not a word character (a-z, A-Z, 0-9, _),
        a hyphen (-), or a dot (.) is replaced with an underscore (_).
      - Leading and trailing underscores are stripped.
      - If the result is empty after sanitisation, "clip" is returned as a
        safe fallback (e.g. if the input was all special characters).

    Args:
        name: The raw filename string from the user (no extension expected).

    Returns:
        A sanitised string suitable for use in os.path.join() and Telegram text.

    Examples:
        >>> safe_filename("my clip!")
        "my_clip_"   →  "my_clip"   (trailing _ stripped)
        >>> safe_filename("../../../etc/passwd")
        "______etc_passwd"   →  "etc_passwd"  (leading _ stripped)
        >>> safe_filename("!@#$%")
        "clip"   (fallback)
    """
    # Strip path-traversal sequences first: collapse any run of two or more
    # consecutive dots into a single dot BEFORE the character whitelist is
    # applied.  This prevents "../../../etc/passwd" from producing a result
    # that still contains ".." after replacing "/" and other special chars.
    #
    # Example trace for "../../../etc/passwd":
    #   1. re.sub(r'\.{2,}', '.', ...) → "./././etc/passwd"
    #   2. re.sub(r'[^\w\-.]', '_', ...) → "._._._etc_passwd"
    #   3. .strip("_")                  → "etc_passwd"   ✓
    name = re.sub(r"\.{2,}", ".", name)

    # Replace any remaining character that is not: word char (a-z, A-Z, 0-9, _),
    # hyphen, or a SINGLE dot with an underscore.
    return re.sub(r"[^\w\-.]", "_", name).strip("_") or "clip"


def progress_bar(step: int, total: int = 4, width: int = 18) -> str:
    """
    Generate a Markdown-formatted progress bar for display in a Telegram message.

    The bar uses block characters:
      █ (U+2588) for completed portions
      ░ (U+2591) for remaining portions

    Wrapped in backticks so Telegram renders it in a monospace font,
    ensuring the bar aligns correctly on all devices.

    Args:
        step:  Current step number (0-based from 0 to total inclusive).
        total: Total number of steps (default: 4 for the four pipeline stages).
        width: Number of block characters in the bar (default: 18).

    Returns:
        A Markdown code-span string, e.g.: `[█████████░░░░░░░░░] 50%`

    Examples:
        >>> progress_bar(0)   # start
        '`[░░░░░░░░░░░░░░░░░░]   0%`'
        >>> progress_bar(2)   # halfway
        '`[█████████░░░░░░░░░]  50%`'
        >>> progress_bar(4)   # done
        '`[██████████████████] 100%`'
    """
    filled = int(width * step / total)          # number of filled blocks
    bar    = "█" * filled + "░" * (width - filled)  # combine filled + empty
    pct    = int(100 * step / total)            # percentage as integer
    # Wrap in backticks for Telegram monospace (Markdown code span)
    return f"`[{bar}] {pct}%`"

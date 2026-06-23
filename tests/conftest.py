"""
tests/conftest.py — Shared pytest fixtures.

Sets up the Flask test client and provides an autouse fixture that resets
the in-memory job store (and semaphore) before every test so that tests are
fully isolated and independent of execution order.

Important: environment variables must be set **before** any project module is
imported.  This file is loaded by pytest before test collection starts, so the
`os.environ` assignments at the top take effect for the entire test session.

The ffmpeg startup check in app.py (subprocess.run) is patched with a
context-manager mock before the Flask application is imported, preventing
tests from requiring ffmpeg to be installed in the test environment.
"""

import os
import threading
from unittest.mock import MagicMock, patch

import pytest

# ── Environment — must precede all project imports ─────────────────────────────
os.environ["SECRET_KEY"] = "test-secret-key-for-pytest"
os.environ.pop("REDIS_URL", None)       # force in-memory backend in all tests
os.environ["DOWNLOAD_DIR"] = "downloads"  # keep same as default

# ── Import Flask app with ffmpeg check mocked out ─────────────────────────────
# app.py calls subprocess.run(["ffmpeg", "-version"]) at module load time.
# We patch subprocess.run globally for this import so tests don't need ffmpeg.
_ffmpeg_stub = MagicMock(returncode=0, stdout=b"ffmpeg version 6.0-test\n")

with patch("subprocess.run", return_value=_ffmpeg_stub):
    from app import app as _flask_app   # noqa: E402 — intentionally late import

from config import MAX_JOBS  # noqa: E402


# ── Fixtures ───────────────────────────────────────────────────────────────────

@pytest.fixture()
def app():
    """Return the Flask app configured for testing."""
    _flask_app.config["TESTING"] = True
    return _flask_app


@pytest.fixture()
def client(app):
    """Return a Flask test client for making HTTP requests."""
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def reset_state():
    """
    Reset the in-memory job store and semaphore before every test.

    This fixture is `autouse=True`, meaning pytest runs it automatically for
    every test function without needing to declare it explicitly.

    Steps:
      1. Clear `_jobs` and `_chat_daily` dicts inside the backend.
      2. Drain the CLIP_SEMAPHORE (release any slots held by a previous test).
      3. Refill it to MAX_JOBS so every test starts with a full capacity pool.

    The semaphore object itself is not replaced — only its internal counter is
    adjusted — so references held by `routes.clip` (imported at session start)
    remain valid throughout the test session.
    """
    import core.jobs as jobs_module

    backend = jobs_module._backend

    # Clear job store and daily rate-limit tracker atomically.
    if hasattr(backend, "_lock"):
        with backend._lock:
            if hasattr(backend, "_jobs"):
                backend._jobs.clear()
            if hasattr(backend, "_chat_daily"):
                backend._chat_daily.clear()

    # Reset CLIP_SEMAPHORE: drain all available tokens, then restore to MAX_JOBS.
    # This handles cases where a previous test acquired a slot but never released it
    # (e.g. when threading.Thread is mocked and the worker never runs).
    sem = jobs_module.CLIP_SEMAPHORE
    while sem.acquire(blocking=False):
        pass   # drain all available tokens
    for _ in range(MAX_JOBS):
        sem.release()   # restore to full capacity

    yield   # test runs here
    # No teardown needed — next test's setup will clean up.

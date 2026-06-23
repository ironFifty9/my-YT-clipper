"""
YouTube Clipper Bot — Flask Application Factory.

Intentionally thin application factory. All business logic is split across:
  - core.worker   : download, cut, and upload pipeline
  - core.jobs     : in-memory job store, pruning, and concurrency control
  - core.telegram : Telegram Bot API helpers (send, edit, upload)
  - core.utils    : time parsing, filename sanitisation, progress bar
  - routes.clip   : HTTP endpoints (registered as a Flask Blueprint)
  - config        : all environment variables and runtime constants

Startup sequence (runs once at import time when gunicorn loads this module):
  a. Configure root logging format
  b. Create the Flask app and register the Blueprint
  c. Create or clean the download directory
  d. Verify ffmpeg is on PATH — crash loudly rather than failing mid-request
  e. Warn if SECRET_KEY was not set in the environment
  f. Kick off the background job-pruner daemon thread
"""

import logging    # standard library logging — used for structured server logs
import os         # filesystem operations: makedirs, listdir, remove
import subprocess # used solely for the startup ffmpeg version check

from flask import Flask   # the web framework

import config               # centralised env-var constants (PORT, DOWNLOAD_DIR, etc.)
from core.jobs import start_pruner  # starts the background job-pruning thread
from routes.clip import bp           # Flask Blueprint containing all HTTP routes


# ── Logging ────────────────────────────────────────────────────────────────────
# Configure the root logger once, here, before anything else runs.
# All child loggers (core.worker, routes.clip, etc.) inherit this format
# automatically because they use getLogger(__name__).
logging.basicConfig(
    level=logging.INFO,
    # Format: timestamp + level + module name + message
    # e.g. "2025-01-15 10:30:00,123 INFO [routes.clip] Job abc123 created | ..."
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger(__name__)   # logger scoped to this module ("__main__")


# ── Flask app ──────────────────────────────────────────────────────────────────
# Create the Flask application object. __name__ is passed so Flask knows where
# to find templates/static files (not used here, but required by convention).
app = Flask(__name__)

# Register the clip Blueprint, which contributes /, /clip, /status/<id>,
# and /health routes to the app. Keeping routes in a Blueprint makes them
# independently testable and avoids circular imports.
app.register_blueprint(bp)


# ── Startup checks ─────────────────────────────────────────────────────────────
# These blocks run at import time (i.e. when gunicorn loads the module).
# Failing here aborts the entire server process, which is intentional —
# it is better to crash loudly at deploy time than to fail silently per request.

# 1. Ensure the download directory exists.
#    exist_ok=True means no error if it already exists.
#    Any leftover files from a previous crash are removed to avoid disk leaks.
os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
for _f in os.listdir(config.DOWNLOAD_DIR):
    try:
        os.remove(os.path.join(config.DOWNLOAD_DIR, _f))
    except OSError:
        # Ignore removal errors (e.g. subdirectories, locked files).
        # They don't affect startup correctness.
        pass

# 2. Confirm ffmpeg is installed and executable.
#    We run `ffmpeg -version` and check the return code. If ffmpeg is missing,
#    we raise EnvironmentError immediately rather than letting the first clip
#    request fail after the user has already been waiting.
_ffmpeg_check = subprocess.run(["ffmpeg", "-version"], capture_output=True)
if _ffmpeg_check.returncode != 0:
    raise EnvironmentError(
        "ffmpeg is not installed or not on PATH. Aborting startup."
    )
# Log the ffmpeg version string (first line of its --version output) so it
# appears in Railway/Render logs and is easy to verify after deployment.
log.info("ffmpeg OK — %s", _ffmpeg_check.stdout.decode().splitlines()[0])

# 3. Warn (but do not crash) if SECRET_KEY was not set explicitly.
#    config.py generates a random fallback, but that value changes on every
#    restart, causing Make.com requests to fail with 401 after a redeploy.
if not os.environ.get("SECRET_KEY"):
    log.warning(
        "SECRET_KEY env variable is not set. A random key was generated "
        "and will change on the next restart. Set SECRET_KEY in production."
    )

# 4. Start the background job-pruner daemon thread.
#    It wakes every 5 minutes and deletes finished jobs older than 1 hour,
#    preventing the in-memory JOBS dict from growing without bound.
#    The thread is daemon=True, so it exits automatically when the process ends.
start_pruner()


# ── Entry point ────────────────────────────────────────────────────────────────
# Only reached when running directly with `python app.py` (development).
# In production, gunicorn imports app as a module and never hits this block.
if __name__ == "__main__":
    log.info("Starting YT Clipper API on port %d (dev mode)", config.PORT)
    # debug=False even in dev — Flask debug mode is incompatible with threading.
    app.run(host="0.0.0.0", port=config.PORT, debug=False)

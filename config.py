"""
config.py — Centralised environment configuration.

All runtime constants are defined here. Every other module imports from this
file instead of calling os.environ directly. This makes it easy to:
  - See every tunable value in one place
  - Override values for testing without patching os.environ in multiple files
  - Swap in a dotenv loader (python-dotenv) by adding it here only

Local development — create a .env file (see .env.example) and the values
will be loaded automatically before any os.environ.get() call below.

Environment variables to set in Railway / Render / Docker / .env:
  SECRET_KEY            — API auth key sent in X-Secret-Key header (REQUIRED in prod)
  REDIS_URL             — Redis connection URL  (optional; enables multi-worker mode)
  DOWNLOAD_DIR          — directory for temporary clip files  (default: "downloads")
  MAX_CLIP_DURATION     — max allowed clip length in seconds  (default: 3600 = 1 hour)
  MAX_FILE_MB           — Telegram upload size cap in MB      (default: 50)
  MAX_CONCURRENT_JOBS   — max simultaneous clip workers       (default: 4)
  MAX_JOBS_PER_CHAT_DAY — max clips a single chat may submit per 24 h (default: 5)
  PORT                  — HTTP port gunicorn/Flask listens on (default: 5000)
"""

import os               # for os.environ.get()
import secrets as _secrets  # aliased to avoid polluting the module namespace;
                            # used only for generating the fallback SECRET_KEY

from dotenv import load_dotenv   # reads a local .env file into os.environ

# Load .env **before** any os.environ.get() calls so local overrides take effect.
# In production (Railway / Render / Docker) there is no .env file — load_dotenv()
# is a silent no-op and every value is injected by the platform's env system.
load_dotenv()


# ── Authentication ─────────────────────────────────────────────────────────────
# SECRET_KEY is compared against the X-Secret-Key header in every POST /clip
# request (see routes/clip.py → require_secret decorator).
#
# Production rule: always set SECRET_KEY as an environment variable.
# The `or _secrets.token_hex(32)` fallback generates a cryptographically
# secure 64-character hex string, but it changes on every process restart,
# meaning any Make.com request in-flight during a redeploy will receive a 401.
SECRET_KEY: str = os.environ.get("SECRET_KEY") or _secrets.token_hex(32)


# ── Redis ──────────────────────────────────────────────────────────────────────
# When REDIS_URL is set, core/jobs.py uses a Redis-backed job store that is
# safe across multiple gunicorn worker processes.  gunicorn.conf.py
# automatically scales workers > 1 when this is set.
#
# Format: redis://[:password@]host[:port][/db-number]
# Example (local):    redis://localhost:6379/0
# Example (Railway):  set via the Railway Redis plugin → it auto-injects REDIS_URL
#
# When not set (None), the in-memory backend is used (single-process only).
REDIS_URL: str | None = os.environ.get("REDIS_URL") or None


# ── Storage ────────────────────────────────────────────────────────────────────
# Directory where yt-dlp downloads source videos and ffmpeg writes cut clips.
# Files are cleaned up automatically after upload (see core/worker.py).
# On Railway/Render this path is ephemeral — it lives inside the container
# and is wiped on each redeploy. That is intentional; temp files should not
# persist across deployments.
DOWNLOAD_DIR: str = os.environ.get("DOWNLOAD_DIR", "downloads")


# ── Clip constraints ───────────────────────────────────────────────────────────
# Maximum duration of a single clip in seconds.
# Default is 3600 (1 hour). Clips longer than this are rejected before
# yt-dlp is even invoked, saving bandwidth and CPU.
MAX_DURATION: int = int(os.environ.get("MAX_CLIP_DURATION", 3600))   # seconds

# Telegram Bot API hard limit for files sent via sendDocument is 50 MB.
# We check the clip size before uploading and return a user-friendly error
# if the file exceeds this limit (see core/worker.py Step 3).
MAX_FILE_MB:  int = int(os.environ.get("MAX_FILE_MB", 50))            # megabytes


# ── Concurrency ────────────────────────────────────────────────────────────────
# Maximum number of clip jobs that may run simultaneously (global cap).
# Each job downloads a video, runs ffmpeg, and uploads to Telegram — all of
# which are I/O-heavy operations. Setting this too high risks saturating disk,
# network bandwidth, and yt-dlp's CDN connections simultaneously.
# The value is used to initialise the CLIP_SEMAPHORE in core/jobs.py.
MAX_JOBS: int = int(os.environ.get("MAX_CONCURRENT_JOBS", 4))


# ── Per-chat daily rate limit ──────────────────────────────────────────────────
# Maximum number of clip jobs a single Telegram chat can submit within any
# rolling 24-hour window.  Once this limit is reached, the chat receives a
# 429 response until the oldest job in the window falls outside 24 hours.
#
# This protects server resources from a single heavy user while allowing
# reasonable usage across many chats simultaneously.
MAX_JOBS_PER_CHAT_DAY: int = int(os.environ.get("MAX_JOBS_PER_CHAT_DAY", 5))


# ── Server ─────────────────────────────────────────────────────────────────────
# TCP port the HTTP server listens on.
# Railway injects PORT automatically; other platforms (Render, Heroku) do too.
# Defaults to 5000 for local development.
PORT: int = int(os.environ.get("PORT", 5000))

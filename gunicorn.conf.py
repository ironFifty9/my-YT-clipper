"""
gunicorn.conf.py — Gunicorn WSGI server configuration.

Gunicorn loads this file automatically when launched with:
  gunicorn app:app --config gunicorn.conf.py

All settings are documented at:
  https://docs.gunicorn.org/en/stable/settings.html

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Worker scaling strategy
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
No REDIS_URL (in-memory backend):
  workers=1   — all requests share one Python process and one JOBS dict.
                Multiple workers would give each process its own JOBS dict;
                GET /status/<id> routed to a different worker than POST /clip
                would return 404 for an actively running job.

REDIS_URL set (Redis backend):
  workers=N   — safe to scale; all processes read/write the same Redis store.
                Default is WEB_CONCURRENCY env var, or 4 if not set.
                To override: set WEB_CONCURRENCY=<n> in Railway/Render.

worker_class = "gthread":
  Each gunicorn worker spawns a pool of OS threads to handle requests
  concurrently.  Correct for I/O-bound workloads (yt-dlp and Telegram
  uploads spend most of their time waiting on network, not burning CPU).
"""

import os   # reads PORT, REDIS_URL, WEB_CONCURRENCY at startup


# ── Binding ────────────────────────────────────────────────────────────────────
# Accept connections on all network interfaces (required in containers).
# PORT is injected by Railway / Render / Heroku; defaults to 5000 locally.
bind = f"0.0.0.0:{os.environ.get('PORT', 5000)}"


# ── Worker model ───────────────────────────────────────────────────────────────
_redis_url = os.environ.get("REDIS_URL")

# Scale to multiple workers when Redis is available; stay at 1 otherwise.
# WEB_CONCURRENCY can override the default in Railway/Render without a code push.
workers = int(os.environ.get("WEB_CONCURRENCY", 4 if _redis_url else 1))

# gthread: each worker spawns a thread pool — correct for I/O-heavy workloads.
worker_class = "gthread"

# Thread pool size per worker.
# Default 8 allows 8 concurrent HTTP requests per worker process.
threads = int(os.environ.get("THREADS_PER_WORKER", 8))


# ── Timeouts ───────────────────────────────────────────────────────────────────
# Worst-case pipeline: yt-dlp download (120 s) + ffmpeg cut (60 s) +
# Telegram upload (120 s) = ~300 s.  Set slightly above that.
timeout   = 300   # seconds before gunicorn kills and restarts a worker
keepalive = 5     # seconds to keep idle HTTP connections open


# ── Logging ────────────────────────────────────────────────────────────────────
# "-" → stdout/stderr, both captured by Railway/Render log dashboards.
accesslog = "-"
errorlog  = "-"
loglevel  = "info"
